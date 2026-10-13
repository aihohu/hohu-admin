"""Built-in destructive previews bind readable targets to frozen arguments."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.schemas.confirm import ConfirmationPresentation
from app.modules.job.ai_tools import _dry_run_job_update_cron, job_update_cron
from app.modules.job.service.job_service import job_service
from app.modules.system.ai_tools import user_management


def _fields(fn, result):
    return _build_direct_confirmation_fields(
        fn.__ai_tool_meta__,
        result.execution_args,
        DryRunSummary(
            summary=result.reason,
            affected_count=result.count,
            confirmation_fields=result.confirmation_fields,
        ),
    )


async def test_cron_preview_binds_named_job_and_exact_new_schedule(monkeypatch):
    job_id = 9007199254740993
    ctx = MagicMock()
    read = AsyncMock(
        return_value=SimpleNamespace(
            job_id=job_id, job_name="Nightly cleanup", cron_expression="0 1 * * *"
        )
    )
    write = AsyncMock()
    monkeypatch.setattr(job_service, "get_by_id", read)
    monkeypatch.setattr(job_service, "update_for_ai", write)
    result = await _dry_run_job_update_cron(
        ctx, job_id=job_id, cron_expression="0 2 * * *"
    )
    assert result.ok
    assert result.execution_args == {
        "job_id": job_id,
        "cron_expression": "0 2 * * *",
    }
    assert result.summary_params == {
        "oldCron": "0 1 * * *",
        "newCron": "0 2 * * *",
    }
    presentation = ConfirmationPresentation(
        title="Update schedule", fields=_fields(job_update_cron, result)
    )
    fields = json.loads(presentation.model_dump_json(by_alias=True))["fields"]
    assert fields[0]["value"] == "Nightly cleanup"
    assert fields[0]["rawValue"] == str(job_id)
    assert fields[1]["value"] == "0 2 * * *"
    read.assert_awaited_once_with(ctx.db, job_id, tenant=ctx.tenant)
    write.assert_not_awaited()
    for label, replacement in (
        ("job_id", job_id + 1),
        ("cron_expression", "* * * * *"),
    ):
        original = result.execution_args[label]
        result.execution_args[label] = replacement
        with pytest.raises(ValueError, match="does not match frozen argument"):
            _fields(job_update_cron, result)
        result.execution_args[label] = original


async def test_delete_preview_lists_all_bound_targets_without_phone(monkeypatch):
    users = [
        SimpleNamespace(
            user_id=9007199254740994, user_name="Bravo", user_phone="13900000000"
        ),
        SimpleNamespace(user_id=9007199254740993, user_name="Alpha", user_phone=None),
    ]
    monkeypatch.setattr(
        user_management, "_resolve_users", AsyncMock(return_value=users)
    )
    scope = AsyncMock()
    monkeypatch.setattr(user_management, "ensure_targets_in_scope", scope)
    result = await user_management._dry_run_user_batch_delete(
        MagicMock(), user_names=["Bravo", "Alpha"]
    )
    assert result.ok
    assert result.execution_args == {
        "user_ids": [9007199254740993, 9007199254740994],
        "user_names": None,
        "phones": None,
    }
    presentation = ConfirmationPresentation(
        title="Delete users", fields=_fields(user_management.user_batch_delete, result)
    )
    targets = presentation.fields[0]
    assert targets.label == "user_ids"
    assert targets.value == "Alpha (ID: 9007199254740993); Bravo (ID: 9007199254740994)"
    assert "13900000000" not in targets.value
    assert [item["userId"] for item in result.business_snapshot["targets"]] == [
        "9007199254740993",
        "9007199254740994",
    ]
    scope.assert_awaited_once()
    result.execution_args["user_ids"] = [9007199254740993]
    with pytest.raises(ValueError, match="does not match frozen argument"):
        _fields(user_management.user_batch_delete, result)


async def test_delete_preview_rejects_incomplete_target_display(monkeypatch):
    users = [
        SimpleNamespace(
            user_id=9007199254740993 + i, user_name=f"Target{i:02d}", user_phone=None
        )
        for i in range(20)
    ]
    monkeypatch.setattr(
        user_management, "_resolve_users", AsyncMock(return_value=users)
    )
    monkeypatch.setattr(user_management, "ensure_targets_in_scope", AsyncMock())
    result = await user_management._dry_run_user_batch_delete(
        MagicMock(), user_ids=[u.user_id for u in users]
    )
    assert not result.ok
    assert "分批" in result.reason
    assert result.execution_args is None
    assert result.business_snapshot is None


async def test_delete_preview_includes_targets_beyond_ten_examples(monkeypatch):
    users = [
        SimpleNamespace(user_id=i, user_name=f"U{i}", user_phone=None)
        for i in range(10, 21)
    ]
    monkeypatch.setattr(
        user_management, "_resolve_users", AsyncMock(return_value=users)
    )
    monkeypatch.setattr(user_management, "ensure_targets_in_scope", AsyncMock())
    result = await user_management._dry_run_user_batch_delete(
        MagicMock(), user_ids=[u.user_id for u in users]
    )
    assert result.ok
    assert len(result.examples) == 10
    presentation = ConfirmationPresentation(
        title="Delete users", fields=_fields(user_management.user_batch_delete, result)
    )
    assert presentation.fields[0].value == "; ".join(
        f"U{i} (ID: {i})" for i in range(10, 21)
    )
    assert presentation.fields[1].value == 11
