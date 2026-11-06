"""Pure RR facts remain lock-free and reject incomplete discovery."""

from types import SimpleNamespace

from app.core.tenant import TenantContext
from app.db.authorization_completion_facts_owner import CompletionFactsOwner
from app.modules.ai.service.completion_read_state_service import (
    completion_read_state_service,
)


async def test_read_state_input_is_bounded_before_opening_a_session(monkeypatch):
    def forbidden():
        raise AssertionError("oversized input reached database")

    monkeypatch.setattr(
        "app.db.authorization_http.get_authorization_runtime",
        lambda: SimpleNamespace(completion_facts=CompletionFactsOwner(forbidden)),
    )
    consumed = []

    def identifiers():
        for value in range(1000):
            consumed.append(value)
            yield f"call-{value}"

    result = await completion_read_state_service.read(
        tenant=TenantContext(0, "plan-0", 101, 1, "access_token"),
        tool_call_ids=identifiers(),
    )
    assert list(result) == [""] and result[""].unknown
    assert len(consumed) == 257
