"""Phase 4 AI Trace model, redaction, DTO, and route contracts."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.routing import APIRoute
from sqlalchemy import String, Text

from app.modules.ai.api.operation_log import router
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.schemas.operation_log import (
    TraceDetailOut,
    TraceOperationOut,
    TraceSummaryOut,
)
from app.modules.ai.service.operation_log_service import build_target_summary


def _route(path: str, method: str) -> APIRoute:
    matches = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    ]
    assert len(matches) == 1, (path, method, matches)
    return matches[0]


def test_trace_model_has_nullable_immutable_audit_fields() -> None:
    agent = AiOperationLog.__table__.c.agent_code
    target = AiOperationLog.__table__.c.target_summary

    assert isinstance(agent.type, String)
    assert agent.type.length == 64
    assert agent.nullable is True
    assert isinstance(target.type, Text)
    assert target.nullable is True


def test_target_summary_keeps_only_sorted_stable_subject_refs() -> None:
    summary = build_target_summary(
        [
            {
                "type": "user",
                "id": "42",
                "password": "TRACE_SENTINEL_PASSWORD",
                "email": "alice@example.com",
            },
            {"type": "dept", "id": "8", "rawArgs": {"secret": "sentinel"}},
            {"type": "user", "id": "42"},
        ]
    )

    assert summary == '[{"id":"8","type":"dept"},{"id":"42","type":"user"}]'
    assert "TRACE_SENTINEL_PASSWORD" not in summary
    assert "alice@example.com" not in summary
    assert "rawArgs" not in summary


def test_target_summary_rejects_noncanonical_refs() -> None:
    assert build_target_summary(None) is None
    assert build_target_summary([]) is None
    assert build_target_summary([{"type": "user", "id": "not-an-id"}]) is None
    assert build_target_summary([{"type": "../../secret", "id": "42"}]) is None


def test_trace_routes_are_independent_read_endpoints() -> None:
    list_route = _route("/traces", "GET")
    detail_route = _route("/traces/{trace_id}", "GET")

    assert list_route.response_model is not None
    assert detail_route.response_model is not None


def test_trace_service_uses_the_system_service_boundary() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "app"
        / "modules"
        / "ai"
        / "service"
        / "trace_service.py"
    ).read_text(encoding="utf-8")

    assert "app.modules.system.models" not in source
    assert "app.modules.system.service.user_service" in source


def test_trace_dto_serialization_has_a_strict_metadata_allowlist() -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
    operation = TraceOperationOut(
        logId=101,
        toolCallId="tc_phase4",
        toolName="user.update",
        agentCode="user_mgmt",
        actorId=7,
        sourceMessageId=55,
        sourceMessageRole="user",
        sourceMessageAt=now,
        targetSummary=[{"type": "user", "id": "42"}],
        executionMode="hitl",
        riskLevel="high",
        status="success",
        confirmationId="conf_phase4",
        approvedBy=7,
        queuedAt=now,
        startedAt=now,
        finishedAt=now,
        durationMs=12,
        hitlWaitMs=5,
    )
    detail = TraceDetailOut(
        traceId="tr_phase4",
        conversationId=900,
        operations=[operation],
    )
    summary = TraceSummaryOut(
        traceId="tr_phase4",
        actorId=7,
        actorName="alice",
        agentCodes=["user_mgmt"],
        toolNames=["user.update"],
        statuses=["success"],
        operationCount=1,
        queuedAt=now,
        finishedAt=now,
    )

    payload = {
        "detail": detail.model_dump(by_alias=True, mode="json"),
        "summary": summary.model_dump(by_alias=True, mode="json"),
    }
    serialized = str(payload)
    for forbidden in (
        "content",
        "systemPrompt",
        "rawPrompt",
        "argsHash",
        "argsSummary",
        "rawArgs",
        "frozenArgs",
        "resultSummary",
        "resultData",
        "resultUi",
    ):
        assert forbidden not in serialized
