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

    async def get_messages(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> list[AiMessage]:
        """获取会话的所有历史消息

        spec §7.4: 加载时再 scrub 一次，防早期版本（脱敏上线前）的脏数据
        """
        await self.get_by_id(db, conversation_id, user_id)  # 验证权限
        stmt = (
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .order_by(AiMessage.create_time.asc())
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
    ) -> AiMessage:
        """保存一条消息

        spec §7.4: 用户输入保存前先 redact_secrets，防 LLM 上下文回灌
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
        )
        db.add(msg)
        return msg


conversation_service = ConversationService()
