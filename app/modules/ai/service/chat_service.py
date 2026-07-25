"""对话核心服务

处理消息发送、历史加载、流式响应。

spec §17.2 重写：从旧 ChatDeps(user_id, db) 迁移到完整新 ChatDeps
（user / perms / db / data_scope / agent / trace_id）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.agents.chat_agent import create_chat_agent

# spec §13 决策 15: 从 constants.py import 避免 service ↔ agents.supervisor 循环依赖.
# 现有 `from app.modules.ai.service.chat_service import DEFAULT_AGENT_CODE` 调用方不破坏.
from app.modules.ai.constants import DEFAULT_AGENT_CODE  # noqa: F401  re-export
from app.modules.ai.core.context import ChatDeps
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.schemas.message import MessageOut
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.models.user import User


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
        agent_code: str | None = None,
    ):
        """保存用户消息（spec §4.1 step 5: 透传 agent_code）."""
        await conversation_service.save_message(
            db,
            conversation_id,
            role="user",
            content=content,
            parts=parts,
            agent_code=agent_code,
        )

    async def save_assistant_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        content: str,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tool_calls: list[dict] | None = None,
        agent_code: str | None = None,
    ):
        """保存 AI 响应消息

        Args:
            tool_calls: 本次 assistant 消息关联的 tool 调用事件（修订 BUG-FE-18）。
                        格式 [{"tool": ..., "tool_call_id": ..., "args": ..., "ok": ..., ...}]
                        存到 ai_message.tool_calls JSON 字段，前端 reload 会话时还原
                        streamEvents 让用户重连后能看到 tool-call 卡片。
            agent_code: spec §4.1 step 5 透传到 ai_message.agent_code
        """
        await conversation_service.save_message(
            db,
            conversation_id,
            role="assistant",
            content=content,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tool_calls=tool_calls,
            agent_code=agent_code,
        )

    async def create_agent(
        self,
        db: AsyncSession,
        model_name: str | None = None,
        *,
        user_perms: set[str] | None = None,
        agent_code: str = "user_mgmt",
    ):
        """创建配置好的 Agent

        spec §5.4: 按 user_perms + agent_code 过滤 tool 可见性
        v1.5+ SR-17: 读 sys_config.ai:enabled_tools 控制 default_enabled=False 的 tool
        """
        from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
            get_ai_config_str_list,
        )

        model = await provider_service.resolve_model(db, model_name)
        enabled_extra = await get_ai_config_str_list(db, "ai:enabled_tools", default=[])
        return create_chat_agent(
            model,
            user_perms=user_perms,
            agent_code=agent_code,
            enabled_extra=enabled_extra,
        )

    async def build_chat_deps(
        self,
        db: AsyncSession,
        user: User,
        *,
        agent_code: str | None = None,
        trace_id: str | None = None,
    ) -> ChatDeps:
        """构造完整 ChatDeps（spec §4.6）

        组装顺序：
          1. perms ← collect_user_buttons(user)（启用角色下的按钮权限码）
          2. data_scope ← build_data_scope_context(db, user)（§6.2 物化 accessible_*_ids + filters）
          3. agent ← ai_agent 表查 code（MVP 单 Agent，code='user_mgmt'）
          4. trace_id ← 默认生成 tr_<uuid4.hex[:16]>，可由调用方传入复用

        注意：
          - 超管 perms 不特殊处理（与 §6.4 L1/L2 不豁免一致）
          - agent_code 必须在 ai_agent 表存在，否则抛 ValueError
        """
        perms = set(collect_user_buttons(user))
        data_scope = await build_data_scope_context(db, user)

        # v1.5+: 前端传 agentCode 切换助手；未传则用默认（user_mgmt）
        actual_agent_code = agent_code or DEFAULT_AGENT_CODE
        agent = await self._load_agent(db, actual_agent_code)
        if agent is None:
            raise ValueError(
                f"Agent code {actual_agent_code!r} not found in ai_agent table; "
                f"run scripts/seed_ai_agents.py to seed built-in agents"
            )

        return ChatDeps(
            user=user,
            perms=perms,
            db=db,
            data_scope=data_scope,
            agent=agent,
            trace_id=trace_id or f"tr_{uuid.uuid4().hex[:16]}",
        )

    async def _load_agent(self, db: AsyncSession, agent_code: str) -> AiAgent | None:
        """从 ai_agent 表加载 Agent 行（按 code 唯一索引）"""
        result = await db.execute(select(AiAgent).where(AiAgent.code == agent_code))
        return result.scalars().first()

    async def attach_trace_to_conversation(
        self,
        db: AsyncSession,
        conversation_id: int | None,
        agent_code: str,
        trace_id: str,
    ) -> None:
        """spec §4.5: 把 trace_id + agent_code 写到 ai_conversation 行

        用于审计反查（§9.3 AI Trace 视图按 trace_id 串联）。
        conversation_id=None 时跳过（如新建会话首条消息）。
        """
        if conversation_id is None:
            return
        conv = await db.get(AiConversation, int(conversation_id))
        if conv is None:
            return
        conv.trace_id = trace_id
        conv.agent_code = agent_code


chat_service = ChatService()
