from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.modules.ai.agents.gateway.redact import redact_secrets
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.conversation import ConversationCreate, ConversationUpdate
from app.utils.pagination import build_filters, paginate


class ConversationService:
    """AI 会话管理服务"""

    async def get_list(self, db: AsyncSession, query, user_id: int):
        field_mapping = {
            "title": ("title", "contains"),
            "status": "status",
        }
        filters = build_filters(
            AiConversation, field_mapping, **query.model_dump(exclude_unset=True)
        )
        # 只看自己的会话
        filters.append(AiConversation.user_id == user_id)
        return await paginate(
            db=db,
            model=AiConversation,
            query_params=query,
            filters=filters,
            order_by=AiConversation.update_time.desc(),
        )

    async def get_by_id(
        self, db: AsyncSession, conversation_id: int, user_id: int | None = None
    ) -> AiConversation:
        obj = await db.get(AiConversation, conversation_id)
        if not obj:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        if user_id and obj.user_id != user_id:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        return obj

    async def create(
        self, db: AsyncSession, data: ConversationCreate, user_id: int
    ) -> AiConversation:
        obj = AiConversation(
            user_id=user_id,
            title=data.title or "新对话",
            system_prompt=data.system_prompt,
        )
        if data.model_name:
            obj.model_name = data.model_name
        db.add(obj)
        return obj

    async def update(
        self,
        db: AsyncSession,
        conversation_id: int,
        data: ConversationUpdate,
        user_id: int,
    ) -> AiConversation:
        obj = await self.get_by_id(db, conversation_id, user_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(obj, field, value)
        return obj

    async def delete(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> None:
        obj = await self.get_by_id(db, conversation_id, user_id)
        await db.delete(obj)

    async def lock_for_delete(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> AiConversation:
        """Lock the conversation before checking durable action blockers."""
        obj = (
            await db.execute(
                select(AiConversation)
                .where(
                    AiConversation.conversation_id == conversation_id,
                    AiConversation.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if obj is None:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        return obj

    async def get_messages(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> list[AiMessage]:
        """获取会话的所有历史消息

        spec §7.4: 加载时再 scrub 一次，防早期版本（脱敏上线前）的脏数据
        """
        await self.get_by_id(db, conversation_id, user_id)  # 验证权限
        stmt = (
            select(AiMessage)
            .where(
                AiMessage.conversation_id == conversation_id,
                AiMessage.is_active.is_(True),
            )
            .order_by(AiMessage.create_time.asc(), AiMessage.message_id.asc())
        )
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        # 防 §7.4 越权回灌：历史消息加载时再 scrub
        for msg in messages:
            if msg.content:
                msg.content = redact_secrets(msg.content)
        return messages

    async def save_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        role: str,
        content: str,
        message_type: str = "text",
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        parts: list[dict] | None = None,
        tool_calls: list[dict] | None = None,
        agent_code: str | None = None,
        parent_message_id: int | None = None,
        trace_id: str | None = None,
        is_active: bool = True,
        supersedes_message_id: int | None = None,
    ) -> AiMessage:
        """保存一条消息

        spec §4.1 step 5 / §7.1b: agent_code 透传到 ai_message.agent_code
        （按消息粒度记录处理 Agent，让历史会话也能还原）.
        spec §7.4: 用户输入保存前先 redact_secrets，防 LLM 上下文回灌
        修订 BUG-FE-18: assistant 消息含 tool_calls 时存 JSON，前端重连还原卡片
        """
        if role == "user" and content:
            content = redact_secrets(content)

        msg = AiMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            message_type=message_type,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            parts=parts,
            tool_calls=tool_calls,
            agent_code=agent_code,
            parent_message_id=parent_message_id,
            trace_id=trace_id,
            is_active=is_active,
            supersedes_message_id=supersedes_message_id,
        )
        db.add(msg)
        return msg


conversation_service = ConversationService()
