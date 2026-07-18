"""跨会话 HITL 恢复端点测试 — spec §14 / SR-14

GET /ai/pending-confirmations 流程：
  1. DB 主查 user_id + status=pending_confirmation
  2. Redis GET 每条 confirmation_id 校验还活着（防 DB 脏数据）
  3. 按 queued_at 降序返回
"""

# ruff: noqa: ARG001, PLC0415

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.ai.agents.hitl.constants import (
    AiExecutionMode,
    AiOperationStatus,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.api.pending_confirmations import _parse_expires_at
from app.modules.ai.service.operation_log_service import operation_log_service


def _make_payload(*, expires_at: str | None = None) -> PendingPayload:
    """构造 Redis pending payload（模拟 hitl_manager.get_pending 返回值）"""
    if expires_at is None:
        expires_at = (datetime.now(UTC) + timedelta(minutes=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return PendingPayload(
        user_id=9001,
        conversation_id=100,
        tool_call_id="tc_test_001",
        trace_id="tr_test",
        tool_name="user.batch_delete",
        args={"user_names": ["test"]},
        dry_run_result=None,
        expires_at=expires_at,
    )


async def _insert_log(
    db,
    *,
    user_id: int = 9001,
    confirmation_id: str = "conf_test_1",
    tool_call_id: str = "tc_test_1",
    queued_at_offset_sec: int = 0,
    status: AiOperationStatus = AiOperationStatus.PENDING_CONFIRMATION,
    conversation_id: int = 100,
) -> int:
    """写一条 ai_operation_log（手动调 start_operation + queued_at 微调）

    不插 ai_conversation 行：service 用 LEFT JOIN，conversation 不存在时 title=None。
    production 里 FK 强约束保证 conversation 存在；测试不依赖 conversation 内容。
    """
    log_id = await operation_log_service.start_operation(
        db,
        trace_id="tr_test",
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="user.batch_delete",
        tool_call_id=tool_call_id,
        args_hash="a" * 64,
        args_summary="tool=user.batch_delete, risk=destructive, mode=hitl",
        risk_level="destructive",
        execution_mode=AiExecutionMode.HITL.value,
        status=status,
        confirmation_id=confirmation_id,
    )
    if queued_at_offset_sec:
        from app.modules.ai.models.operation_log import AiOperationLog

        log = await db.get(AiOperationLog, log_id)
        if log is not None:
            log.queued_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
                seconds=queued_at_offset_sec
            )
    return log_id


@pytest.mark.usefixtures("db_session")
class TestListPendingByUser:
    """service 层：DB 主查逻辑"""

    async def test_empty_when_no_pending(self, db_session) -> None:
        rows = await operation_log_service.list_pending_by_user(db_session, 9001)
        assert rows == []

    async def test_returns_pending_for_current_user_only(self, db_session) -> None:
        await _insert_log(db_session, user_id=9001, confirmation_id="c1")
        await _insert_log(
            db_session,
            user_id=9999,
            confirmation_id="c2",
            tool_call_id="tc_other",
        )
        rows = await operation_log_service.list_pending_by_user(db_session, 9001)
        assert len(rows) == 1
        assert rows[0][0].confirmation_id == "c1"

    async def test_excludes_non_pending_status(self, db_session) -> None:
        """status=success/failed/rejected/expired 的行不应返回"""
        await _insert_log(
            db_session,
            confirmation_id="c_run",
            tool_call_id="tc_run",
            status=AiOperationStatus.RUNNING,
        )
        await _insert_log(
            db_session,
            confirmation_id="c_success",
            tool_call_id="tc_success",
            status=AiOperationStatus.SUCCESS,
        )
        rows = await operation_log_service.list_pending_by_user(db_session, 9001)
        assert rows == []

    async def test_ordering_by_queued_at_desc(self, db_session) -> None:
        """多条 pending，按 queued_at 降序（最新的在前）"""
        await _insert_log(
            db_session,
            confirmation_id="old",
            tool_call_id="tc_old",
            queued_at_offset_sec=-100,
        )
        await _insert_log(
            db_session,
            confirmation_id="new",
            tool_call_id="tc_new",
            queued_at_offset_sec=0,
        )
        rows = await operation_log_service.list_pending_by_user(db_session, 9001)
        assert len(rows) == 2
        assert rows[0][0].confirmation_id == "new"
        assert rows[1][0].confirmation_id == "old"


class TestParseExpiresAt:
    """_parse_expires_at 边界测试"""

    def test_parses_iso_utc(self) -> None:
        result = _parse_expires_at("2026-07-18T12:30:45Z")
        assert result == datetime(2026, 7, 18, 12, 30, 45)

    def test_naive_no_tz(self) -> None:
        """解析后是 naive datetime（与 DB TIMESTAMP WITHOUT TIME ZONE 一致）"""
        result = _parse_expires_at("2026-07-18T12:30:45Z")
        assert result.tzinfo is None


@pytest.mark.usefixtures("db_session")
class TestEndpointRedisFilter:
    """端点层：DB ∩ Redis 校验逻辑（mock get_pending）"""

    async def test_filters_out_redis_expired(self, db_session) -> None:
        """DB 有 pending_confirmation 行但 Redis 已 expire（get_pending 返回 None）→ 跳过"""
        await _insert_log(db_session, confirmation_id="dead", tool_call_id="tc_dead")

        with patch(
            "app.modules.ai.api.pending_confirmations.hitl_manager.get_pending",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from types import SimpleNamespace

            from app.modules.ai.api.pending_confirmations import (
                list_pending_confirmations,
            )

            fake_user = SimpleNamespace(user_id=9001)
            response = await list_pending_confirmations(
                db=db_session, current_user=fake_user
            )
            assert response.data == []

    async def test_returns_alive_only(self, db_session) -> None:
        """两条 DB 行：一条 Redis 活、一条 Redis 死 → 只返 1 条"""

        async def fake_get_pending(redis, confirmation_id):
            if confirmation_id == "alive":
                return _make_payload()
            return None

        await _insert_log(db_session, confirmation_id="alive", tool_call_id="tc_alive")
        await _insert_log(db_session, confirmation_id="dead", tool_call_id="tc_dead")

        with patch(
            "app.modules.ai.api.pending_confirmations.hitl_manager.get_pending",
            new=AsyncMock(side_effect=fake_get_pending),
        ):
            from types import SimpleNamespace

            from app.modules.ai.api.pending_confirmations import (
                list_pending_confirmations,
            )

            fake_user = SimpleNamespace(user_id=9001)
            response = await list_pending_confirmations(
                db=db_session, current_user=fake_user
            )
            assert len(response.data) == 1
            assert response.data[0].confirmation_id == "alive"
            # conversation_id 经 field_serializer 序列化为 str（防 JS BigInt）
            dumped = response.data[0].model_dump(by_alias=True)
            assert dumped["conversationId"] == "100"
            assert isinstance(dumped["expiresAt"], str)
            assert isinstance(dumped["queuedAt"], str)

    async def test_filters_out_wake_action_already_set(self, db_session) -> None:
        """DB pending + Redis wake_action=rejected（已被 confirm 过）→ 跳过

        场景：memory 模式下 worker 死亡，confirm(rejected) 写了 Redis wake_action
        但 DB 状态未迁移（_transition 流程未跑完）。list 不应再展示这种半死不活的
        行——避免 banner 永远卡死。
        """

        async def fake_get_pending(redis, confirmation_id):
            if confirmation_id == "waked":
                # 模拟 wake_action 已设（confirm 过）
                payload = _make_payload()
                object.__setattr__(payload, "wake_action", "rejected")
                return payload
            if confirmation_id == "fresh":
                return _make_payload()
            return None

        await _insert_log(db_session, confirmation_id="waked", tool_call_id="tc_waked")
        await _insert_log(db_session, confirmation_id="fresh", tool_call_id="tc_fresh")

        with patch(
            "app.modules.ai.api.pending_confirmations.hitl_manager.get_pending",
            new=AsyncMock(side_effect=fake_get_pending),
        ):
            from types import SimpleNamespace

            from app.modules.ai.api.pending_confirmations import (
                list_pending_confirmations,
            )

            fake_user = SimpleNamespace(user_id=9001)
            response = await list_pending_confirmations(
                db=db_session, current_user=fake_user
            )
            # 只返 fresh，waked 被 wake_action 过滤掉
            assert len(response.data) == 1
            assert response.data[0].confirmation_id == "fresh"
