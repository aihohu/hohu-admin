"""AI Tool Gateway Prometheus 指标（spec §6.3 v1.5+）

集中定义 8 个核心 metric + record helper（v2+ 提前到 v1.5+）：

  spec §6.3 核心：
    - ai_tool_calls_total{tool, status, risk, execution_mode}  Counter
    - ai_tool_call_duration_seconds{tool}                      Histogram

  HITL（spec §6.3 + §8.4.1 多 worker 配套）：
    - ai_hitl_pending_count{mode}                              Gauge
    - ai_hitl_wake_total{mode, result}                         Counter
    - ai_hitl_pubsub_lost_total                                Counter（防丢失兜底命中）
    - ai_hitl_timeout_total{mode}                              Counter

  配额 / 安全（spec §6.4 / §11）：
    - ai_quota_rejected_total{level}                           Counter
    - ai_security_events_total{event_type}                     Counter

label 命名遵循 Prometheus 最佳实践：
  - 一律 lowercase + snake_case
  - **不含 user_id / confirmation_id / tool_call_id 等高基数 label**（§22 SR-8）
  - 标签集合冻结：上线后改标签 = 数据断档

业务代码不直接操作 prometheus 对象，只调 record_* helper。
"""

from prometheus_client import Counter, Gauge, Histogram

# ============ spec §6.3 核心 ============

TOOL_CALLS_TOTAL = Counter(
    "ai_tool_calls_total",
    "Total AI tool calls",
    ["tool", "status", "risk", "execution_mode"],
)

TOOL_CALL_DURATION_SECONDS = Histogram(
    "ai_tool_call_duration_seconds",
    "AI tool call wall-clock duration (含 HITL 等待时间)",
    ["tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# ============ HITL（spec §6.3 + §8.4.1 多 worker 配套） ============

HITL_PENDING_COUNT = Gauge(
    "ai_hitl_pending_count",
    "Currently pending HITL confirmations",
    ["mode"],  # mode: memory / redis_pubsub
)

HITL_WAKE_TOTAL = Counter(
    "ai_hitl_wake_total",
    "HITL wake attempts",
    ["mode", "result"],  # result: success / not_found
)

HITL_PUBSUB_LOST_TOTAL = Counter(
    "ai_hitl_pubsub_lost_total",
    "HITL pubsub messages lost (race detected via wake_action fallback)",
)

HITL_TIMEOUT_TOTAL = Counter(
    "ai_hitl_timeout_total",
    "HITL hang timeouts (5min TTL expired)",
    ["mode"],
)

# ============ 配额 / 安全（spec §6.4 / §11） ============

QUOTA_REJECTED_TOTAL = Counter(
    "ai_quota_rejected_total",
    "AI quota rejections",
    ["level"],  # level: l1_rate / l2_daily
)

SECURITY_EVENTS_TOTAL = Counter(
    "ai_security_events_total",
    "AI security events",
    ["event_type"],  # injection / keyword / auto_disable / ip_blacklist
)


# ============ record helper（业务代码用这些，不直接操作 metric 对象） ============


def record_tool_call(
    tool: str, status: str, risk: str, execution_mode: str, duration_sec: float
) -> None:
    """埋点单次 tool 调用（spec §6.3）

    Args:
        tool: tool 名（如 user.update_dept）
        status: success / failed / rejected / hitl_approved / hitl_rejected / hitl_expired
        risk: low / high / destructive
        execution_mode: autonomous / hitl
        duration_sec: 墙钟耗时（含 HITL 等待），由调用方 monotonic 测量
    """
    TOOL_CALLS_TOTAL.labels(
        tool=tool, status=status, risk=risk, execution_mode=execution_mode
    ).inc()
    TOOL_CALL_DURATION_SECONDS.labels(tool=tool).observe(duration_sec)


def record_hitl_created(mode: str) -> None:
    """HITL 挂起创建（create_pending 后调）"""
    HITL_PENDING_COUNT.labels(mode=mode).inc()


def record_hitl_resolved(mode: str) -> None:
    """HITL 挂起解决（hang 返回 / 超时 / 异常后调，必须配对调）"""
    HITL_PENDING_COUNT.labels(mode=mode).dec()


def record_hitl_wake(mode: str, result: str) -> None:
    """HITL wake 尝试

    Args:
        mode: memory / redis_pubsub
        result: success / not_found
    """
    HITL_WAKE_TOTAL.labels(mode=mode, result=result).inc()


def record_hitl_pubsub_lost() -> None:
    """HITL pubsub 防丢失分支命中（多 worker 模式专属）

    触发条件：_hang_pubsub subscribe 完成后 GET pending 发现已设 wake_action，
    说明 PUBLISH 在 SUBSCRIBE 之前到达（消息丢失）。是 redis_pubsub 模式
    健康度的核心可观测性指标。
    """
    HITL_PUBSUB_LOST_TOTAL.inc()


def record_hitl_timeout(mode: str) -> None:
    """HITL hang 5min TTL 超时"""
    HITL_TIMEOUT_TOTAL.labels(mode=mode).inc()


def record_quota_rejected(level: str) -> None:
    """配额拒绝埋点

    Args:
        level: l1_rate（用户写速率）/ l2_daily（用户日配额）
    """
    QUOTA_REJECTED_TOTAL.labels(level=level).inc()


def record_security_event(event_type: str) -> None:
    """安全事件埋点

    Args:
        event_type: injection / keyword / auto_disable / ip_blacklist
    """
    SECURITY_EVENTS_TOTAL.labels(event_type=event_type).inc()
