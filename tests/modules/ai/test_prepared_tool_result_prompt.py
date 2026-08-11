"""Prepared tool results must expose unambiguous terminal semantics to the LLM."""

import json

from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.tools.pydantic_ai_wrapper import (
    _tool_result_to_llm_string,
)


def test_approved_prepared_result_is_marked_executed() -> None:
    payload = json.loads(
        _tool_result_to_llm_string(
            ToolResult.success({"successCount": 1, "skippedCount": 2}),
            prepared_outcome="execute_if_approved",
        )
    )

    assert payload["_gateway"] == {
        "interactionFlow": "prepared",
        "actionStatus": "executed",
        "confirmationStatus": "approved",
    }
    assert payload["result"] == {"successCount": 1, "skippedCount": 2}


def test_preview_only_result_is_not_presented_as_pending_confirmation() -> None:
    payload = json.loads(
        _tool_result_to_llm_string(
            ToolResult.success({"totalRows": 3}),
            prepared_outcome="preview_only",
        )
    )

    assert payload["_gateway"] == {
        "interactionFlow": "prepared",
        "actionStatus": "previewed",
        "confirmationStatus": "not_requested",
    }
