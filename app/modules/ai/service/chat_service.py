"""对话核心服务

处理消息发送、历史加载、流式响应。

负责构造包含 user、perms、db、data_scope、agent 和 trace_id 的完整 ChatDeps。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import resolve_tenant_id
from app.modules.ai.agents.chat_agent import create_chat_agent

# 从 constants.py 导入，避免 service 与 agents.supervisor 循环依赖。
# 现有 `from app.modules.ai.service.chat_service import DEFAULT_AGENT_CODE` 调用方不破坏.
from app.modules.ai.constants import DEFAULT_AGENT_CODE  # noqa: F401  re-export
from app.modules.ai.core.context import ChatDeps
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
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

    async def ensure_trace_available(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        trace_id: str,
    ) -> None:
        """同一 conversation 内 message/operation 已占用的 run key 不得复用。"""
        message_id = await db.scalar(
            select(AiMessage.message_id)
            .where(
                AiMessage.conversation_id == conversation_id,
                AiMessage.trace_id == trace_id,
            )
            .limit(1)
        )
        operation_id = await db.scalar(
            select(AiOperationLog.log_id)
            .where(
                AiOperationLog.conversation_id == conversation_id,
                AiOperationLog.trace_id == trace_id,
            )
            .limit(1)
        )
        if message_id is not None or operation_id is not None:
            raise BusinessRuleException(
                "traceId 已被其他 ChatCommand 使用",
                error_code="AI_CHAT_TRACE_CONFLICT",
            )

    async def save_user_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        _user_id: int,
        content: str,
        parts: list[dict] | None = None,
        agent_code: str | None = None,
        trace_id: str | None = None,
    ):
        """保存用户消息并透传 ``agent_code``。"""
        message = await conversation_service.save_message(
            db,
            conversation_id,
            role="user",
            content=content,
            parts=parts,
            agent_code=agent_code,
            trace_id=trace_id,
        )
        await db.flush()
        return message

    async def save_assistant_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        content: str,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tool_calls: list[dict] | None = None,
        agent_code: str | None = None,
        trace_id: str | None = None,
        source_user_message_id: int | None = None,
    ):
        """保存 AI 响应消息

        Args:
            tool_calls: 本次 assistant 消息关联的 tool 调用事件（修订 BUG-FE-18）。
                        格式 [{"tool": ..., "tool_call_id": ..., "args": ..., "ok": ..., ...}]
                        存到 ai_message.tool_calls JSON 字段，前端 reload 会话时还原
                        streamEvents 让用户重连后能看到 tool-call 卡片。
            agent_code: 写入 ``ai_message.agent_code``
        """
        message = await conversation_service.save_message(
            db,
            conversation_id,
            role="assistant",
            content=content,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tool_calls=tool_calls,
            agent_code=agent_code,
            trace_id=trace_id,
            parent_message_id=source_user_message_id,
        )
        await db.flush()
        return message

    async def create_agent(
        self,
        db: AsyncSession,
        model_name: str | None = None,
        *,
        user_perms: set[str] | None = None,
        agent_code: str = "user_mgmt",
    ):
        """创建配置好的 Agent

        按 ``user_perms`` 和 ``agent_code`` 过滤工具可见性，并读取
        ``sys_config.ai:enabled_tools`` 控制默认关闭的工具。
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
        conversation_id: int | None = None,
    ) -> ChatDeps:
        """构造包含数据权限和粘滞路由信息的完整 ``ChatDeps``。

        组装顺序：
          1. perms ← collect_user_buttons(user)（启用角色下的按钮权限码）
          2. data_scope ← build_data_scope_context(db, user)，物化可访问 ID 和过滤器
          3. sticky_decision ← resolve_sticky_agent_code（单次调用）
          4. agent ← 根据 sticky_decision 加载（run_supervisor=True 时为 None，
             由 chat.py 路由后通过 attach_agent_to_deps 注入）
          5. trace_id ← 默认生成 tr_<uuid4.hex[:16]>，可由调用方传入复用

        关键：
          - agent 字段可能为 None（run_supervisor=True 时由 chat.py 在路由后注入）
          - sticky_decision 字段挂上 StickyDecision，chat.py 读它再分支（不重调 stickiness）
          - agent_code 找不到 → 抛 ValueError（chat.py 入口 try/except 捕获后 emit AI_ROUTING_FAILED）

        注意：
          - 超管 perm 可见性 bypass（与 HTTP API 层 require_permissions 对齐）：
            is_super_admin(user) → perms = all_registry_perms()，跳过按钮 menu 绑定检查
          - 超级管理员仍受 L1/L2 配额限制
        """
        from app.core.rbac import is_super_admin  # noqa: PLC0415
        from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
            get_ai_config_bool,
        )
        from app.modules.ai.agents.supervisor.stickiness import (  # noqa: PLC0415
            resolve_sticky_agent_code,
        )
        from app.modules.ai.agents.tools.registry import (  # noqa: PLC0415
            all_registry_perms,
        )

        # 超管 bypass：覆盖所有 tool 的 required_perms（agent_code 过滤仍在
        # compute_available_tools 内做，这里只让 perm 维度通过）
        if is_super_admin(user):
            perms = all_registry_perms()
        else:
            perms = set(collect_user_buttons(user))
        data_scope = await build_data_scope_context(db, user)

        # 取会话上轮 agent_code（粘滞用）
        conv_agent_code: str | None = None
        if conversation_id:
            conv = await db.get(AiConversation, int(conversation_id))
            if conv:
                conv_agent_code = conv.agent_code

        # 粘滞路由只在此处计算，chat.py 不重复调用。
        decision = await resolve_sticky_agent_code(
            db,
            user_id=user.user_id,
            conversation_id=conversation_id,
            agent_code_param=agent_code,
            conv_agent_code=conv_agent_code,
        )

        agent: AiAgent | None = None
        if not decision.run_supervisor:
            actual_code = decision.agent_code
            agent = await self._load_agent(db, actual_code) if actual_code else None
            if agent is None and actual_code:
                # agent_code 在 ai_agent 表找不到时抛 ValueError，
                # chat.py 入口（Step 7c 的 try/except）捕获后 emit AI_ROUTING_FAILED，
                # 不让异常透到 FastAPI 默认 500 处理.
                raise ValueError(
                    f"Agent code {actual_code!r} not found in ai_agent table; "
                    f"run scripts/seed_ai_agents.py to seed built-in agents"
                )
        else:
            # run_supervisor=True：检查 supervisor_enabled（决定 deps.agent 是否预加载）
            supervisor_on = await get_ai_config_bool(
                db, "ai:supervisor_enabled", default=True
            )
            if not supervisor_on:
                agent = await self._load_agent(db, DEFAULT_AGENT_CODE)
                if agent is None:
                    raise ValueError(
                        f"DEFAULT_AGENT_CODE {DEFAULT_AGENT_CODE!r} not found"
                    )

        return ChatDeps(
            user=user,
            perms=perms,
            db=db,
            data_scope=data_scope,
            agent=agent,
            trace_id=trace_id or f"tr_{uuid.uuid4().hex[:16]}",
            tenant_id=resolve_tenant_id(user),
            sticky_decision=decision,
        )

    async def attach_agent_to_deps(self, deps: ChatDeps, agent_code: str) -> None:
        """Supervisor 路由完成后将 agent 注入 deps。

        chat.py 在路由块成功后调用，把 router 选定的 agent_code 加载成 AiAgent
        挂到 deps.agent，让下游 attach_trace_to_conversation / create_agent 可用.
        """
        agent = await self._load_agent(deps.db, agent_code)
        if agent is None:
            raise ValueError(f"Routed agent {agent_code!r} not found in ai_agent table")
        deps.agent = agent

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
        """将 trace_id 和 agent_code 写入 ai_conversation。

        AI Trace 视图可按 trace_id 串联审计记录。
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
