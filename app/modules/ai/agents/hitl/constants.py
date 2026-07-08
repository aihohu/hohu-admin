"""HITL 状态机 enum + DryRunResult

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §4.4 / §5.3 / §8.3。

为什么用 enum 而不是字符串字面量：
  - 状态值散落在 operation_log_service / hitl_manager / executor / api/confirm.py 多处
  - 字符串字面量易拼写错（"pendng_confirmation" vs "pending_confirmation"）且 IDE 不补全
  - enum 强约束 + 双向映射到 DB 的 String(32) 列（§4.4）
"""

from dataclasses import dataclass
from enum import StrEnum


class AiOperationStatus(StrEnum):
    """ai_operation_log.status 状态机（spec §4.4）

    状态流转：
      autonomous 流:  running → success | failed
      HITL 流:        pending_confirmation → running → success | failed
                      pending_confirmation → rejected（用户主动拒绝）
                      pending_confirmation → expired（5min TTL 超时 OR 服务重启）
    """

    RUNNING = "running"
    PENDING_CONFIRMATION = "pending_confirmation"
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        """终态：success / failed / rejected / expired（不可再迁移）"""
        return self in (
            AiOperationStatus.SUCCESS,
            AiOperationStatus.FAILED,
            AiOperationStatus.REJECTED,
            AiOperationStatus.EXPIRED,
        )


class AiExecutionMode(StrEnum):
    """ai_operation_log.execution_mode（spec §4.4）"""

    AUTONOMOUS = "autonomous"
    HITL = "hitl"


class ConfirmAction(StrEnum):
    """POST /ai/confirm 的 action 字段（spec §8.3）"""

    APPROVED = "approved"
    REJECTED = "rejected"


# Redis key 前缀（spec §8.3）：
#   ai:confirm:{confirmation_id} → JSON pending payload，TTL 5min
#   ai:failures:{...}            → 连续失败计数（Gateway failures.py 已用）
#   ai:write:{user_id}           → L1 速率（Gateway quota.py 已用）
AI_CONFIRM_REDIS_PREFIX = "ai:confirm"

# 与 ai_operation_log 表关联：DB 中 confirmation_id 也用此格式
AI_OPERATION_LOG_DB_PREFIX = "ai_op"  # 仅用于日志/调试，不进 DB


@dataclass(frozen=True)
class DryRunResult:
    """dry_run 函数返回值（spec §5.3）

    业务方实现命名约定 `async def _dry_run_<tool>(ctx, **args) -> DryRunResult`，
    装饰器反射查找并回填到 RegisteredTool.dry_run_fn。

    risk.py 的 classify_execution_mode 会调 dry_run_fn 拿 count，
    按 §5.3 矩阵（high + count > 1 → HITL）判定执行模式。

    ok=False 时 count 应为 0，reason 必填（用于 LLM 反问）。
    """

    ok: bool
    count: int = 0
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.ok and self.count > 0:
            raise ValueError(
                "DryRunResult: ok=False 但 count > 0 自相矛盾（dry_run 失败不应有计数）"
            )
        if not self.ok and not self.reason:
            raise ValueError(
                "DryRunResult: ok=False 时 reason 必填（用于 LLM 反问原因）"
            )
