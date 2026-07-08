"""AI Tool Gateway — 鉴权 / 风险分级 / HITL / 调度

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §3 / §6 / §7。

Gateway 是 tool 执行的"中间层"，业务方函数永远不直接被 LLM 调用，
全部经过 Gateway 三件套鉴权 + 审计 + 异常转译 + 脱敏 + 容量限制。
"""

from .executor import execute_tool
from .failures import (
    FAILURE_THRESHOLD,
    FAILURE_TTL_SEC,
    check_repeated_failure,
    clear_failures,
    compute_args_hash,
    record_failure,
)
from .quota import (
    DEFAULT_L1_RATE_PER_MIN,
    DEFAULT_L2_DAILY_QUOTA,
    DEFAULT_L3_TIMEOUT_SEC,
    check_l1_rate_limit,
    check_l2_daily_quota,
    get_l3_timeout,
    is_write_tool,
    with_l3_timeout,
)
from .redact import contains_redacted_marker, redact_secrets
from .result import ToolResult
from .sensitive import GLOBAL_OUTPUT_BLOCKLIST, serialize_for_llm
from .targets import ensure_targets_in_scope

__all__ = [
    "DEFAULT_L1_RATE_PER_MIN",
    "DEFAULT_L2_DAILY_QUOTA",
    "DEFAULT_L3_TIMEOUT_SEC",
    "FAILURE_THRESHOLD",
    "FAILURE_TTL_SEC",
    "GLOBAL_OUTPUT_BLOCKLIST",
    "ToolResult",
    "check_l1_rate_limit",
    "check_l2_daily_quota",
    "check_repeated_failure",
    "clear_failures",
    "compute_args_hash",
    "contains_redacted_marker",
    "ensure_targets_in_scope",
    "execute_tool",
    "get_l3_timeout",
    "is_write_tool",
    "record_failure",
    "redact_secrets",
    "serialize_for_llm",
    "with_l3_timeout",
]
