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

# ruff: noqa: ARG001, PLC0415

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
async def clean_redis_hitl():
    """每个测试重建 redis_client（绑新 loop）+ 清 ai:confirm:* keys + reset manager"""
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
        assert payload.tool_call_id == "tc_1"
        assert payload.expires_at.endswith("Z")

        # Redis 已写
        body = await redis_module.redis_client.get(f"{AI_CONFIRM_REDIS_PREFIX}:{cid}")
        assert body is not None
        data = json.loads(body)
        assert data["user_id"] == 9001
        assert data["args"] == {"user_id": 42, "new_dept_id": 8}

        # 进程内 Event 已注册
        assert hitl_manager._has_pending(cid)

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
