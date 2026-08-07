"""spec §6.4 / §7.1c: routing feedback service.

权限校验链：
1. message 存在 → 否则 NotFoundException(AI_MESSAGE_NOT_FOUND)
2. message owner 校验：通过 AiConversation.user_id（AiMessage 本身无 user_id 字段）
   - owner 或超管可提交
3. correctedAgentCode 可见性：复用 list_visible_agents（spec §6.4 明说复用，避免双份维护）
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import is_super_admin
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.ai.service.agent_visibility import list_visible_agents
from app.modules.system.models.user import User


class RoutingFeedbackService:
    async def submit(
        self,
        db: AsyncSession,
        *,
        message_id: int,
        request,
        user: User,
    ) -> None:
        """spec §6.4: 写 ai_message.routing_feedback + 追加 ai_routing_feedback."""
        msg = await db.get(AiMessage, message_id)
        if msg is None or not msg.is_active:
            raise NotFoundException(
                resource_type="AI消息",
                error_code="AI_MESSAGE_NOT_FOUND",
            )

        # spec §6.4: 通过 AiConversation 校验 owner（AiMessage 无 user_id 字段）
        conv = await db.get(AiConversation, msg.conversation_id)
        is_admin = is_super_admin(user)
        if conv is None or (conv.user_id != user.user_id and not is_admin):
            raise AuthorizationException(
                "非消息 owner，无权提交反馈",
                error_code="AI_AUTHORIZATION",
            )

        # 校验 correctedAgentCode（feedback='wrong' 时）
        if request.feedback == "wrong":
            if not request.corrected_agent_code:
                # schema model_validator 已 422 拦；service 兜底返回 400
                raise BusinessRuleException(
                    "feedback='wrong' 时必须提供 correctedAgentCode",
                    error_code="AI_ROUTING_FEEDBACK_MISSING_CORRECTION",
                )
            # spec §6.4: 复用 list_visible_agents（单一真相源，避免 SQL 漂移）
            visible_agents = await list_visible_agents(db, user)
            visible_codes = {a.code for a in visible_agents}
            if request.corrected_agent_code not in visible_codes:
                raise AuthorizationException(
                    f"Agent {request.corrected_agent_code!r} 不可见",
                    error_code="AI_AGENT_NOT_VISIBLE",
                )

        # 写当前态
        msg.routing_feedback = request.feedback

        # 追加历史（append-only）
        feedback_row = AiRoutingFeedback(
            message_id=message_id,
            user_id=user.user_id,
            original_agent=msg.agent_code or "unknown",
            feedback=request.feedback,
            corrected_agent=(
                request.corrected_agent_code if request.feedback == "wrong" else None
            ),
            trace_id=msg.trace_id,
        )
        db.add(feedback_row)


routing_feedback_service = RoutingFeedbackService()
