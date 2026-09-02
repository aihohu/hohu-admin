"""HITL Manager — Redis 挂起 + 双模式唤醒。

机制：
  1. Gateway Executor 判定 mode=HITL 后：
     - operation_log_service.start_operation(status=PENDING_CONFIRMATION)
     - hitl_manager.create_pending(...) 写 Redis pending payload
     - emit confirmation_required SSE 事件
     - await hitl_manager.hang(confirmation_id)  # 阻塞直到 wake 或超时
  2. /ai/confirm endpoint 收到用户 approve/reject：
     - owner + conversation_id 校验
     - hitl_manager.wake(confirmation_id, action)
  3. hang 返回 action，Executor 据此 mark_running 或 mark_rejected

双模式：
  - memory（默认）：进程内 asyncio.Event 唤醒；强制单 worker。
    Redis pending payload 仍写（用于 owner 校验 + 重启清扫），但 wake 只走进程内 dict。
  - redis_pubsub：Redis pub/sub 跨 worker 唤醒；允许多 worker / k8s 多 pod。
    wake 时用 tenant-scoped side-state Lua CAS 原子认领决定并 PUBLISH channel；
    hang 时 SUBSCRIBE channel + 防丢失检查 side-state（subscribe 前到达的 wake 不丢）。

为什么进程内 dict + Redis 双存（memory 模式）：
  - asyncio.Event 是进程内对象，多 worker 下其它进程拿不到
  - Redis 存 pending payload 是为了：
      a) 服务重启后能清扫
      b) /ai/confirm endpoint 跨请求取 pending 信息

为什么 pub/sub + tenant-scoped side-state 双写（redis_pubsub 模式，防丢失）：
  - 纯 pub/sub fire-and-forget，subscribe 前到达的 wake 消息丢失
  - wake 用同一段 Lua first-writer-wins 地写 side-state 并发布；hang subscribe 完成后
    立即读取 side-state，已设则直接返回（race-safe）
  - rolling upgrade 期间仍识别旧 pending payload 内嵌的 wake_action
  - 替代方案 LIST+BRPOP 在 SSE 取消时需 LREM 清理 LIST 残留，复杂度高

Args 4KB 限制：
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
from app.core.tenant import PlatformContext, TenantContext
from app.modules.ai.agents.hitl.constants import (
    AI_CONFIRM_REDIS_PREFIX,
    AI_HITL_WAKE_CHANNEL_PREFIX,
    ConfirmAction,
)

logger = logging.getLogger(__name__)

AI_HITL_WAKE_STATE_PREFIX = "ai:hitl:wake-state:v1"

_CREATE_PENDING_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
redis.call('DEL', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

_CLAIM_AND_PUBLISH_WAKE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
local ttl_ms = redis.call('PTTL', KEYS[1])
if ttl_ms <= 0 then
    return 0
end
local payload = redis.call('GET', KEYS[1])
if string.find(payload, '"wake_action"%s*:%s*"approved"')
    or string.find(payload, '"wake_action"%s*:%s*"rejected"') then
    return 2
end
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 2
end
local claimed = redis.call('SET', KEYS[2], ARGV[1], 'PX', ttl_ms, 'NX')
if not claimed then
    return 2
end
local subscribers = redis.call('PUBLISH', KEYS[3], ARGV[2])
if subscribers > 0 then
    return 1
end
return -1
"""


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
    """Redis 中的 pending JSON 结构。

    ID 字段按 DB 列原值存（int / str），与 Snowflake 序列化策略无关。
    expires_at 是 ISO 8601 UTC 字符串。

    wake_action：
      None = 尚未被 wake；"approved"/"rejected" = 已被 wake。
      hang 在 subscribe 完成后立即检查此字段，防 race 丢失。
    """

    user_id: int
    tenant_id: int
    conversation_id: int
    tool_call_id: str
    trace_id: str
    tool_name: str
    args: dict[str, Any]
    dry_run_result: dict[str, Any] | None
    expires_at: str  # ISO 8601 UTC
    """创建 pending 时的可信租户快照。"""
    source_user_message_id: int | None = None
    guard_owner_token: str | None = None
    command_action: str = "send"
    agent_code: str | None = None
    risk_level: str = "high"
    chip_target: str | None = None
    action_id: int | None = None
    """Durable PostgreSQL action binding; None only for legacy Redis-only payloads."""
    wake_action: str | None = None

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.__dict__, default=str).encode("utf-8")


class HitlManager:
    """HITL 挂起 / 唤醒 / 清扫

    单例风格（模块级实例 hitl_manager），与 user_service / chat_service 一致。
    """

    def __init__(self) -> None:
        # (tenant_id, confirmation_id) → _PendingEntry（仅本进程的挂起流）
        self._pending: dict[tuple[int, str], _PendingEntry] = {}

    # ============ ID 生成 ============

    @staticmethod
    def generate_confirmation_id() -> str:
        """生成不可枚举的确认 ID。"""
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_tool_call_id() -> str:
        """生成与操作日志唯一索引匹配的工具调用 ID。

        格式：tc_<32 hex chars>，前缀便于 grep / 日志识别
        """
        return f"tc_{secrets.token_hex(16)}"

    # ============ Args 大小校验 ============

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
        tenant: TenantContext,
        conversation_id: int,
        tool_call_id: str,
        trace_id: str,
        tool_name: str,
        args: dict[str, Any],
        dry_run_result: dict[str, Any] | None = None,
        source_user_message_id: int | None = None,
        guard_owner_token: str | None = None,
        command_action: str = "send",
        agent_code: str | None = None,
        risk_level: str = "high",
        chip_target: str | None = None,
    ) -> PendingPayload:
        """创建挂起：写 Redis + 注册进程内 Event

        校验：
          - args 4KB 限制
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
            tenant_id=tenant.tenant_id,
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            trace_id=trace_id,
            tool_name=tool_name,
            args=args,
            dry_run_result=dry_run_result,
            expires_at=expires_at_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_user_message_id=source_user_message_id,
            guard_owner_token=guard_owner_token,
            command_action=command_action,
            agent_code=agent_code,
            risk_level=risk_level,
            chip_target=chip_target,
        )

        memory_key = self._memory_key(confirmation_id, tenant=tenant)
        # 3. confirmation_id 在同一租户内不应重复；不同租户必须相互独立。
        if settings.AI_HITL_MODE == "memory" and memory_key in self._pending:
            logger.error(
                "confirmation_id collision (extremely unlikely): %s",
                confirmation_id,
            )
            raise BusinessRuleException(
                "confirmation_id 已存在",
                error_code="AI_HITL_CONFIRMATION_ID_COLLISION",
            )

        # 4. 原子创建 Redis pending 并清掉同 business key 的旧 wake state。
        # SET-if-absent protects pub/sub workers from a generated-ID collision.
        key = self._redis_key(confirmation_id, tenant=tenant)
        body = payload.to_json_bytes()
        created = await redis.eval(
            _CREATE_PENDING_LUA,
            2,
            key,
            self._wake_state_key(confirmation_id, tenant=tenant),
            body,
            settings.AI_HITL_PENDING_TTL_SEC,
        )
        if not created:
            raise BusinessRuleException(
                "confirmation_id 已存在",
                error_code="AI_HITL_CONFIRMATION_ID_COLLISION",
            )

        # 5. 注册进程内 Event（仅 memory 模式；redis_pubsub 模式 dict 不用）
        if settings.AI_HITL_MODE == "memory":
            self._pending[memory_key] = _PendingEntry()

        return payload

    # ============ 挂起等待唤醒 ============

    async def hang(
        self,
        confirmation_id: str,
        *,
        tenant: TenantContext,
        timeout_sec: int | None = None,
    ) -> ConfirmAction:
        """按当前模式挂起协程，直到 wake 或超时。

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
            return await self._hang_pubsub(confirmation_id, timeout_sec, tenant=tenant)
        return await self._hang_memory(confirmation_id, timeout_sec, tenant=tenant)

    async def _hang_memory(
        self,
        confirmation_id: str,
        timeout_sec: int,
        *,
        tenant: TenantContext,
    ) -> ConfirmAction:
        """memory 模式：等待进程内 asyncio.Event。"""
        from app.modules.ai.metrics import record_hitl_timeout  # noqa: PLC0415

        memory_key = self._memory_key(confirmation_id, tenant=tenant)
        entry = self._pending.get(memory_key)
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
            if self._pending.get(memory_key) is entry:
                self._pending.pop(memory_key, None)
            record_hitl_timeout("memory")
            raise

        # 被 wake 唤醒
        action = entry.action
        if self._pending.get(memory_key) is entry:
            self._pending.pop(memory_key, None)
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
        *,
        tenant: TenantContext,
    ) -> ConfirmAction:
        """redis_pubsub 模式：使用 pub/sub，并用 wake_action 防止订阅前消息丢失。

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

        channel = self._wake_channel(confirmation_id, tenant=tenant)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        try:
            # 防丢失：subscribe 后立即检查 wake_action（race-safe）
            pending = await self.get_pending(
                redis_client, confirmation_id, tenant=tenant
            )
            if pending is not None and pending.wake_action is not None:
                # 记录订阅前已写入 wake_action 的防丢失分支。
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
        *,
        tenant: TenantContext,
    ) -> bool:
        """按当前模式唤醒挂起协程。

        Args:
            confirmation_id: pending 流的 ID
            action: APPROVED / REJECTED

        Returns:
            True = 唤醒成功；False = 该 confirmation_id 不可达
                   （可能：跨进程（memory 模式）/ 已 expired / 还没
                   create_pending / 已被另一个并发 wake 唤醒过）

        mode 分支：
          - memory：原子写入首个 action 并 Event.set，由 hang 消费后移除
          - redis_pubsub：Lua CAS side-state + PUBLISH channel
        """
        if settings.AI_HITL_MODE == "redis_pubsub":
            return await self._wake_pubsub(confirmation_id, action, tenant=tenant)
        return await self._wake_memory(confirmation_id, action, tenant=tenant)

    async def _wake_memory(
        self,
        confirmation_id: str,
        action: ConfirmAction,
        *,
        tenant: TenantContext,
    ) -> bool:
        """memory 模式：原子认领决定后设置 Event，防止双击竞争。

        entry remains reachable until hang consumes it, so a confirmation that
        arrives between create_pending and hang cannot be lost.  There is no
        await between the action check and assignment, so one event-loop worker
        observes a first-writer-wins transition.

        memory 模式下 Redis 只保存跨请求所需 payload，无需写回 wake 状态。
        进程内 Event 是同步唤醒机制。
        """
        from app.modules.ai.metrics import record_hitl_wake  # noqa: PLC0415

        memory_key = self._memory_key(confirmation_id, tenant=tenant)
        entry = self._pending.get(memory_key)
        if entry is None:
            record_hitl_wake("memory", "not_found")
            return False
        # Double taps and conflicting decisions cannot overwrite the winner.
        if entry.action is not None:
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
        *,
        tenant: TenantContext,
    ) -> bool:
        """redis_pubsub 模式：原子认领 wake_action，再发布唤醒消息。

        流程：
          1. Lua 检查 tenant-scoped pending 存在且仍有 TTL
          2. 兼容检查旧 payload 内嵌 wake_action
          3. 以 pending 剩余 TTL 对 side-state 执行 SET NX
          4. 首次认领者在同一 Lua 内 PUBLISH channel

        防双击 / 防丢失：
          - 第二次 wake 不能覆盖首个决定；作为幂等重试返回 True，但不会重复发布。
          - 防丢失：见 _hang_pubsub 注释。
        """
        from app.core.redis import redis_client  # noqa: PLC0415
        from app.modules.ai.metrics import record_hitl_wake  # noqa: PLC0415

        channel = self._wake_channel(confirmation_id, tenant=tenant)
        message = json.dumps(
            {
                "action": action.value,
                "confirmation_id": confirmation_id,
                "ts": time.time(),
            }
        )
        result = int(
            await redis_client.eval(
                _CLAIM_AND_PUBLISH_WAKE_LUA,
                3,
                self._redis_key(confirmation_id, tenant=tenant),
                self._wake_state_key(confirmation_id, tenant=tenant),
                channel,
                action.value,
                message,
            )
        )
        # 1: claimed with a live subscriber; 2: already resolved (idempotent).
        reachable = result in {1, 2}
        record_hitl_wake("redis_pubsub", "success" if reachable else "not_found")
        return reachable

    # ============ Redis 查询（/ai/confirm endpoint 用） ============

    @staticmethod
    async def get_pending(
        redis: Redis,
        confirmation_id: str,
        *,
        tenant: TenantContext,
    ) -> PendingPayload | None:
        """从 Redis 取 pending payload，不存在返回 None

        /ai/confirm endpoint 用：
          - 取 pending → 校验 owner + conversation_id → wake
        """
        key = HitlManager._redis_key(confirmation_id, tenant=tenant)
        body = await redis.get(key)
        if body is None:
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        try:
            data = json.loads(body)
            pending = PendingPayload(**data)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "invalid HITL pending payload confirmation_id=%s",
                confirmation_id,
            )
            return None
        if pending.tenant_id != tenant.tenant_id:
            return None
        if settings.AI_HITL_MODE == "redis_pubsub":
            wake_action = await redis.get(
                HitlManager._wake_state_key(confirmation_id, tenant=tenant)
            )
            if isinstance(wake_action, bytes):
                wake_action = wake_action.decode("utf-8")
            if wake_action is not None:
                if wake_action not in {item.value for item in ConfirmAction}:
                    return None
                pending = replace(pending, wake_action=wake_action)
        return pending

    @staticmethod
    async def get_pending_for_platform(
        redis: Redis,
        confirmation_id: str,
        *,
        tenant_id: int,
        platform: PlatformContext,
    ) -> PendingPayload | None:
        """Read one tenant envelope only for platform lifecycle recovery."""
        if not isinstance(platform, PlatformContext):
            raise TypeError("platform context is required")
        body = await redis.get(
            HitlManager._redis_key_for_id(confirmation_id, tenant_id=tenant_id)
        )
        if body is None:
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        try:
            pending = PendingPayload(**json.loads(body))
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "invalid platform HITL pending payload confirmation_id=%s",
                confirmation_id,
            )
            return None
        return pending if pending.tenant_id == tenant_id else None

    @staticmethod
    async def ttl(redis: Redis, confirmation_id: str, *, tenant: TenantContext) -> int:
        """返回 pending 剩余 TTL；续传接口会拒绝剩余不足 60 秒的确认。

        /ai/chat/resume endpoint 用：剩余 < 60s → 422 AI_RESUME_TTL_TOO_SHORT。
        Redis key 不存在时返回 -2（Redis 标准）。
        """
        return await redis.ttl(HitlManager._redis_key(confirmation_id, tenant=tenant))

    @staticmethod
    async def bind_durable_action(
        redis: Redis,
        confirmation_id: str,
        action_id: int,
        *,
        tenant: TenantContext,
    ) -> PendingPayload:
        """Bind a newly committed action before its confirmation is exposed."""
        pending = await HitlManager.get_pending(redis, confirmation_id, tenant=tenant)
        if pending is None:
            raise BusinessRuleException(
                "HITL pending 在绑定 action 前已丢失",
                error_code="AI_PREPARED_ACTION_BINDING_INVALID",
            )
        ttl_sec = await redis.ttl(
            HitlManager._redis_key(confirmation_id, tenant=tenant)
        )
        if ttl_sec <= 0:
            raise BusinessRuleException(
                "HITL pending 在绑定 action 前已过期",
                error_code="AI_PREPARED_ACTION_BINDING_INVALID",
            )
        bound = replace(pending, action_id=action_id)
        updated = await redis.set(
            HitlManager._redis_key(confirmation_id, tenant=tenant),
            bound.to_json_bytes(),
            ex=ttl_sec,
            xx=True,
        )
        if not updated:
            raise BusinessRuleException(
                "HITL pending action 绑定失败",
                error_code="AI_PREPARED_ACTION_BINDING_INVALID",
            )
        return bound

    @staticmethod
    async def delete_pending(
        redis: Redis,
        confirmation_id: str,
        *,
        tenant: TenantContext,
    ) -> None:
        """显式删 Redis pending（mark_rejected / mark_expired 时调）"""
        await redis.delete(
            HitlManager._redis_key(confirmation_id, tenant=tenant),
            HitlManager._wake_state_key(confirmation_id, tenant=tenant),
        )

    # ============ 服务重启清扫 ============

    async def cleanup_pending_on_startup(self) -> int:
        """服务重启时清扫 Redis 中失去流所有者的 pending。

        服务重启 = 所有挂起的 SSE 流已断，进程内 Event 已丢，
        Redis 里的 pending 成了"孤儿"，必须清扫避免：
          a) 用户后续 /ai/confirm 时取到 stale pending（owner 校验虽然能挡，
             但 Redis 内存浪费）
          b) ai_operation_log 残留 pending_confirmation 状态（审计不准）

        本方法只清 Redis；DB 中的 ai_operation_log 状态由调用方（lifespan）
        配合 operation_log_service.mark_expired 清扫。

        Redis 故障容错：
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
        pattern = f"{AI_CONFIRM_REDIS_PREFIX}:tenant:*"
        try:
            async for key in redis_client.scan_iter(match=pattern, count=100):
                cleaned += 1
                key_text = key.decode() if isinstance(key, bytes) else str(key)
                await redis_client.delete(key)
                try:
                    confirmation_id = key_text.rsplit(":", 1)[-1]
                    tenant_id = int(key_text.rsplit(":", 2)[-2])
                    if tenant_id < 0:
                        raise ValueError("negative tenant ID")
                except (IndexError, ValueError):
                    logger.warning("startup cleanup skipped malformed HITL key")
                    continue
                await redis_client.delete(
                    self._wake_state_key_for_id(confirmation_id, tenant_id=tenant_id)
                )
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

    def _has_pending(
        self,
        confirmation_id: str,
        *,
        tenant: TenantContext | None = None,
    ) -> bool:
        """测试断言用"""
        if tenant is not None:
            return self._memory_key(confirmation_id, tenant=tenant) in self._pending
        return any(key[1] == confirmation_id for key in self._pending)

    # ============ 私有 ============

    @staticmethod
    def _redis_key(confirmation_id: str, *, tenant: TenantContext) -> str:
        return HitlManager._redis_key_for_id(
            confirmation_id, tenant_id=tenant.tenant_id
        )

    @staticmethod
    def _redis_key_for_id(confirmation_id: str, *, tenant_id: int) -> str:
        return f"{AI_CONFIRM_REDIS_PREFIX}:tenant:{tenant_id}:{confirmation_id}"

    @staticmethod
    def _memory_key(confirmation_id: str, *, tenant: TenantContext) -> tuple[int, str]:
        return tenant.tenant_id, confirmation_id

    @staticmethod
    def _wake_state_key(confirmation_id: str, *, tenant: TenantContext) -> str:
        return HitlManager._wake_state_key_for_id(
            confirmation_id, tenant_id=tenant.tenant_id
        )

    @staticmethod
    def _wake_state_key_for_id(confirmation_id: str, *, tenant_id: int) -> str:
        return f"{AI_HITL_WAKE_STATE_PREFIX}:tenant:{tenant_id}:{confirmation_id}"

    @staticmethod
    def _wake_channel(confirmation_id: str, *, tenant: TenantContext) -> str:
        return (
            f"{AI_HITL_WAKE_CHANNEL_PREFIX}:tenant:{tenant.tenant_id}:{confirmation_id}"
        )


hitl_manager = HitlManager()
