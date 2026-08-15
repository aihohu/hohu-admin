"""agentCode 三种语义的粘滞路由决策树。

值           | conv_agent_code 存在？ | legacy_null_mode？ | 决策
-------------|------------------------|--------------------|----
具体 code    | -                      | -                  | 用该 code，reason=manual_override
"auto"       | -                      | -                  | Supervisor 重路由，reason=auto_explicit
null + legacy| -                      | True               | DEFAULT_AGENT_CODE 旧行为，reason=legacy_null_mode
null + 粘滞值| 是                      | False              | 交给统一 Agent Policy 复核
null + 新会话| 否                     | False              | Supervisor，reason=auto_fallback
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.agents.safety.ai_config import get_ai_config_bool
from app.modules.ai.constants import DEFAULT_AGENT_CODE


@dataclass
class StickyDecision:
    """粘滞决策结果."""

    agent_code: str | None = None
    """最终 agent_code（run_supervisor=True 时为 None，由 router 决定）"""

    run_supervisor: bool = False
    """True → 调 AgentRouter.route；False → 直接用 agent_code"""

    reason: str = ""
    """写 ai_routing_log.reason"""


async def resolve_sticky_agent_code(
    db: AsyncSession,
    *,
    user_id: int,  # noqa: ARG001  调用方契约字段，本函数未直接用（保留日志/审计可读性）
    conversation_id: int | None,  # noqa: ARG001  同上
    agent_code_param: str | None,
    conv_agent_code: str | None,
) -> StickyDecision:
    """解析 agentCode 三种语义并返回 StickyDecision。

    Args:
        db: 用于查 sys_config 的 session.
        user_id: 当前用户 ID（保留参数，决策日志可读性，本函数不直接用）.
        conversation_id: 当前会话 ID（None 表示新会话；本函数不直接查 DB，靠
            调用方传入 conv_agent_code）.
        agent_code_param: 请求中传入的 agentCode 三种语义（具体 code / "auto" / None）.
        conv_agent_code: 会话上轮保存的 agent_code（None 表示新会话或上轮未保存）.

    Returns:
        StickyDecision: 决策结果（agent_code / run_supervisor / reason）.
    """
    # 1. 显式 code：手动覆盖
    if agent_code_param is not None and agent_code_param != "auto":
        return StickyDecision(
            agent_code=agent_code_param, run_supervisor=False, reason="manual_override"
        )

    # 2. "auto"：强制路由
    if agent_code_param == "auto":
        return StickyDecision(run_supervisor=True, reason="auto_explicit")

    # 3. null / 不传
    legacy_mode = await get_ai_config_bool(
        db, "ai:routing_legacy_null_mode", default=False
    )
    if legacy_mode:
        return StickyDecision(
            agent_code=DEFAULT_AGENT_CODE,
            run_supervisor=False,
            reason="legacy_null_mode",
        )

    if conv_agent_code:
        return StickyDecision(
            agent_code=conv_agent_code,
            run_supervisor=False,
            reason="session_sticky",
        )

    return StickyDecision(run_supervisor=True, reason="auto_fallback")
