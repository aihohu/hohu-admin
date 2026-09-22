"""Legacy AI UTC storage must retain the same instant in browser DTOs and filters."""

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.ai.schemas.operation_log import (
    OperationLogOut,
    TraceListQuery,
    TraceOperationOut,
    TraceSummaryOut,
)
from app.modules.ai.schemas.routing_feedback import FeedbackListItem
from app.modules.ai.service import routing_feedback_query
from app.modules.ai.service.trace_service import TraceService
from tests.tenant_helpers import tenant_context


@pytest.mark.parametrize("aware", [False, True])
def test_audit_responses_preserve_utc_instants(aware):
    stamp = datetime(2026, 9, 17, 6, 0)
    if aware:
        stamp = stamp.replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=8)))
    summary = TraceSummaryOut(
        trace_id="tr_time",
        actor_id=1,
        actor_name="test",
        agent_codes=[],
        tool_names=[],
        statuses=[],
        operation_count=1,
        queued_at=stamp,
    )
    operation = TraceOperationOut(
        log_id=1,
        tool_call_id="tc_time",
        tool_name="user.count",
        agent_code="user_mgmt",
        actor_id=1,
        execution_mode="autonomous",
        risk_level="low",
        status="success",
        queued_at=stamp,
        source_message_at=stamp,
        started_at=stamp,
        finished_at=stamp,
    )
    feedback = FeedbackListItem(
        feedback_id=1,
        message_id=1,
        user_id=1,
        user_name="test",
        original_agent="shared",
        original_agent_name="test",
        feedback="correct",
        create_time=stamp,
    )
    owner_status = OperationLogOut(
        tool_call_id="tc_time",
        tool_name="user.count",
        status="success",
        finished_at=stamp,
    )
    for model, fields in [
        (summary, ["queuedAt"]),
        (operation, ["queuedAt", "sourceMessageAt", "startedAt", "finishedAt"]),
        (feedback, ["createTime"]),
        (owner_status, ["finishedAt"]),
    ]:
        payload = model.model_dump(mode="json", by_alias=True)
        for field in fields:
            assert payload[field] == "2026-09-17T06:00:00Z"
    assert summary.model_dump(mode="json", by_alias=True)["finishedAt"] is None


@pytest.mark.parametrize(
    "value", ["2026-09-17T14:00:00+08:00", 1789624800000, "1789624800000"]
)
def test_trace_filter_uses_utc_at_the_legacy_database_boundary(value):
    query = TraceListQuery(queued_from=value)
    assert query.queued_from == datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
    clauses = TraceService._filters(query, tenant_context())
    assert clauses[-1].right.value == datetime(2026, 9, 17, 6, 0)


@pytest.mark.parametrize("method", ["summary", "list_items"])
async def test_feedback_window_uses_utc_even_on_a_non_utc_application_host(
    monkeypatch, method
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
            return instant.astimezone(tz) if tz else datetime(2026, 9, 17, 14, 0)

    monkeypatch.setattr(routing_feedback_query, "datetime", Clock)
    empty = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: []),
        all=lambda: [],
        scalar=lambda: 0,
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=empty))
    kwargs = {"days": 7, "tenant": tenant_context()}
    if method == "list_items":
        kwargs.update(
            current=1,
            size=20,
            feedback="wrong",
            original_agent=None,
            corrected_agent=None,
        )
    await getattr(routing_feedback_query.routing_feedback_query_service, method)(
        db, **kwargs
    )
    params = db.execute.call_args_list[0].args[0].compile().params
    assert datetime(2026, 9, 10, 6, 0) in params.values()
