"""对话核心服务

处理消息发送、历史加载、流式响应。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.agents.chat_agent import create_chat_agent
from app.modules.ai.schemas.message import MessageOut
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.provider_service import provider_service


class ChatService:
    """对话核心服务"""

    async def load_history(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> list[MessageOut]:
        """加载会话历史消息"""
        messages = await conversation_service.get_messages(db, conversation_id, user_id)
        return [MessageOut.model_validate(m) for m in messages]

    async def save_user_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        _user_id: int,
        content: str,
        parts: list[dict] | None = None,
    ):
        """保存用户消息"""
        await conversation_service.save_message(
            db, conversation_id, role="user", content=content, parts=parts
        )

    async def save_assistant_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        content: str,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
    ):
        """保存 AI 响应消息"""
        await conversation_service.save_message(
            db,
            conversation_id,
            role="assistant",
            content=content,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )

    async def create_agent(self, db: AsyncSession, model_name: str | None = None):
        """创建配置好的 Agent"""
        model = await provider_service.resolve_model(db, model_name)
        return create_chat_agent(model)


chat_service = ChatService()
