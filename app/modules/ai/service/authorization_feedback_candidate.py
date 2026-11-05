"""Fixed routing feedback facts under current identity and context locks."""

from sqlalchemy import select

from app.core.auth import ensure_ai_chat_use, is_super_admin
from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.authorization_chat_ingress_candidate import tool_settings
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.auth.service import load_live_user_authority
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)


class FeedbackCandidate:
    async def submit(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        message_id,
        feedback,
        corrected_agent_code=None,
    ):
        if (
            type(message_id) is not int
            or not 0 < message_id < 2**63
            or feedback not in {"correct", "wrong"}
            or (
                feedback == "wrong"
                and (
                    type(corrected_agent_code) is not str
                    or not 1 <= len(corrected_agent_code) <= 64
                )
            )
        ):
            raise _order_error()
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.message_id == message_id,
            )
        )
        if message is None:
            raise NotFoundException("AI消息", error_code="AI_MESSAGE_NOT_FOUND")
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=(message.conversation_id,),
        )
        agents = tuple(
            await db.scalars(select(AiAgent.agent_id).order_by(AiAgent.agent_id))
        )
        _, setting_keys = await tool_settings(db, tenant)
        key = ResourceKey(K.ROUTING_FEEDBACK, tenant.tenant_id, next_id())
        await tx.lock(
            LockPlan(
                existing=tuple(
                    {
                        *identity.keys,
                        *inventory.keys,
                        *setting_keys,
                        *(ResourceKey(K.AGENT, None, number) for number in agents),
                    }
                ),
                new=(NewResource(key),),
            )
        )
        await assert_identity(db, identity)
        await ai_context_candidate.recheck(db, tx, inventory)
        actor = await load_live_user_authority(db, tenant=tenant)
        ensure_ai_chat_use(actor)
        message = await db.get(AiMessage, message_id, populate_existing=True)
        conversation = await db.get(
            AiConversation, message.conversation_id, populate_existing=True
        )
        if (
            not message.is_active
            or conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.deleted_at is not None
        ):
            raise NotFoundException("AI消息", error_code="AI_MESSAGE_NOT_FOUND")
        if conversation.user_id != tenant.actor_user_id and not is_super_admin(actor):
            raise AuthorizationException(
                "非消息 owner，无权提交反馈", error_code="AI_AUTHORIZATION"
            )
        if feedback == "wrong":
            visible = await agent_authorization_service.list_agents(
                db, actor, force_refresh=True
            )
            if corrected_agent_code not in {agent.code for agent in visible}:
                raise AuthorizationException(
                    "纠正 Agent 不可见", error_code="AI_AGENT_NOT_VISIBLE"
                )
        message_key = ResourceKey(
            K.SOURCE_MESSAGE if message.role == "user" else K.ASSISTANT_MESSAGE,
            tenant.tenant_id,
            message_id,
        )
        await tx.update_factual(
            message_key,
            {"routing_feedback": feedback},
            expected={"routing_feedback": message.routing_feedback},
        )
        await tx.insert_reserved(
            key,
            {
                "message_id": message_id,
                "user_id": tenant.actor_user_id,
                "original_agent": message.agent_code or "unknown",
                "feedback": feedback,
                "corrected_agent": corrected_agent_code
                if feedback == "wrong"
                else None,
                "trace_id": message.trace_id,
            },
        )


feedback_candidate = FeedbackCandidate()
