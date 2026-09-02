"""ai_routing_log 字段完整性和 HMAC hash 测试。"""

import hashlib

import pytest
from sqlalchemy import select

from app.core.tenant import TenantContext
from app.modules.ai.models.routing_log import AiRoutingLog
from app.modules.ai.service.routing_log_service import routing_log_service


def _tenant(actor_user_id: int) -> TenantContext:
    return TenantContext(0, "default", actor_user_id, 1, "access_token")


@pytest.mark.asyncio
async def test_log_contains_llm_decision(db_session):
    """路由成功时记录完整决策链。"""
    await routing_log_service.write_log(
        db_session,
        trace_id="tr_abc",
        user_id=1,
        conversation_id=10,
        input_message="重置密码",
        candidates=["user_mgmt", "shared"],
        llm_choice="user_mgmt",
        final_agent="user_mgmt",
        reason="llm_resolved",
        latency_ms=120,
        tenant=_tenant(1),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AiRoutingLog).where(AiRoutingLog.trace_id == "tr_abc")
        )
    ).scalar_one()
    assert row.final_agent == "user_mgmt"
    assert row.llm_choice == "user_mgmt"
    assert row.reason == "llm_resolved"
    assert row.latency_ms == 120
    assert row.parent_log_id is None
    assert row.plan_step_index is None


@pytest.mark.asyncio
async def test_hash_is_hmac_not_plain(db_session):
    """input_message_hash 必须使用 HMAC，不能是裸 SHA256。"""
    await routing_log_service.write_log(
        db_session,
        trace_id="tr_hmac",
        user_id=42,
        conversation_id=None,
        input_message="common message",
        candidates=["shared"],
        final_agent="shared",
        reason="manual_override",
        latency_ms=0,
        tenant=_tenant(42),
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AiRoutingLog).where(AiRoutingLog.trace_id == "tr_hmac")
        )
    ).scalar_one()

    plain_sha = hashlib.sha256(b"common message").hexdigest()
    assert row.input_message_hash != plain_sha, (
        "HMAC 必须不同于裸 SHA256（防彩虹表反查）"
    )
    # SHA256 hexdigest 长度 = 32 字节 × 2 = 64 字符
    assert len(row.input_message_hash) == 64


@pytest.mark.asyncio
async def test_all_request_types_logged(db_session):
    """所有路由 reason 都必须写日志。"""
    reasons = [
        "llm_resolved",
        "clarification",
        "session_sticky",
        "manual_override",
        "supervisor_disabled",
        "safety_blocked",
        "no_provider",
        "no_candidates",
        "legacy_null_mode",
    ]
    for reason in reasons:
        await routing_log_service.write_log(
            db_session,
            trace_id=f"tr_{reason}",
            user_id=1,
            conversation_id=None,
            input_message="msg",
            candidates=["user_mgmt"],
            final_agent=(
                "user_mgmt"
                if reason not in ("clarification", "no_candidates", "no_provider")
                else None
            ),
            reason=reason,
            latency_ms=10,
            tenant=_tenant(1),
        )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(AiRoutingLog.reason).where(
                    AiRoutingLog.trace_id.in_([f"tr_{r}" for r in reasons])
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(rows) == set(reasons)


def test_fanout_fields_nullable_by_default():
    """parent_log_id 和 plan_step_index 默认 NULL。"""
    cols = AiRoutingLog.__table__.columns
    assert cols["parent_log_id"].nullable is True
    assert cols["plan_step_index"].nullable is True
