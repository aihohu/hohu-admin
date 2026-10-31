"""Fixed source and routing audit writes, with prepared authority revalidation."""

from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.redact import redact_secrets
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.authorization_catalog_inputs import wire
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.authorization_chat_model import (
    ChatModelSelection,
    model_candidates,
    recheck_model,
    select_model,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.ai.service.routing_log_service import _hash_message
from app.modules.system.models.setting import SystemSetting
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)


@dataclass(frozen=True, slots=True, repr=False)
class RunAuthority:
    model: ChatModelSelection
    agent_id: int
    agent_code: str
    agent_facts: bytes = field(repr=False)
    setting_facts: bytes = field(repr=False)
    setting_keys: tuple
    scope_hash: str
    permissions: frozenset[str]
    dependencies: tuple[int, ...]


def row_facts(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.c}


async def tool_settings(db, tenant):
    rows = list(
        await db.scalars(
            select(SystemSetting)
            .where(
                SystemSetting.tenant_id == tenant.tenant_id,
                SystemSetting.setting_key == "ai:enabled_tools",
            )
            .order_by(SystemSetting.setting_id)
            .execution_options(populate_existing=True)
        )
    )
    return (
        wire([row_facts(row) for row in rows]),
        tuple(
            ResourceKey(K.SETTING, tenant.tenant_id, row.setting_key) for row in rows
        ),
    )


async def freeze_run_authority(db, *, deps, model_name, model_selection=None):
    """Snapshot before Agent construction; this is data, never lock evidence."""
    settings, keys = await tool_settings(db, deps.tenant)
    return RunAuthority(
        model=model_selection
        if model_selection is not None
        else await select_model(db, deps.tenant, model_name),
        agent_id=deps.agent.agent_id,
        agent_code=deps.agent.code,
        agent_facts=wire(row_facts(deps.agent)),
        setting_facts=settings,
        setting_keys=keys,
        scope_hash=deps.data_scope_hash,
        permissions=frozenset(deps.perms),
        dependencies=tuple(deps.projection_dependency_message_ids),
    )


def routing_values(*, tenant, trace_id, conversation_id, audit):
    if type(audit) is not dict or set(audit) != {
        "input_message",
        "candidates",
        "llm_choice",
        "final_agent",
        "reason",
        "latency_ms",
    }:
        raise _order_error()
    if (
        type(trace_id) is not str
        or not 1 <= len(trace_id) <= 64
        or type(audit["input_message"]) is not str
        or len(audit["input_message"].encode()) > 256 * 1024
        or type(audit["candidates"]) not in {tuple, list}
        or len(audit["candidates"]) > 256
        or any(type(v) is not str or not 1 <= len(v) <= 64 for v in audit["candidates"])
        or any(
            v is not None and (type(v) is not str or not 1 <= len(v) <= 64)
            for v in (audit["llm_choice"], audit["final_agent"])
        )
        or type(audit["reason"]) is not str
        or len(audit["reason"]) > 64
        or type(audit["latency_ms"]) is not int
        or not 0 <= audit["latency_ms"] <= 2**31 - 1
    ):
        raise _order_error()
    return {
        "trace_id": trace_id,
        "user_id": tenant.actor_user_id,
        "conversation_id": conversation_id,
        "input_message_hash": _hash_message(
            audit["input_message"], tenant.actor_user_id
        ),
        **{key: value for key, value in audit.items() if key != "input_message"},
        "candidates": list(audit["candidates"]),
    }


class ChatIngressCandidate:
    async def model_candidates(self, db, tx, *, tenant, actor_user_version, model_ref):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        choices = await model_candidates(db, tenant, model_ref)
        await tx.lock(
            LockPlan(
                existing=tuple(
                    {
                        *identity.keys,
                        *(key for choice in choices for key in choice.selection.keys),
                    }
                )
            )
        )
        await assert_identity(db, identity)
        fresh = await model_candidates(db, tenant, model_ref)
        if tuple(choice.selection for choice in fresh) != tuple(
            choice.selection for choice in choices
        ):
            raise _order_error()
        db.expunge_all()
        return fresh

    async def routing_audit(
        self, db, tx, *, tenant, actor_user_version, conversation_id, trace_id, audit
    ):
        values = routing_values(
            tenant=tenant,
            trace_id=trace_id,
            conversation_id=conversation_id,
            audit=audit,
        )
        key = ResourceKey(K.ROUTING_LOG, tenant.tenant_id, next_id())
        if conversation_id is not None:
            await chat_candidate._lock(
                db,
                tx,
                tenant=tenant,
                actor_user_version=actor_user_version,
                conversation_id=conversation_id,
                new=(NewResource(key),),
            )
        else:
            identity = await read_identity(
                db,
                tenant=tenant,
                actor_user_version=actor_user_version,
                permissions=("ai:chat:use",),
            )
            await tx.lock(LockPlan(existing=identity.keys, new=(NewResource(key),)))
            await assert_identity(db, identity)
        await tx.insert_reserved(key, values)
        return key.identity[0]

    async def start_run(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        trace_id,
        content,
        parts,
        authority,
        audit,
    ):
        if (
            type(authority) is not RunAuthority
            or type(content) is not str
            or len(content.encode()) > 256 * 1024
            or (parts is not None and type(parts) is not list)
            or len(wire(parts)) > 256 * 1024
            or len(authority.dependencies) > 16384
            or any(
                type(v) is not int or not 0 < v < 2**63 for v in authority.dependencies
            )
        ):
            raise _order_error()
        values = routing_values(
            tenant=tenant,
            trace_id=trace_id,
            conversation_id=conversation_id,
            audit=audit,
        )
        if values["final_agent"] != authority.agent_code:
            raise _order_error()
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        source = ResourceKey(K.SOURCE_MESSAGE, tenant.tenant_id, next_id())
        route = ResourceKey(K.ROUTING_LOG, tenant.tenant_id, next_id())
        agent_key = ResourceKey(K.AGENT, None, authority.agent_id)
        conversation, actor = await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            new=(NewResource(source, parent), NewResource(route)),
            extra=(*authority.model.keys, agent_key, *authority.setting_keys),
        )
        await recheck_model(db, tx, tenant, authority.model)
        agent = await db.get(AiAgent, authority.agent_id, populate_existing=True)
        settings, keys = await tool_settings(db, tenant)
        if (
            agent is None
            or wire(row_facts(agent)) != authority.agent_facts
            or settings != authority.setting_facts
            or keys != authority.setting_keys
            or authority.permissions
            != frozenset(agent_authorization_service.tool_permissions(actor))
            or authority.scope_hash
            != await result_projection_service.compute_data_scope_hash(db, actor)
        ):
            raise _order_error()
        allowed = await agent_authorization_service.list_agents(
            db, actor, force_refresh=True
        )
        if authority.agent_id not in {item.agent_id for item in allowed}:
            raise AuthorizationException(
                "AI Agent unavailable", error_code="AI_AGENT_FORBIDDEN"
            )
        if not await result_projection_service._authorize_projection_dependencies(
            db,
            actor,
            owner_user_id=tenant.actor_user_id,
            dependency_message_ids=authority.dependencies,
        ):
            raise _order_error()
        await chat_service.ensure_trace_available(
            db,
            conversation_id=conversation_id,
            trace_id=trace_id,
            tenant=tenant,
        )
        if await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=conversation_id,
            user_id=tenant.actor_user_id,
            tenant=tenant,
        ):
            raise BusinessRuleException(
                "该会话仍有待确认或正在执行的操作", error_code="AI_CHAT_RUN_IN_PROGRESS"
            )
        await tx.insert_reserved(
            source,
            {
                "conversation_id": conversation_id,
                "role": "user",
                "content": redact_secrets(content),
                "parts": parts,
                "trace_id": trace_id,
                "agent_code": authority.agent_code,
                "is_active": True,
                "projection_dependency_message_ids": [
                    str(v) for v in authority.dependencies
                ],
            },
        )
        changes = {
            "trace_id": trace_id,
            "agent_code": authority.agent_code,
            "model_name": str(authority.model.model_id),
        }
        await tx.update_factual(
            parent,
            changes,
            expected={key: getattr(conversation, key) for key in changes},
        )
        await tx.insert_reserved(route, values)
        return source.identity[0]


chat_ingress_candidate = ChatIngressCandidate()
