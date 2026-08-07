"""ChatDeps / AiToolContext / DataScopeContext — AI 模块上下文对象

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §4.6。

两套上下文分工：
  ChatDeps       — /ai/chat SSE 主流上下文（PydanticAI Agent deps_type）
                   加载历史 / 写消息 / 调度 Agent
  AiToolContext  — Gateway 内 tool 执行子流上下文（独立 session）
                   鉴权 / 调 service / 写 ai_operation_log

build_tool_context 把 ChatDeps 转换成 AiToolContext（替换 db，丢弃 agent，
注入 tool_meta），由 Phase 1.2b 的 PydanticAI 包装层调用。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.agents.hitl.events import AiStreamEvent
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.system.models.user import User

if TYPE_CHECKING:
    from app.modules.ai.agents.supervisor.stickiness import StickyDecision
    from app.modules.ai.models.agent import AiAgent


@dataclass
class DataScopeContext:
    """用户数据权限视图（spec §6.2 / §14 v1.5+ subquery 优化）

    accessible_dept_ids：
        None = 全部可见（超管 / data_scope=DATA_SCOPE_ALL），ensure_targets_in_scope 跳过检查
        非 None = set[int] 显式集合（部门数量小，物化无 OOM 风险）

    accessible_user_scope：
        None = 全部可见（同上）
        非 None = SQL Select 子查询，返可见 user_id 集合。ensure_targets_in_scope 走
        SQL EXISTS 路径验证目标是否在可见范围内（避免物化大 set OOM，spec §14）。

    filters：
        SQLAlchemy ColumnElement 列表，给 stats tool 等聚合函数直接拼到 WHERE 子句用（§5.5）。
        默认空 list。User 模型 filter 由 build_data_scope_context 填（最常见 stats 目标），
        其它模型 stats tool 在函数内自行调 get_data_scope_filters(db, user, OtherModel)。
    """

    accessible_dept_ids: set[int] | None
    accessible_user_scope: Select[tuple[int]] | None
    filters: list[ColumnElement[bool]] = field(default_factory=list)


@dataclass
class ChatDeps:
    """PydanticAI Agent 的 deps_type，绑定到 /ai/chat 端点

    spec §4.6 / §17.2：从 core/config.py 迁移到 core/context.py 并扩展。
    旧 ChatDeps（user_id + db 两字段）在 1.5 chat_agent 重写时切换到本类。
    """

    user: User
    perms: set[str]
    db: AsyncSession
    """chat endpoint 的 session，不暴露给 tool（tool 用独立 tool_db）"""

    data_scope: DataScopeContext
    agent: "AiAgent | None"
    """当前会话绑定的 Agent。

    None 表示 supervisor 路由模式下 build_chat_deps 未预加载（run_supervisor=True 时
    由 chat.py 路由块通过 attach_agent_to_deps 注入）。下游访问 deps.agent.code 必须
    先 None 检查（参考 chat.py:654 / resume.py:211）。
    """

    trace_id: str
    """必填非空，build_tool_context 时断言校验，防 "" 漏到 DB 索引"""

    tenant_id: int = 0
    """服务端 tenant resolver 注入的可信租户；禁止从 Chat body/tool args 读取。"""

    conversation_id: int | None = None
    """当前会话 ID（spec §4.5 / §9.3 关联 ai_conversation + ai_operation_log 用）。
    None 表示新建会话首条消息（attach_trace_to_conversation 时仍写）。
    Phase 3.2 execute_tool 写 ai_operation_log.conversation_id 用此字段。"""

    signal_event: Callable[[AiStreamEvent], Awaitable[None]] | None = None
    """SSE 自定义事件回调（spec §8.1）。
    chat.py 创建 asyncio.Queue，把 queue.put 注入此字段；
    execute_tool emit tool_call_started / tool_call_result / confirmation_required 时调。
    None 表示无 SSE 环境（如直接调 execute_tool 的单元测试），事件被静默丢弃。"""

    injection_hit: bool = False
    """§11.1 prompt injection 检测命中标记。
    chat.py 入口对当前 user message 跑 injection_detector，命中则 True。
    execute_tool 据此调 classify_execution_mode(injection_hit=True) → 强制 HITL
    （降级而非拒绝，§11.1）。"""

    client_ip: str | None = None
    """§11.4 客户端 IP（从 FastAPI request.client.host 注入）。
    用于鉴权拒绝时的 IP 级自动拉黑计数；None 表示单元测试 / 旧路径。"""

    sticky_decision: "StickyDecision | None" = None
    """spec §5.3: build_chat_deps 调一次 stickiness 后挂这里；chat.py 入口直接读，
    不再重复调用（避免双调 / 状态不一致）.
    None 表示走 build_chat_deps 旧路径（未传 conversation_id 时）."""

    source_user_message_id: int | None = None
    """本 run 已持久化的 source user message；operation/assistant 因果键。"""

    command_action: str = "send"
    """当前仅启用 send；为共享 finalizer/handoff 保留 action 语义。"""

    guard_owner_token: str | None = None
    """conversation run guard owner；只能由同 token 续期或释放。"""

    guard_handoff: bool = False
    """进入 HITL pending 后为 True；原 SSE 断开不得释放 guard。"""


@dataclass
class AiToolContext:
    """Gateway 内 tool 执行的上下文，独立 session（spec §6.3）

    业务 tool 函数签名约定：async def fn(ctx: AiToolContext, **args)
    不直接接触 PydanticAI 的 RunContext（拆包由装饰器包装层负责，Phase 1.2b）
    """

    user: User
    perms: set[str]
    db: AsyncSession
    """独立 tool_db（Gateway 在 execute_tool 内 AsyncSessionLocal() 创建）"""

    data_scope: DataScopeContext
    trace_id: str
    tool_meta: AiToolMeta
    """聚合 tool 用（如 max_groups / allowed_filters，§5.5）"""

    tenant_id: int = 0
    """继承自 ChatDeps 的可信租户，用于 file/resource ACL。"""

    secrets: dict[str, str] = field(default_factory=dict)
    """sensitive_input 注入点（MVP 留空，v1.5+ 扩展，§7.2）"""


def build_tool_context(
    deps: ChatDeps,
    tool_db: AsyncSession,
    tool_meta: AiToolMeta,
) -> AiToolContext:
    """从 ChatDeps 构造 AiToolContext（spec §4.6）

    - 替换 db：tool 用独立 session（事务边界隔离，§6.3）
    - 丢弃 agent：tool 不需要 Agent 信息（meta 已含）
    - 注入 tool_meta：聚合 tool 读取 max_groups / allowed_filters 等
    - 复用 user / perms / data_scope / trace_id

    在 Phase 1.2b 的 PydanticAI 包装层调用，每次 tool 执行前调用一次。
    """
    assert deps.trace_id, "ChatDeps.trace_id 必填非空，build 前由端点设置"
    return AiToolContext(
        user=deps.user,
        perms=deps.perms,
        db=tool_db,
        data_scope=deps.data_scope,
        trace_id=deps.trace_id,
        tool_meta=tool_meta,
        tenant_id=deps.tenant_id,
        secrets={},  # MVP 留空（§7.2）
    )
