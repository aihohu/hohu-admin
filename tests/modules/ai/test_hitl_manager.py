"""HITL Manager 单元测试 — spec §8.3 / §8.4

覆盖：
  - generate_confirmation_id / generate_tool_call_id 格式
  - validate_args_size 4KB 边界
  - create_pending 写 Redis + 注册 Event
  - hang/wake 并发唤醒
  - hang 超时（5min TTL）
  - get_pending / delete_pending
  - cleanup_pending_on_startup 清扫残留
"""

# ruff: noqa: ARG001, ARG002, PLC0415

import asyncio
import json

import pytest
import redis.asyncio as aioredis

from app.core import redis as redis_module
from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.manager import (
    AI_CONFIRM_REDIS_PREFIX,
    PendingPayload,
    hitl_manager,
)


@pytest.fixture(autouse=True)
async def clean_redis_hitl(monkeypatch):
    """每个测试重建 redis_client（绑新 loop）+ 清 ai:confirm:* keys + reset manager

    强制 memory 模式：本文件覆盖的是 memory 模式语义（_pending dict pop / Event.set /
    双击 race）。生产 .env 切 redis_pubsub 后这些断言失效，故 monkeypatch 锁定 memory。
    redis_pubsub 路径由 test_resume.py 覆盖（resume 端点集成测试）。
    """
    monkeypatch.setattr(settings, "AI_HITL_MODE", "memory")
    original_pool = redis_module.redis_pool
    original_client = redis_module.redis_client

    redis_module.redis_pool = aioredis.ConnectionPool.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    redis_module.redis_client = aioredis.Redis(connection_pool=redis_module.redis_pool)

    keys = await redis_module.redis_client.keys(f"{AI_CONFIRM_REDIS_PREFIX}:*")
    if keys:
        await redis_module.redis_client.delete(*keys)
    hitl_manager._reset_for_test()

    yield

    keys = await redis_module.redis_client.keys(f"{AI_CONFIRM_REDIS_PREFIX}:*")
    if keys:
        await redis_module.redis_client.delete(*keys)
    hitl_manager._reset_for_test()

    redis_module.redis_pool = original_pool
    redis_module.redis_client = original_client


# ============ ID 生成 ============


class TestGenerateIds:
    def test_confirmation_id_format(self) -> None:
        """secrets.token_urlsafe(32) ~43 字符 URL-safe base64"""
        cid = hitl_manager.generate_confirmation_id()
        assert len(cid) >= 32
        # URL-safe 字符
        assert all(c.isalnum() or c in "-_" for c in cid)

    def test_confirmation_id_unique(self) -> None:
        ids = {hitl_manager.generate_confirmation_id() for _ in range(100)}
        assert len(ids) == 100  # 无碰撞

    def test_tool_call_id_format(self) -> None:
        tcid = hitl_manager.generate_tool_call_id()
        assert tcid.startswith("tc_")
        # hex 部分 32 字符
        assert len(tcid) == 3 + 32

    def test_tool_call_id_unique(self) -> None:
        ids = {hitl_manager.generate_tool_call_id() for _ in range(100)}
        assert len(ids) == 100


# ============ Args 4KB 限制 ============


class TestValidateArgsSize:
    def test_small_args_passes(self) -> None:
        hitl_manager.validate_args_size({"user_id": 42, "reason": "x"})

    def test_empty_args_passes(self) -> None:
        hitl_manager.validate_args_size({})

    def test_oversized_args_raises(self) -> None:
        """spec §8.3: args JSON 超 4KB 拒绝（防恶意 user 撑爆 Redis）"""
        big_str = "x" * (settings.AI_HITL_ARGS_MAX_BYTES + 100)
        with pytest.raises(BusinessRuleException) as exc_info:
            hitl_manager.validate_args_size({"hint": big_str})
        assert exc_info.value.error_code == "AI_HITL_ARGS_TOO_LARGE"

    def test_exactly_at_limit_passes(self) -> None:
        """边界值：恰好 ≤ 限制通过"""
        # {"k": "xxxxx..."} 总长 = JSON 包装 ~12 字节 + value 长度
        target = settings.AI_HITL_ARGS_MAX_BYTES - 12
        hitl_manager.validate_args_size({"k": "x" * target})


# ============ create_pending ============


class TestCreatePending:
    async def test_writes_redis_and_registers_event(self) -> None:
        cid = hitl_manager.generate_confirmation_id()
        payload = await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=9001,
            tenant_id=37,
            conversation_id=100,
            tool_call_id="tc_1",
            trace_id="tr_1",
            tool_name="user.update_dept",
            args={"user_id": 42, "new_dept_id": 8},
            dry_run_result={"count": 1},
        )
        # 返回 payload
        assert isinstance(payload, PendingPayload)
        assert payload.user_id == 9001
        assert payload.tenant_id == 37
        assert payload.tool_call_id == "tc_1"
        assert payload.expires_at.endswith("Z")

        # Redis 已写
        body = await redis_module.redis_client.get(f"{AI_CONFIRM_REDIS_PREFIX}:{cid}")
        assert body is not None
        data = json.loads(body)
        assert data["user_id"] == 9001
        assert data["tenant_id"] == 37
        assert data["args"] == {"user_id": 42, "new_dept_id": 8}

        # 进程内 Event 已注册
        assert hitl_manager._has_pending(cid)

    async def test_binds_durable_action_without_losing_pending_payload(self) -> None:
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=9001,
            tenant_id=37,
            conversation_id=100,
            tool_call_id="tc_bound",
            trace_id="tr_bound",
            tool_name="user.update_dept",
            args={"user_id": 42},
        )

        bound = await hitl_manager.bind_durable_action(
            redis_module.redis_client, cid, 7483433649145122816
        )
        restored = await hitl_manager.get_pending(redis_module.redis_client, cid)

        assert bound.action_id == 7483433649145122816
        assert restored is not None
        assert restored.action_id == 7483433649145122816
        assert restored.tool_call_id == "tc_bound"

    async def test_oversized_args_rejected(self) -> None:
        """create_pending 入口必须先校验 args 大小"""
        cid = hitl_manager.generate_confirmation_id()
        with pytest.raises(BusinessRuleException) as exc_info:
            await hitl_manager.create_pending(
                redis_module.redis_client,
                confirmation_id=cid,
                user_id=1,
                conversation_id=1,
                tool_call_id="tc_big",
                trace_id="tr",
                tool_name="x",
                args={"hint": "x" * (settings.AI_HITL_ARGS_MAX_BYTES + 10)},
            )
        assert exc_info.value.error_code == "AI_HITL_ARGS_TOO_LARGE"
        # 不应残留
        assert not hitl_manager._has_pending(cid)


# ============ hang / wake ============


class TestHangWake:
    async def test_wake_with_approved(self) -> None:
        """spec §8.3: hang 被 wake 唤醒后返回 APPROVED"""
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_a",
            trace_id="tr",
            tool_name="x",
            args={"a": 1},
        )

        # 并发：先启 hang task，再 wake
        async def wake_later():
            await asyncio.sleep(0.05)  # 让 hang 先 await event.wait()
            ok = await hitl_manager.wake(cid, ConfirmAction.APPROVED)
            assert ok

        async def hang():
            return await hitl_manager.hang(cid, timeout_sec=2)

        wake_task = asyncio.create_task(wake_later())
        result = await hang()
        await wake_task

        assert result == ConfirmAction.APPROVED
        # hang 完应清理 entry
        assert not hitl_manager._has_pending(cid)

    async def test_wake_with_rejected(self) -> None:
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_r",
            trace_id="tr",
            tool_name="x",
            args={},
        )

        async def wake_later():
            await asyncio.sleep(0.05)
            await hitl_manager.wake(cid, ConfirmAction.REJECTED)

        wake_task = asyncio.create_task(wake_later())
        result = await hitl_manager.hang(cid, timeout_sec=2)
        await wake_task

        assert result == ConfirmAction.REJECTED

    async def test_hang_timeout(self) -> None:
        """spec §8.3: 5min TTL 无人确认 → EXPIRED（抛 TimeoutError）"""
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_t",
            trace_id="tr",
            tool_name="x",
            args={},
        )

        with pytest.raises(asyncio.TimeoutError):
            await hitl_manager.hang(cid, timeout_sec=0.1)

        # 超时后应清理 entry
        assert not hitl_manager._has_pending(cid)

    async def test_wake_unknown_returns_false(self) -> None:
        """不存在的 confirmation_id → False（stream 已断 / 跨进程）"""
        ok = await hitl_manager.wake("nonexistent", ConfirmAction.APPROVED)
        assert ok is False

    async def test_wake_double_tap_returns_false_on_second(self) -> None:
        """修订 S-14：防双击 race — 第一次 wake 成功 pop，第二次找不到 entry 返回 False

        场景：用户双击 / 双标签确认同一 confirmation_id。
        修订前：第二次 wake 仍能 get entry（已设 action + event.set() 幂等），
        但端点会返回 200+queued 误导前端。
        修订后：第一次 wake 立即 pop entry，第二次 wake 找不到返回 False →
        端点返回 status="stream_gone"。
        """
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_double",
            trace_id="tr",
            tool_name="x",
            args={},
        )

        # 第一次 wake（无 hang 等待，但 pop 应成功）
        ok1 = await hitl_manager.wake(cid, ConfirmAction.APPROVED)
        assert ok1 is True

        # 第二次 wake：entry 已被 pop，找不到 → False（防双击）
        ok2 = await hitl_manager.wake(cid, ConfirmAction.APPROVED)
        assert ok2 is False

        # entry 已不在 _pending
        assert not hitl_manager._has_pending(cid)

    async def test_wake_does_not_block_hang(self) -> None:
        """修订 S-14 配套：wake pop entry 后 hang 仍能通过自己持有的 entry 引用醒来

        验证：wake 立即 pop 不会破坏 hang 的 event 机制（hang 在 wake 之前
        已 get 了 entry 引用，event.set() 仍能让 hang 醒来）。
        """
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_wake_hang",
            trace_id="tr",
            tool_name="x",
            args={},
        )

        async def wake_later():
            await asyncio.sleep(0.05)
            ok = await hitl_manager.wake(cid, ConfirmAction.APPROVED)
            assert ok

        async def hang():
            return await hitl_manager.hang(cid, timeout_sec=2)

        wake_task = asyncio.create_task(wake_later())
        result = await hang()
        await wake_task

        # hang 通过自己持有的 entry 引用读到 wake 设的 action
        assert result == ConfirmAction.APPROVED

    async def test_hang_unregistered_raises(self) -> None:
        """hang 一个未 create_pending 的 confirmation_id → 调用方错误"""
        with pytest.raises(BusinessRuleException) as exc_info:
            await hitl_manager.hang("never_created")
        assert exc_info.value.error_code == "AI_HITL_PENDING_NOT_FOUND"


# ============ Redis pending 查询 / 删除 ============


class TestRedisPayload:
    async def test_get_pending_found(self) -> None:
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_g",
            trace_id="tr",
            tool_name="x",
            args={"k": "v"},
        )

        pending = await hitl_manager.get_pending(redis_module.redis_client, cid)
        assert pending is not None
        assert pending.user_id == 1
        assert pending.args == {"k": "v"}

    async def test_legacy_payload_without_tenant_defaults_to_single_tenant(
        self,
    ) -> None:
        """升级前仍在 Redis 的 pending 只能恢复为当前单租户 0。"""
        cid = hitl_manager.generate_confirmation_id()
        legacy = {
            "user_id": 1,
            "conversation_id": 1,
            "tool_call_id": "tc_legacy",
            "trace_id": "tr_legacy",
            "tool_name": "x",
            "args": {},
            "dry_run_result": None,
            "expires_at": "2099-01-01T00:00:00Z",
            "wake_action": None,
        }
        await redis_module.redis_client.set(
            f"{AI_CONFIRM_REDIS_PREFIX}:{cid}", json.dumps(legacy)
        )

        pending = await hitl_manager.get_pending(redis_module.redis_client, cid)

        assert pending is not None
        assert pending.tenant_id == 0

    async def test_get_pending_not_found(self) -> None:
        pending = await hitl_manager.get_pending(
            redis_module.redis_client, "never_existed"
        )
        assert pending is None

    async def test_get_pending_after_ttl_expires(self) -> None:
        """Redis TTL 过期后 get_pending 返回 None"""
        cid = hitl_manager.generate_confirmation_id()
        # 写入 1 秒 TTL
        await redis_module.redis_client.set(
            f"{AI_CONFIRM_REDIS_PREFIX}:{cid}",
            b"{}",
            ex=1,
        )
        await asyncio.sleep(1.1)
        pending = await hitl_manager.get_pending(redis_module.redis_client, cid)
        assert pending is None

    async def test_delete_pending(self) -> None:
        cid = hitl_manager.generate_confirmation_id()
        await hitl_manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_d",
            trace_id="tr",
            tool_name="x",
            args={},
        )
        await hitl_manager.delete_pending(redis_module.redis_client, cid)
        pending = await hitl_manager.get_pending(redis_module.redis_client, cid)
        assert pending is None


# ============ 服务重启清扫 ============


class TestCleanupOnStartup:
    async def test_cleanup_removes_stale_pending(self) -> None:
        """spec §8.4: 服务重启清扫 Redis 残留"""
        # 模拟 3 个孤儿 pending（写 Redis 但不创建 Event，等同 stream 已断）
        for i in range(3):
            await redis_module.redis_client.set(
                f"{AI_CONFIRM_REDIS_PREFIX}:stale_{i}",
                b'{"user_id": 1}',
                ex=300,
            )

        cleaned = await hitl_manager.cleanup_pending_on_startup()
        assert cleaned == 3

        # 验证全清干净
        keys = await redis_module.redis_client.keys(f"{AI_CONFIRM_REDIS_PREFIX}:*")
        assert keys == []

    async def test_cleanup_no_pending(self) -> None:
        """无 pending 时返回 0"""
        cleaned = await hitl_manager.cleanup_pending_on_startup()
        assert cleaned == 0

    async def test_cleanup_redis_unavailable_returns_zero(self) -> None:
        """修订 S-14：Redis 故障时 cleanup 不抛异常，返回 0 + log warning

        场景：服务启动时 Redis 短暂不可用（网络抖动 / Redis 重启）。
        修订前：scan_iter 抛 RedisError 阻断 lifespan。
        修订后：try/except + log warning + 返回 0；stale pending 由 Redis
        5min TTL 自然清掉，DB 端走 mark_expired 兜底。
        """
        from redis.exceptions import RedisError

        # mock scan_iter 为 async generator，第一次进入即抛 RedisError
        async def _failing_scan(*args, **kwargs):
            raise RedisError("connection refused")
            yield  # noqa  不可达，仅为让 Python 识别为 async generator

        original_scan = redis_module.redis_client.scan_iter
        redis_module.redis_client.scan_iter = _failing_scan

        try:
            cleaned = await hitl_manager.cleanup_pending_on_startup()
            assert cleaned == 0  # 不抛 + 返回 0
        finally:
            redis_module.redis_client.scan_iter = original_scan


# ============ v1.5+ redis_pubsub 模式 ============


@pytest.fixture
async def pubsub_mode(monkeypatch):
    """切到 redis_pubsub 模式（spec §8.4.1 v1.5+）

    非 autouse：仅显式声明的测试用，确保现有 memory 模式测试不变。
    """
    monkeypatch.setattr(settings, "AI_HITL_MODE", "redis_pubsub")
    yield


class TestPubSubMode:
    """redis_pubsub 模式：跨 worker 唤醒 + 防丢失（spec §8.4.1 v1.5+）"""

    async def test_cross_worker_wake(self, pubsub_mode) -> None:
        """模拟跨 worker：两个 HitlManager 实例共享同一 Redis

        场景：worker A 持有 SSE 流（hang），wake 落到 worker B。
        修订前（memory 模式）：worker B 找不到 entry → False → 5min 超时。
        v1.5+（redis_pubsub 模式）：B PUBLISH channel → A 收到 → 唤醒 hang。
        """
        from app.modules.ai.agents.hitl.manager import HitlManager

        manager_a = HitlManager()
        manager_b = HitlManager()

        cid = manager_a.generate_confirmation_id()
        await manager_a.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_xw",
            trace_id="tr",
            tool_name="x",
            args={"k": "v"},
        )

        async def hang() -> ConfirmAction:
            return await manager_a.hang(cid, timeout_sec=3)

        hang_task = asyncio.create_task(hang())
        await asyncio.sleep(0.15)  # 等 subscribe 完成

        # worker B wake（不同实例，模拟不同 worker）
        woken = await manager_b.wake(cid, ConfirmAction.APPROVED)
        assert woken is True

        # worker A 的 hang 应被唤醒
        result = await asyncio.wait_for(hang_task, timeout=2)
        assert result == ConfirmAction.APPROVED

    async def test_wake_before_subscribe_no_loss(self, pubsub_mode) -> None:
        """防丢失：wake 在 hang subscribe 之前到达

        场景：用户秒确认（前端预渲染了抽屉）。wake PUBLISH 在 hang subscribe
        之前到达，PUBLISH 消息丢失。但 wake 已先 SET pending.wake_action，
        hang subscribe 完成后 GET 检查 wake_action → 立即返回。
        """
        from app.modules.ai.agents.hitl.manager import HitlManager

        manager_a = HitlManager()
        manager_b = HitlManager()

        cid = manager_a.generate_confirmation_id()
        await manager_a.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_race",
            trace_id="tr",
            tool_name="x",
            args={},
        )

        # B wake 在 A hang 之前
        woken = await manager_b.wake(cid, ConfirmAction.REJECTED)
        assert woken is True

        # 验证 wake_action 已写入 Redis
        pending = await manager_a.get_pending(redis_module.redis_client, cid)
        assert pending is not None
        assert pending.wake_action == "rejected"

        # A 开始 hang — 应该立即拿到 wake_action，不等 PUBLISH
        result = await asyncio.wait_for(manager_a.hang(cid, timeout_sec=3), timeout=2)
        assert result == ConfirmAction.REJECTED

    async def test_wake_not_found_returns_false(self, pubsub_mode) -> None:
        """wake 不存在的 confirmation_id → False（已 expired / 未 create_pending）"""
        from app.modules.ai.agents.hitl.manager import HitlManager

        manager = HitlManager()
        woken = await manager.wake("nonexistent", ConfirmAction.APPROVED)
        assert woken is False

    async def test_pubsub_timeout_raises(self, pubsub_mode) -> None:
        """5min TTL 超时 → TimeoutError"""
        from app.modules.ai.agents.hitl.manager import HitlManager

        manager = HitlManager()
        cid = manager.generate_confirmation_id()
        await manager.create_pending(
            redis_module.redis_client,
            confirmation_id=cid,
            user_id=1,
            conversation_id=1,
            tool_call_id="tc_to",
            trace_id="tr",
            tool_name="x",
            args={},
        )

        with pytest.raises(asyncio.TimeoutError):
            await manager.hang(cid, timeout_sec=0.5)

    async def test_pending_payload_wake_action_default_none(self) -> None:
        """PendingPayload 默认 wake_action=None，向后兼容旧 payload"""
        from app.modules.ai.agents.hitl.manager import PendingPayload

        payload = PendingPayload(
            user_id=1,
            conversation_id=1,
            tool_call_id="tc",
            trace_id="tr",
            tool_name="x",
            args={},
            dry_run_result=None,
            expires_at="2026-07-13T00:00:00Z",
        )
        assert payload.wake_action is None
        assert payload.tenant_id == 0
