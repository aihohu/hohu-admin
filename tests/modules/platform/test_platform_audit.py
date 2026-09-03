from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessException
from app.modules.platform.audit import add_platform_audit, authorize_platform_request
from app.modules.platform.constants import PLATFORM_AI_READ


async def test_missing_audit_envelope_fails_before_business_execution():
    principal = SimpleNamespace(
        principal_id=17,
        principal_name="platform-auditor",
        permissions=frozenset({PLATFORM_AI_READ}),
    )
    persist = AsyncMock(return_value=1001)

    with pytest.raises(BusinessException) as exc_info:
        await authorize_platform_request(
            principal=principal,
            permission=PLATFORM_AI_READ,
            method="GET",
            path="/ai/provider/list",
            reason=None,
            ticket_id="SEC-1",
            correlation_id="corr-1",
            ip="127.0.0.1",
            request_summary={"queryKeys": []},
            persist=persist,
        )

    assert exc_info.value.code == 400
    assert exc_info.value.error_code == "PLATFORM_AUDIT_CONTEXT_REQUIRED"
    persist.assert_awaited_once()
    assert persist.await_args.kwargs["event_type"] == "denied"


async def test_authorization_intent_is_persisted_before_context_is_returned():
    principal = SimpleNamespace(
        principal_id=17,
        principal_name="platform-operator",
        permissions=frozenset({PLATFORM_AI_READ}),
    )
    persist = AsyncMock(return_value=1002)

    result = await authorize_platform_request(
        principal=principal,
        permission=PLATFORM_AI_READ,
        method="GET",
        path="/ai/provider/list",
        reason="Review provider configuration",
        ticket_id="SEC-2",
        correlation_id="corr-2",
        ip="127.0.0.1",
        request_summary={"queryKeys": ["current"]},
        persist=persist,
    )

    persist.assert_awaited_once()
    assert persist.await_args.kwargs["event_type"] == "authorized"
    assert result.authorization_audit_id == 1002
    assert result.context.actor_principal_id == 17
    assert result.context.ticket_id == "SEC-2"


async def test_sensitive_audit_context_is_redacted_and_rejected_before_business():
    principal = SimpleNamespace(
        principal_id=17,
        principal_name="platform-operator",
        permissions=frozenset({PLATFORM_AI_READ}),
    )
    persist = AsyncMock(return_value=1003)

    with pytest.raises(BusinessException) as exc_info:
        await authorize_platform_request(
            principal=principal,
            permission=PLATFORM_AI_READ,
            method="GET",
            path="/ai/provider/token=abcdefghijklmnop123456",
            reason="Investigate token=abcdefghijklmnop123456",
            ticket_id="SEC-SENSITIVE",
            correlation_id="corr-sensitive",
            ip="password=abcdefghijklmnop123456",
            request_summary={"queryKeyCount": 1, "apiKey": "do-not-store"},
            persist=persist,
        )

    assert exc_info.value.error_code == "PLATFORM_AUDIT_CONTEXT_SENSITIVE"
    stored = persist.await_args.kwargs
    assert stored["event_type"] == "denied"
    assert "abcdefghijklmnop123456" not in stored["reason"]
    assert "abcdefghijklmnop123456" not in stored["path"]
    assert stored["ip"] is None
    assert stored["request_summary"] == {"queryKeyCount": 1}


async def test_generic_audit_writer_cannot_bypass_idempotent_completion_path():
    db = SimpleNamespace(add=MagicMock(), flush=AsyncMock())

    with pytest.raises(BusinessException) as exc_info:
        await add_platform_audit(
            db,
            actor_principal_id=17,
            actor_name="platform-operator",
            permission=PLATFORM_AI_READ,
            event_type="completed",
            method="GET",
            path="/ai/provider/list",
            reason="Review provider configuration",
            ticket_id="SEC-3",
            correlation_id="corr-3",
            ip="127.0.0.1",
            authorization_audit_id=1002,
            status_code=200,
            duration_ms=1,
            result_summary={"statusCode": 200},
        )

    assert exc_info.value.error_code == "PLATFORM_AUDIT_COMPLETION_WRITER_REQUIRED"
    db.add.assert_not_called()
