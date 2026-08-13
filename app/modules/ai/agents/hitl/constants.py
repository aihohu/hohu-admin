"""HITL 状态机 enum + DryRunResult

集中定义 HITL 状态、执行模式和 Redis 键约定。

为什么用 enum 而不是字符串字面量：
  - 状态值散落在 operation_log_service / hitl_manager / executor / api/confirm.py 多处
  - 字符串字面量易拼写错（"pendng_confirmation" vs "pending_confirmation"）且 IDE 不补全
  - enum 强约束并映射到数据库的 String(32) 列
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AiOperationStatus(StrEnum):
    """``ai_operation_log.status`` 状态机。

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
    """``ai_operation_log.execution_mode`` 可选值。"""

    AUTONOMOUS = "autonomous"
    HITL = "hitl"


class ConfirmAction(StrEnum):
    """``POST /ai/confirm`` 的 action 字段。"""

    APPROVED = "approved"
    REJECTED = "rejected"


class PreparedActionStatus(StrEnum):
    """持久化网关授权状态。"""

    PREPARED = "prepared"
    PENDING_CONFIRMATION = "pending_confirmation"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {
            PreparedActionStatus.SUCCEEDED,
            PreparedActionStatus.FAILED,
            PreparedActionStatus.REJECTED,
            PreparedActionStatus.EXPIRED,
        }


# 待确认载荷的 Redis key 前缀：
#   ai:confirm:{confirmation_id} → JSON pending payload，TTL 5min
#   ai:failures:{...}            → 连续失败计数（Gateway failures.py 已用）
#   ai:write:{user_id}           → L1 速率（Gateway quota.py 已用）
AI_CONFIRM_REDIS_PREFIX = "ai:confirm"

# 与 ai_operation_log 表关联：DB 中 confirmation_id 也用此格式
AI_OPERATION_LOG_DB_PREFIX = "ai_op"  # 仅用于日志/调试，不进 DB

# Redis Pub/Sub 唤醒通道前缀。
# 完整 channel: f"{AI_HITL_WAKE_CHANNEL_PREFIX}:{confirmation_id}"
# 每 confirmation 独立 channel；wake 时 PUBLISH，hang 时 SUBSCRIBE
AI_HITL_WAKE_CHANNEL_PREFIX = "ai:hitl:wake"

# SSE 续传的 owner 锁。
# 完整 lock_key: f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
# TTL 必须不小于 AI_TOOL_TIMEOUT（默认 30 秒），否则工具执行较慢时锁会先
# 过期 → 新 worker B 抢锁成功 → 双执行 race。设 60s 留余量（tool_timeout 30s + 抖动）。
# 60 秒可覆盖工具超时和调度抖动，避免其他 worker 提前抢锁导致重复执行。
AI_HITL_OWNER_LOCK_PREFIX = "ai:hitl:owner"
AI_HITL_OWNER_LOCK_TTL_SEC = 60


@dataclass(frozen=True)
class DryRunResult:
    """dry-run 函数返回值。

    业务方实现命名约定 `async def _dry_run_<tool>(ctx, **args) -> DryRunResult`，
    装饰器反射查找并回填到 RegisteredTool.dry_run_fn。

    risk.py 的 classify_execution_mode 会调 dry_run_fn 拿 count，
    根据风险和影响行数矩阵判定执行模式。

    ok=False 时 count 应为 0，reason 必填（用于 LLM 反问）。
    """

    ok: bool
    count: int = 0
    reason: str | None = None
    summary_key: str | None = None
    summary_params: dict[str, str | int | float] | None = None
    examples: list[str] | None = None
    confirmation_fields: list[dict[str, str | int | float]] | None = None
    """仅用于 direct HITL canonical presentation 的安全展示覆盖。

    每项必须包含 ``label``、与 frozen args 相等的 ``value``，以及可选的
    ``display_value``。Gateway 先验证 raw value 与冻结参数绑定，再把 display
    value 写入 canonical presentation，禁止出现展示 A、实际执行 B。
    """
    # Gateway-only approval binding. Dry-run SSE/Redis summary serialization omits
    # both fields; the Gateway separately persists only their trusted bindings.
    execution_args: dict[str, Any] | None = None
    business_snapshot: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.ok and self.count > 0:
            raise ValueError(
                "DryRunResult: ok=False 但 count > 0 自相矛盾（dry_run 失败不应有计数）"
            )
        if not self.ok and not self.reason:
            raise ValueError(
                "DryRunResult: ok=False 时 reason 必填（用于 LLM 反问原因）"
            )
