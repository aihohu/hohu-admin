"""Structured i18n metadata for durable confirmation presentation."""

import json

from app.modules.ai.agents.hitl.constants import DryRunResult
from app.modules.ai.agents.hitl.events import (
    ConfirmationRequiredEvent,
    DryRunSummary,
    event_to_sse_data,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service


def test_confirmation_presentation_normalizes_i18n_metadata_to_camel_case():
    presentation = prepared_action_service.validate_presentation(
        {
            "title": "user.batch_delete",
            "summary": "将删除 2 个用户",
            "summary_key": "page.ai.chat.confirmBatchDeleteSummary",
            "summary_params": {"count": 2, "users": "alice, bob"},
            "fields": [],
            "warnings": ["此操作不可逆，请确认影响范围。"],
            "warning_keys": ["page.ai.chat.destructiveWarning"],
        }
    )

    assert presentation["summaryKey"] == "page.ai.chat.confirmBatchDeleteSummary"
    assert presentation["summaryParams"] == {"count": 2, "users": "alice, bob"}
    assert presentation["warningKeys"] == ["page.ai.chat.destructiveWarning"]
    assert "summary_key" not in presentation


def test_confirmation_presentation_preserves_safe_raw_machine_value():
    presentation = prepared_action_service.validate_presentation(
        {
            "title": "dept.update",
            "fields": [
                {
                    "label": "dept_id",
                    "value": "华东-客服组",
                    "rawValue": "800000004",
                }
            ],
            "warnings": [],
        }
    )

    assert presentation["fields"] == [
        {
            "label": "dept_id",
            "value": "华东-客服组",
            "rawValue": "800000004",
        }
    ]


def test_dry_run_result_accepts_structured_summary_metadata():
    result = DryRunResult(
        ok=True,
        count=1,
        reason="将 cron 从 old 改为 new",
        summary_key="page.ai.chat.confirmUpdateCronSummary",
        summary_params={"oldCron": "old", "newCron": "new"},
    )

    assert result.summary_key == "page.ai.chat.confirmUpdateCronSummary"
    assert result.summary_params == {"oldCron": "old", "newCron": "new"}


def test_confirmation_event_serializes_structured_dry_run_metadata():
    payload = json.loads(
        event_to_sse_data(
            ConfirmationRequiredEvent(
                confirmation_id="conf_i18n",
                tool="job.update_cron",
                tool_call_id="tc_i18n",
                summary="确认更新 cron",
                expires_at="2026-08-12T12:00:00Z",
                dry_run=DryRunSummary(
                    summary="将 cron 从 old 改为 new",
                    affected_count=1,
                    summary_key="page.ai.chat.confirmUpdateCronSummary",
                    summary_params={"oldCron": "old", "newCron": "new"},
                ),
            )
        )
    )

    assert payload["dryRun"]["summaryKey"] == "page.ai.chat.confirmUpdateCronSummary"
    assert payload["dryRun"]["summaryParams"] == {
        "oldCron": "old",
        "newCron": "new",
    }
