"""Re-prove immutable source domains under the recipient's current G/B fence."""

from sqlalchemy import case, select

from app.core.config import settings
from app.core.exceptions import AuthorizationException, BusinessException
from app.core.field_policy import Access
from app.core.field_policy.contracts import LEVELS
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.authorization_completion_projection import (
    project_completions,
)
from app.modules.ai.service.completion_read_state_service import EffectiveCompletion
from app.modules.ai.service.delivery_source_domains import (
    authority_header,
    validate_receipt,
)
from app.modules.job.models.job import SysJob
from app.modules.system.models.dept import Dept
from app.modules.system.models.file import File
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.delivery_read_context import load_delivery_context

MODELS = {
    "system.user": (User, "user_id"),
    "system.role": (Role, "role_id"),
    "system.dept": (Dept, "dept_id"),
    "system.menu": (Menu, "menu_id"),
    "system.file": (File, "file_id"),
    "job.definition": (SysJob, "job_id"),
    "ai.agent": (AiAgent, "agent_id"),
    "ai.message": (AiMessage, "message_id"),
}


def changed():
    return AuthorizationException(
        "交付来源已失效，请按当前权限重新查询", error_code="FIELD_AUTHORIZATION_CHANGED"
    )


async def _completion(db, tenant, group):
    log = await db.scalar(
        select(AiOperationLog)
        .where(
            AiOperationLog.tenant_id == tenant.tenant_id,
            AiOperationLog.user_id == tenant.actor_user_id,
            AiOperationLog.log_id == int(group["log_id"]),
            AiOperationLog.tool_call_id == group["tool_call_id"],
        )
        .execution_options(populate_existing=True)
    )
    if log is None:
        raise changed()
    action = await db.scalar(
        select(AiPreparedAction)
        .where(
            AiPreparedAction.tenant_id == tenant.tenant_id,
            AiPreparedAction.user_id == tenant.actor_user_id,
            AiPreparedAction.execute_tool_call_id == log.tool_call_id,
        )
        .execution_options(populate_existing=True)
    )
    conversation = await db.scalar(
        select(AiConversation)
        .where(
            AiConversation.tenant_id == tenant.tenant_id,
            AiConversation.user_id == tenant.actor_user_id,
            AiConversation.conversation_id == log.conversation_id,
        )
        .execution_options(populate_existing=True)
    )
    source = await db.scalar(
        select(AiMessage)
        .where(
            AiMessage.tenant_id == tenant.tenant_id,
            AiMessage.message_id == log.source_user_message_id,
            AiMessage.conversation_id == log.conversation_id,
            AiMessage.role == "user",
        )
        .execution_options(populate_existing=True)
    )
    if conversation is None or source is None:
        raise changed()
    states = await project_completions(
        db,
        tenant=tenant,
        actions=[action] if action else [],
        logs=[log],
        conversations={conversation.conversation_id: conversation},
        sources={source.message_id: source},
    )
    state = states.get(log.tool_call_id)
    if (
        state is None
        and log.readonly_snapshot
        and validate_receipt(log.field_authority) is not None
        and (registered := ToolRegistry.get().find(log.tool_name)) is not None
        and registered.meta.readonly
        and registered.meta.agent == log.agent_code
        and log.status in {"success", "failed", "expired", "rejected"}
        and log.finished_at is not None
        and log.queued_at is not None
        and log.finished_at >= log.queued_at
        and (
            log.started_at is None or log.queued_at <= log.started_at <= log.finished_at
        )
        and (
            log.status != "success"
            or log.started_at is not None
            and log.error_code is None
        )
        and (log.status != "failed" or bool(log.error_code))
    ):
        state = EffectiveCompletion(
            log.status, log.error_code, log.finished_at, log.duration_ms, verified=True
        )
    if state is None or (
        not state.unknown
        if group["status"] == "unknown"
        else not state.verified or state.status != group["status"]
    ):
        raise changed()


async def revalidate_sources(db, tx, *, tenant, actor_user_version, receipt):
    with bounded_authorization_reads(
        db, AuthorizationReadBudget(max_statements=4096, max_bytes=128 * 1024 * 1024)
    ):
        return await _revalidate_sources(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            receipt=receipt,
        )


async def _revalidate_sources(db, tx, *, tenant, actor_user_version, receipt):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    if validate_receipt(receipt) is None:
        raise changed()
    header = authority_header(receipt)
    if (
        header["tenant_id"] != str(tenant.tenant_id)
        or header["actor_id"] != str(tenant.actor_user_id)
        or header["build"] != (settings.RELEASE_BUILD_SHA or "development")
    ):
        raise changed()
    contexts = {}
    try:
        for group in receipt["source_domains"]:
            if group.get("kind") in {"file", "artifact"}:
                from app.modules.ai.service.delivery_external_sources import (  # noqa: PLC0415
                    recheck_external,  # noqa: PLC0415
                )

                await recheck_external(
                    db, tx, tenant=tenant, version=actor_user_version, group=group
                )
                continue
            if group.get("kind") == "completion":
                await _completion(db, tenant, group)
                continue
            action = (
                "delivery.message-read"
                if group.get("kind") == "input"
                else group["action_id"]
            )
            context = contexts.get(action)
            if context is None:
                context = await load_delivery_context(
                    db,
                    tx,
                    tenant=tenant,
                    version=actor_user_version,
                    action=action,
                    agents=action.startswith("ai.tool."),
                )
                contexts[action] = context
                if action.startswith("ai.tool."):
                    tool = ToolRegistry.get().find(action.removeprefix("ai.tool."))
                    permissions = agent_authorization_service.tool_permissions(
                        context.actor
                    )
                    if (
                        tool is None
                        or not set(tool.meta.required_perms) <= permissions
                        or tool.meta.agent not in context.visible_agent_codes
                    ):
                        raise changed()
            if group.get("kind") == "input":
                decisions = context.decisions(
                    context.record("ai.message", "input", context.scopes)
                )
                if any(not decisions[name].allowed for name in group["read"]):
                    raise changed()
                continue
            obj = group["object_id"]
            if obj not in MODELS:
                raise changed()
            model, anchor = MODELS[obj]
            table = model.__table__
            queries = tuple((parts[0], Access(*parts[1:])) for parts in group["query"])
            if queries:
                await context.prove(db, obj, table, queries)
            expected = {int(value) for value in group["records"]}
            actual = set()
            scopes = context.scope_sql(obj, table)
            for offset in range(0, len(group["records"]), 256):
                batch = tuple(
                    int(value) for value in group["records"][offset : offset + 256]
                )
                columns = [
                    table.c[anchor],
                    *(
                        case((scope, True), else_=False).label(f"_role_{index}")
                        for index, scope in enumerate(scopes.values())
                    ),
                ]
                for row in (
                    await db.execute(
                        select(*columns)
                        .where(context.base(obj, table), table.c[anchor].in_(batch))
                        .order_by(table.c[anchor])
                    )
                ).mappings():
                    identifier = row[anchor]
                    actual.add(identifier)
                    record = context.record(
                        obj,
                        str(identifier),
                        (
                            rid
                            for index, rid in enumerate(scopes)
                            if row[f"_role_{index}"]
                        ),
                    )
                    decisions = context.decisions(record)
                    for name, level in group["read"]:
                        if (
                            not decisions[name].allowed
                            or LEVELS[decisions[name].level] < LEVELS[level]
                        ):
                            raise changed()
            if actual != expected:
                raise changed()
    except BusinessException:
        raise changed() from None
