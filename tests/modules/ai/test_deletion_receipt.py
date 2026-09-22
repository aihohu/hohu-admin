"""A successful deletion exposes only an independently authorized receipt."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.result_projection_service import result_projection_service
from tests.modules.ai.test_conversation_api import _message
from tests.modules.ai.test_result_projection_service import TENANT, _user


async def test_projected_receipt_excludes_original_sensitive_payload():
    message = _message(
        message_id=22,
        role="assistant",
        content="private name and phone",
        tool_calls=[{"args": "secret", "ui": {"downloadUrl": "private"}}],
    )
    message.parts = [{"image_url": "private-image"}]
    with (
        patch.object(
            conversation_service, "get_messages", AsyncMock(return_value=[message])
        ),
        patch.object(
            result_projection_service,
            "authorize_message_projection",
            AsyncMock(return_value=False),
        ),
        patch.object(
            result_projection_service,
            "deletion_receipt",
            AsyncMock(return_value="本轮删除操作已成功完成。"),
            create=True,
        ),
    ):
        result = await conversation_service.project_messages(
            AsyncMock(), conversation_id=8001, current_user=_user()
        )
    assert result[0].content == "本轮删除操作已成功完成。"
    assert result[0].parts is None
    assert result[0].tool_calls is None
    assert "private" not in result[0].model_dump_json()
    assert message.content == "private name and phone"


@pytest.mark.parametrize(
    "change,allowed",
    [
        ({}, True),
        ({"status": "expired"}, False),
        ({"status": "failed"}, False),
        ({"user_id": 999}, False),
        ({"tenant_id": 99}, False),
        ({"conversation_id": 999}, False),
        ({"trace_id": "other"}, False),
        ({"tool_call_id": "other"}, False),
        ({"tool_name": "user.update"}, False),
    ],
)
async def test_receipt_requires_matching_success_audit(db_session, change, allowed):
    user = _user("ai:chat:use", "system:user:delete")
    lineage = result_projection_service.freeze_lineage(
        tenant=TENANT,
        agent_code="user_mgmt",
        tool_codes=["user.batch_delete"],
        subject_refs=[{"type": "user", "id": "9"}],
        projection_dependency_message_ids=[8],
    )
    message = SimpleNamespace(
        role="assistant",
        conversation_id=8001,
        trace_id="receipt-trace",
        tool_calls=[
            {"tool": "user.batch_delete", "tool_call_id": "receipt-call", "ok": True}
        ],
    )
    values = {
        "tenant_id": 0,
        "user_id": 101,
        "conversation_id": 8001,
        "trace_id": "receipt-trace",
        "tool_call_id": "receipt-call",
        "tool_name": "user.batch_delete",
        "agent_code": "user_mgmt",
        "args_hash": "a" * 64,
        "args_summary": "{}",
        "risk_level": "destructive",
        "execution_mode": "hitl",
        "status": "success",
    }
    values.update(change)
    db_session.add(AiOperationLog(**values))
    await db_session.flush()
    with (
        patch.object(
            result_projection_service, "lineage_from_record", return_value=lineage
        ),
        patch.object(
            result_projection_service,
            "_authorize_agent_and_tools",
            AsyncMock(return_value=True),
        ),
    ):
        result = await result_projection_service.deletion_receipt(
            db_session,
            user,
            owner_user_id=101,
            message=message,
        )
    assert bool(result) is allowed
    if result:
        assert result == "本轮删除操作已成功完成。目标数据已删除，历史详情不再展示。"


@pytest.mark.parametrize("reason", ["owner", "tenant", "chat", "tool", "hash", "scope"])
async def test_receipt_preserves_live_authority_checks(reason):
    user = _user("ai:chat:use", "system:user:delete")
    lineage = result_projection_service.freeze_lineage(
        tenant=TENANT,
        agent_code="user_mgmt",
        tool_codes=["user.batch_delete"],
        subject_refs=[{"type": "user", "id": "9"}],
        data_scope_hash="original",
    )
    if reason == "tenant":
        lineage = replace(lineage, tenant_id=99)
    if reason == "hash":
        lineage = replace(lineage, subject_refs_hash="tampered")
    if reason == "chat":
        user = _user("system:user:delete")
    db = AsyncMock()
    message = SimpleNamespace(
        role="assistant",
        trace_id="trace",
        conversation_id=8001,
        tool_calls=[{"tool": "user.batch_delete", "tool_call_id": "call"}],
    )
    with (
        patch.object(
            result_projection_service, "lineage_from_record", return_value=lineage
        ),
        patch.object(
            result_projection_service,
            "_authorize_agent_and_tools",
            AsyncMock(return_value=reason != "tool"),
        ),
        patch.object(
            result_projection_service,
            "compute_data_scope_hash",
            AsyncMock(return_value="changed" if reason == "scope" else "original"),
        ),
    ):
        result = await result_projection_service.deletion_receipt(
            db,
            user,
            owner_user_id=999 if reason == "owner" else 101,
            message=message,
        )
    assert result is None
    db.scalar.assert_not_awaited()
