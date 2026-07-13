"""HITL Manager — Redis 挂起 + 双模式唤醒（spec §8.3 / §8.4 / §8.4.1）

机制：
  1. Gateway Executor（Phase 3.2 接入）判定 mode=HITL 后：
     - operation_log_service.start_operation(status=PENDING_CONFIRMATION)
     - hitl_manager.create_pending(...) 写 Redis pending payload
     - emit confirmation_required SSE 事件
     - await hitl_manager.hang(confirmation_id)  # 阻塞直到 wake 或超时
  2. /ai/confirm endpoint 收到用户 approve/reject：
     - owner + conversation_id 校验
     - hitl_manager.wake(confirmation_id, action)
  3. hang 返回 action，Executor 据此 mark_running 或 mark_rejected

双模式（spec §8.4.1 v1.5+）：
  - memory（默认，MVP）：进程内 asyncio.Event 唤醒；强制单 worker。
    Redis pending payload 仍写（用于 owner 校验 + 重启清扫），但 wake 只走进程内 dict。
  - redis_pubsub（v1.5+）：Redis pub/sub 跨 worker 唤醒；允许多 worker / k8s 多 pod。
    wake 时 SET pending.wake_action + PUBLISH channel；hang 时 SUBSCRIBE channel +
    防丢失检查 wake_action（subscribe 前到达的 wake 不丢）。

为什么进程内 dict + Redis 双存（memory 模式）：
  - asyncio.Event 是进程内对象，多 worker 下其它进程拿不到（spec §8.4 强制单 worker）
  - Redis 存 pending payload 是为了：
      a) 服务重启后能清扫（spec §8.4 cleanup_pending_on_startup）
      b) /ai/confirm endpoint 跨请求取 pending 信息

为什么 pub/sub + wake_action 字段双写（redis_pubsub 模式，防丢失）：
  - 纯 pub/sub fire-and-forget，subscribe 前到达的 wake 消息丢失
  - wake 总是先 SET pending.wake_action 再 PUBLISH；hang subscribe 完成后
    立即 GET pending 检查 wake_action，已设则直接返回（race-safe）
  - 替代方案 LIST+BRPOP 在 SSE 取消时需 LREM 清理 LIST 残留，复杂度高

Args 4KB 限制（spec §8.3）：
  防恶意 user 把 hint 字段塞 1MB 撑爆 Redis。校验在 create_pending 入口。
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.hitl.constants import (
    AI_CONFIRM_REDIS_PREFIX,
    AI_HITL_WAKE_CHANNEL_PREFIX,
    ConfirmAction,
)

logger = logging.getLogger(__name__)


@dataclass
class _PendingEntry:
    """进程内挂起项，配合 Redis 中的 pending payload 使用

    Redis 是冷数据（跨请求 / 重启清扫用），_PendingEntry 是热数据（异步唤醒用）。
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    action: ConfirmAction | None = None
    created_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class PendingPayload:
    """Redis 中的 pending JSON 结构（spec §8.3 + §8.4.1 v1.5+）

    ID 字段按 DB 列原值存（int / str），与 Snowflake 序列化策略无关。
    expires_at 是 ISO 8601 UTC 字符串。

    wake_action（v1.5+ redis_pubsub 模式新增）：
      None = 尚未被 wake；"approved"/"rejected" = 已被 wake。
      hang 在 subscribe 完成后立即检查此字段，防 race 丢失。
    """

    user_id: int
    conversation_id: int
    tool_call_id: str
    trace_id: str
    tool_name: str
    args: dict[str, Any]
    dry_run_result: dict[str, Any] | None
    expires_at: str  # ISO 8601 UTC
    wake_action: str | None = None

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.__dict__, default=str).encode("utf-8")


class HitlManager:
    """HITL 挂起 / 唤醒 / 清扫

    单例风格（模块级实例 hitl_manager），与 user_service / chat_service 一致。
    """

    def __init__(self) -> None:
        # confirmation_id → _PendingEntry（仅本进程的挂起流）
        self._pending: dict[str, _PendingEntry] = {}

    # ============ ID 生成（spec §8.3） ============

    @staticmethod
    def generate_confirmation_id() -> str:
        """spec §8.3: secrets.token_urlsafe(32)，不可枚举"""
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_tool_call_id() -> str:
        """每次 tool 调用独立 ID（§4.4 tool_call_id 唯一索引）

        格式：tc_<32 hex chars>，前缀便于 grep / 日志识别
        """
        return f"tc_{secrets.token_hex(16)}"

    # ============ Args 大小校验（spec §8.3 4KB 限制） ============

    @staticmethod
    def validate_args_size(args: dict[str, Any]) -> None:
        """args JSON 序列化后必须 ≤ AI_HITL_ARGS_MAX_BYTES（默认 4096）

        超限抛 AI_HITL_ARGS_TOO_LARGE，调用方应回 LLM 友好错误让其引导用户精简。
        """
        body = json.dumps(args, default=str).encode("utf-8")
        if len(body) > settings.AI_HITL_ARGS_MAX_BYTES:
            raise BusinessRuleException(
                f"HITL args JSON 超过 {settings.AI_HITL_ARGS_MAX_BYTES} 字节限制，"
                f"请精简输入或拆分任务",
                error_code="AI_HITL_ARGS_TOO_LARGE",
            )

    # ============ 创建挂起（写 Redis + 注册 Event） ============

    async def create_pending(
        self,
        redis: Redis,
        *,
        confirmation_id: str,
        user_id: int,
        conversation_id: int,
        tool_call_id: str,
        trace_id: str,
        tool_name: str,
        args: dict[str, Any],
        dry_run_result: dict[str, Any] | None = None,
    ) -> PendingPayload:
        """创建挂起：写 Redis + 注册进程内 Event

        校验：
          - args 4KB 限制（spec §8.3）
          - confirmation_id 不重复（防御性，正常 token_urlsafe 不会撞）

        Args:
            redis: redis_client（caller 注入便于测试 mock）
            dry_run_result: dry_run 函数返回值（已 dict 化），HITL 抽屉展示影响范围用

        Returns:
            PendingPayload（caller 用于 SSE confirmation_required 事件）
        """
        # 1. args 4KB 校验
        self.validate_args_size(args)

        # 2. expires_at（UTC naive，与 DB TIMESTAMP WITHOUT TIME ZONE 一致）
        expires_at_dt = datetime.now(UTC) + timedelta(
            seconds=settings.AI_HITL_PENDING_TTL_SEC
        )
        payload = PendingPayload(
            user_id=user_id,
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            trace_id=trace_id,
            tool_name=tool_name,
            args=args,
            dry_run_result=dry_run_result,
            expires_at=expires_at_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        # 3. confirmation_id 不应重复（防御性，仅 memory 模式检查进程内 dict）
        if settings.AI_HITL_MODE == "memory" and confirmation_id in self._pending:
            logger.error(
                "confirmation_id collision (extremely unlikely): %s",
                confirmation_id,
            )
            raise BusinessRuleException(
                "confirmation_id 已存在",
                error_code="AI_HITL_CONFIRMATION_ID_COLLISION",
            )

        # 4. 写 Redis（带 TTL，过期自动清掉）
        key = self._redis_key(confirmation_id)
        body = payload.to_json_bytes()
        await redis.set(key, body, ex=settings.AI_HITL_PENDING_TTL_SEC)

        # 5. 注册进程内 Event（仅 memory 模式；redis_pubsub 模式 dict 不用）
        if settings.AI_HITL_MODE == "memory":
            self._pending[confirmation_id] = _PendingEntry()

        return payload

    # ============ 挂起等待唤醒（spec §8.3 hang） ============

    async def hang(
        self,
        confirmation_id: str,
        *,
        timeout_sec: int | None = None,
    ) -> ConfirmAction:
        """挂起当前协程直到 wake 或超时（spec §8.3，v1.5+ 双模式）

        Args:
            confirmation_id: 必须先 create_pending 注册过
            timeout_sec: 默认用 settings.AI_HITL_PENDING_TTL_SEC（5min）

        Returns:
            ConfirmAction.APPROVED | ConfirmAction.REJECTED

        超时抛 TimeoutError，调用方负责：
          - operation_log_service.mark_expired(log_id)
          - Redis key 已被 EXPIRE 自动清，无需手动 del
          - 清理 self._pending 残留 entry（memory 模式）

        mode 分支：
          - memory：进程内 asyncio.Event
          - redis_pubsub：Redis pub/sub + wake_action 防丢失
        """
        if timeout_sec is None:
            timeout_sec = settings.AI_HITL_PENDING_TTL_SEC

        if settings.AI_HITL_MODE == "redis_pubsub":
            return await self._hang_pubsub(confirmation_id, timeout_sec)
        return await self._hang_memory(confirmation_id, timeout_sec)

    async def _hang_memory(
        self,
        confirmation_id: str,
        timeout_sec: int,
    ) -> ConfirmAction:
        """memory 模式 hang：进程内 asyncio.Event（spec §8.3）"""
        from app.modules.ai.metrics import record_hitl_timeout  # noqa: PLC0415

        entry = self._pending.get(confirmation_id)
        if entry is None:
            # 没有 create_pending 就 hang？调用方错误
            raise BusinessRuleException(
                f"confirmation_id {confirmation_id!r} 未注册，无法挂起",
                error_code="AI_HITL_PENDING_NOT_FOUND",
            )

        try:
            await asyncio.wait_for(entry.event.wait(), timeout=timeout_sec)
        except TimeoutError:
            # 5min 无人确认 → EXPIRED
            self._pending.pop(confirmation_id, None)
            record_hitl_timeout("memory")
            raise

        # 被 wake 唤醒
        action = entry.action
        self._pending.pop(confirmation_id, None)
        if action is None:
            # 不该发生（wake 一定先写 action 再 set），防御性兜底
            logger.error(
                "hang woke but action is None (confirmation_id=%s)",
                confirmation_id,
            )
            raise BusinessRuleException(
                "HITL 唤醒但 action 缺失",
                error_code="AI_HITL_WAKE_WITHOUT_ACTION",
            )
        return action

    async def _hang_pubsub(
        self,
        confirmation_id: str,
        timeout_sec: int,
    ) -> ConfirmAction:
        """redis_pubsub 模式 hang：Redis pub/sub + wake_action 防丢失（§8.4.1 v1.5+）

        流程：
          1. SUBSCRIBE channel
          2. GET Redis pending — 若 wake_action 已设 → 直接返回（race-safe）
          3. listen for message（带 timeout）
          4. finally: unsubscribe + aclose

        防丢失关键：subscribe 完成后立即检查 wake_action。wake 总是 SET
        wake_action 再 PUBLISH，所以即使 PUBLISH 在 subscribe 之前到达（消息
        丢失），wake_action 仍在 Redis 中被本函数检测到。
        """
        from app.core.redis import redis_client  # noqa: PLC0415
        from app.modules.ai.metrics import (  # noqa: PLC0415
            record_hitl_pubsub_lost,
            record_hitl_timeout,
        )

        channel = f"{AI_HITL_WAKE_CHANNEL_PREFIX}:{confirmation_id}"
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        try:
            # 防丢失：subscribe 后立即检查 wake_action（race-safe）
            pending = await self.get_pending(redis_client, confirmation_id)
            if pending is not None and pending.wake_action is not None:
                # spec §6.3 metric：防丢失分支命中（PUBLISH 在 SUBSCRIBE 之前
                # 到达，靠 wake_action 兜底）。redis_pubsub 模式健康度核心指标。
                record_hitl_pubsub_lost()
                return ConfirmAction(pending.wake_action)

            deadline = time.monotonic() + timeout_sec
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record_hitl_timeout("redis_pubsub")
                    raise TimeoutError()
                message = await asyncio.wait_for(
                    pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=remaining,
                    ),
                    timeout=remaining,
                )
                if message is None:
                    continue
                if message.get("type") == "message":
                    data = json.loads(message["data"])
                    return ConfirmAction(data["action"])
        finally:
            try:
                await pubsub.unsubscribe(channel)
            finally:
                await pubsub.aclose()

    # ============ 唤醒（/ai/confirm endpoint 调用） ============

    async def wake(
        self,
        confirmation_id: str,
        action: ConfirmAction,
    ) -> bool:
        """唤醒挂起的协程（spec §8.3 + §8.4.1 v1.5+ 双模式）

        Args:
            confirmation_id: pending 流的 ID
            action: APPROVED / REJECTED

        Returns:
            True = 唤醒成功；False = 该 confirmation_id 不可达
                   （可能：跨进程（memory 模式）/ 已 expired / 还没
                   create_pending / 已被另一个并发 wake 唤醒过）

        mode 分支：
          - memory：进程内 dict pop + Event.set（修订 S-14 防双击）
          - redis_pubsub：SET pending.wake_action + PUBLISH channel
        """
        if settings.AI_HITL_MODE == "redis_pubsub":
            return await self._wake_pubsub(confirmation_id, action)
        return await self._wake_memory(confirmation_id, action)

    async def _wake_memory(
        self,
        confirmation_id: str,
        action: ConfirmAction,
    ) -> bool:
        """memory 模式 wake：进程内 dict pop + Event.set（修订 S-14 防双击 race）

        立即 pop entry（其它 wake 看不到）→ set action → event.set() → True。
        第二次 wake（双击 / 双标签）：pop 返回 None → False。

        spec §8.3: wake 写回 Redis 不必要，Redis 只用于跨请求取 pending payload。
        进程内 Event 是同步唤醒机制。
        """
        from app.modules.ai.metrics import record_hitl_wake  # noqa: PLC0415

        # 修订 S-14：立即 pop，防双击 race
        entry = self._pending.pop(confirmation_id, None)
        if entry is None:
            record_hitl_wake("memory", "not_found")
            return False
        # 防御性：极端 race 下 entry 已有 action（不该发生，pop 已原子）
        if entry.action is not None:
            logger.warning(
                "wake: entry already has action (extreme race) confirmation_id=%s",
                confirmation_id,
            )
            record_hitl_wake("memory", "not_found")
            return False
        entry.action = action
        entry.event.set()
        record_hitl_wake("memory", "success")
        return True

    async def _wake_pubsub(
        self,
        confirmation_id: str,
        action: ConfirmAction,
    ) -> bool:
        """redis_pubsub 模式 wake：SET pending.wake_action + PUBLISH（§8.4.1 v1.5+）

        流程：
          1. GET Redis pending；不存在 → False（已 expired / 未 create_pending）
          2. dataclasses.replace 重写 wake_action（frozen 兼容）
          3. SET Redis payload（保留 TTL）
          4. PUBLISH channel: {"action": "...", "confirmation_id": "...", "ts": ...}

        防双击 / 防丢失：
          - 第二次 wake 同 confirmation_id：step 1 检查到 wake_action 已设，
            但仍覆盖 SET + PUBLISH（无害，hang 端只读一次）。考虑过在这里 short
            -circuit 返回 False，但实际场景下：第二次 wake 通常意味着第一次没
            成功送达（如 SSE 流已断 + 用户重试），保守返回 True 让 confirm.py
            走"等 stream 自然处理"路径更安全。
          - 防丢失：见 _hang_pubsub 注释。
        """
        from app.core.redis import redis_client  # noqa: PLC0415
        from app.modules.ai.metrics import record_hitl_wake  # noqa: PLC0415

        pending = await self.get_pending(redis_client, confirmation_id)
        if pending is None:
            record_hitl_wake("redis_pubsub", "not_found")
            return False

        # 用 dataclasses.replace 重写 wake_action（PendingPayload frozen=True）
        updated = replace(pending, wake_action=action.value)
        await redis_client.set(
            self._redis_key(confirmation_id),
            updated.to_json_bytes(),
            ex=settings.AI_HITL_PENDING_TTL_SEC,
        )

        channel = f"{AI_HITL_WAKE_CHANNEL_PREFIX}:{confirmation_id}"
        await redis_client.publish(
            channel,
            json.dumps(
                {
                    "action": action.value,
                    "confirmation_id": confirmation_id,
                    "ts": time.time(),
                }
            ),
        )
        record_hitl_wake("redis_pubsub", "success")
        return True

    # ============ Redis 查询（/ai/confirm endpoint 用） ============

    @staticmethod
    async def get_pending(
        redis: Redis,
        confirmation_id: str,
    ) -> PendingPayload | None:
        """从 Redis 取 pending payload，不存在返回 None

        /ai/confirm endpoint 用：
          - 取 pending → 校验 owner + conversation_id → wake
        """
        key = HitlManager._redis_key(confirmation_id)
        body = await redis.get(key)
        if body is None:
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        data = json.loads(body)
        return PendingPayload(**data)

    @staticmethod
    async def delete_pending(
        redis: Redis,
        confirmation_id: str,
    ) -> None:
        """显式删 Redis pending（mark_rejected / mark_expired 时调）"""
        await redis.delete(HitlManager._redis_key(confirmation_id))

    # ============ 服务重启清扫（spec §8.4） ============

    async def cleanup_pending_on_startup(self) -> int:
        """服务重启时清扫 Redis 残留 pending（spec §8.4）

        服务重启 = 所有挂起的 SSE 流已断，进程内 Event 已丢，
        Redis 里的 pending 成了"孤儿"，必须清扫避免：
          a) 用户后续 /ai/confirm 时取到 stale pending（owner 校验虽然能挡，
             但 Redis 内存浪费）
          b) ai_operation_log 残留 pending_confirmation 状态（审计不准）

        本方法只清 Redis；DB 中的 ai_operation_log 状态由调用方（lifespan）
        配合 operation_log_service.mark_expired 清扫。

        Redis 故障容错（修订 S-14）：
          - 启动时 Redis 短暂不可用，scan_iter / delete 抛 RedisError
          - 不阻断 lifespan（应用仍能启动；Redis 恢复后 stale pending 由
            Redis TTL 5min 自然清掉；DB 端 lifespan 调用方应额外跑一次
            mark_expired WHERE status='pending_confirmation' AND started_at < now()-5min）
          - 仅 log warning + 返回 0

        Returns:
            清扫的 pending 数量（Redis 故障时返回 0）
        """
        # 局部 import 拿当前模块引用（fixture 重建 redis_client 后能拿到新引用）
        from redis.exceptions import RedisError  # noqa: PLC0415

        from app.core.redis import redis_client  # noqa: PLC0415

        cleaned = 0
        pattern = f"{AI_CONFIRM_REDIS_PREFIX}:*"
        try:
            async for key in redis_client.scan_iter(match=pattern, count=100):
                cleaned += 1
                await redis_client.delete(key)
            if cleaned:
                logger.warning(
                    "startup cleanup: removed %d stale HITL pending entries from Redis",
                    cleaned,
                )
        except RedisError:
            logger.warning(
                "startup cleanup: Redis unavailable, skipped; stale pending will "
                "expire via 5min TTL. Run DB-side mark_expired backfill separately.",
                exc_info=True,
            )
            return 0
        return cleaned

    # ============ 测试辅助 ============

    def _reset_for_test(self) -> None:
        """测试间清空进程内挂起表（生产代码不要调）"""
        self._pending.clear()

    def _has_pending(self, confirmation_id: str) -> bool:
        """测试断言用"""
        return confirmation_id in self._pending

    # ============ 私有 ============

    @staticmethod
    def _redis_key(confirmation_id: str) -> str:
        return f"{AI_CONFIRM_REDIS_PREFIX}:{confirmation_id}"


hitl_manager = HitlManager()
