"""ChatDeps / AiToolContext / DataScopeContext — AI 模块上下文对象。

两套上下文分工：
  ChatDeps       — /ai/chat SSE 主流上下文（PydanticAI Agent deps_type）
                   加载历史 / 写消息 / 调度 Agent
  AiToolContext  — Gateway 内 tool 执行子流上下文（独立 session）
                   鉴权 / 调 service / 写 ai_operation_log

build_tool_context 把 ChatDeps 转换成 AiToolContext：替换数据库会话、丢弃 Agent，
并注入工具元数据。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext, get_bound_tenant_context
from app.modules.ai.agents.hitl.events import AiStreamEvent
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.system.models.user import User

if TYPE_CHECKING:
    from app.modules.ai.agents.supervisor.stickiness import StickyDecision
    from app.modules.ai.models.agent import AiAgent


@dataclass
class DataScopeContext:
    """用户数据权限视图。

    accessible_dept_ids：
        None = 全部可见（超管 / data_scope=DATA_SCOPE_ALL），ensure_targets_in_scope 跳过检查
        非 None = set[int] 显式集合（部门数量小，物化无 OOM 风险）

    accessible_user_scope：
        None = 全部可见（同上）
        非 None = SQL Select 子查询，返可见 user_id 集合。ensure_targets_in_scope 走
        SQL EXISTS 路径验证目标是否在可见范围内，避免物化大集合导致内存压力。

    filters：
        SQLAlchemy ColumnElement 列表，供聚合工具直接拼接 WHERE 子句。
        默认空 list。User 模型 filter 由 build_data_scope_context 填（最常见 stats 目标），
        其它模型 stats tool 在函数内自行调 get_data_scope_filters(db, user, OtherModel)。
    """

    tenant_id: int
    """Tenant frozen by the shared System data-scope resolver."""

    accessible_dept_ids: set[int] | None
    accessible_user_scope: Select[tuple[int]] | None
    filters: list[ColumnElement[bool]] = field(default_factory=list)
    scope_kinds: frozenset[str] = field(default_factory=frozenset)
    """Exact enabled-role scope kinds supplied by the shared resolver."""


@dataclass
class ChatDeps:
    """绑定到 /ai/chat 端点的 PydanticAI Agent 依赖。"""

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
    """当前会话 ID，用于关联会话和工具操作日志。
    None 表示新建会话首条消息（attach_trace_to_conversation 时仍写）。
    execute_tool 写 ai_operation_log.conversation_id 时使用此字段。"""

    signal_event: Callable[[AiStreamEvent], Awaitable[None]] | None = None
    """SSE 自定义事件回调。
    chat.py 创建 asyncio.Queue，把 queue.put 注入此字段；
    execute_tool emit tool_call_started / tool_call_result / confirmation_required 时调。
    None 表示无 SSE 环境（如直接调 execute_tool 的单元测试），事件被静默丢弃。"""

    injection_hit: bool = False
    """prompt injection 检测命中标记。
    chat.py 入口对当前 user message 跑 injection_detector，命中则 True。
    execute_tool 据此调 classify_execution_mode(injection_hit=True) → 强制 HITL
    命中后强制人工确认而不是直接拒绝。"""

    client_ip: str | None = None
    """从 FastAPI request.client.host 注入的客户端 IP。
    用于鉴权拒绝时的 IP 级自动拉黑计数；None 表示单元测试 / 旧路径。"""

    sticky_decision: "StickyDecision | None" = None
    """build_chat_deps 只计算一次粘滞路由决策，chat.py 入口直接复用，
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

    resolved_model_id: int | None = None
    """Stable model selected for this run and frozen into new prepared actions."""

    resolved_provider_id: int | None = None
    """Stable provider selected for this run and frozen into new prepared actions."""

    data_scope_hash: str | None = None
    """Canonical resolver state used only by scope-bound result projections."""

    projection_dependency_message_ids: tuple[int, ...] = ()
    """Immutable prior assistant projections that may influence this run."""

    @property
    def tenant(self) -> TenantContext:
        """Return the immutable auth-bound tenant and reject context drift."""
        tenant = get_bound_tenant_context(self.user)
        if tenant.tenant_id != self.tenant_id:
            raise RuntimeError(
                "AI chat tenant context does not match authenticated user"
            )
        return tenant


@dataclass
class AiToolContext:
    """Gateway 内工具执行上下文，使用独立数据库会话。

    业务 tool 函数签名约定：async def fn(ctx: AiToolContext, **args)
    不直接接触 PydanticAI 的 RunContext，拆包由装饰器包装层负责。
    """

    user: User
    perms: set[str]
    db: AsyncSession
    """独立 tool_db（Gateway 在 execute_tool 内 AsyncSessionLocal() 创建）"""

    data_scope: DataScopeContext
    trace_id: str
    tool_meta: AiToolMeta
    """工具运行时元数据，例如聚合分组上限和过滤白名单。"""

    tenant_id: int = 0
    """继承自 ChatDeps 的可信租户，用于 file/resource ACL。"""

    data_scope_hash: str | None = None
    """Canonical resolver state used by scope-bound result projections."""

    projection_dependency_message_ids: tuple[int, ...] = ()
    """Immutable prior assistant projections inherited from the chat run."""

    secrets: dict[str, str] = field(default_factory=dict)
    """由可信服务端注入、不会出现在普通工具参数中的敏感值。"""

    approved_business_snapshot: dict[str, Any] | None = None
    """Server-owned business snapshot attached only to an approved action."""

    @property
    def tenant(self) -> TenantContext:
        """Return the immutable auth-bound tenant and reject context drift."""
        tenant = get_bound_tenant_context(self.user)
        if tenant.tenant_id != self.tenant_id:
            raise RuntimeError(
                "AI tool tenant context does not match authenticated user"
            )
        return tenant


def build_tool_context(
    deps: ChatDeps,
    tool_db: AsyncSession,
    tool_meta: AiToolMeta,
    *,
    approved_business_snapshot: dict[str, Any] | None = None,
) -> AiToolContext:
    """从 ChatDeps 构造 AiToolContext。

    - 替换 db：工具使用独立会话隔离事务边界
    - 丢弃 agent：tool 不需要 Agent 信息（meta 已含）
    - 注入 tool_meta：聚合 tool 读取 max_groups / allowed_filters 等
    - 复用 user / perms / data_scope / trace_id

    每次工具执行前由 PydanticAI 包装层调用一次。
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
        data_scope_hash=deps.data_scope_hash,
        projection_dependency_message_ids=(deps.projection_dependency_message_ids),
        secrets={},
        approved_business_snapshot=approved_business_snapshot,
    )
