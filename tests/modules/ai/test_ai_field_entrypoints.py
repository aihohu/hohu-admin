"""Real Gateway adapters must execute field proof before handlers/serialization."""

from dataclasses import replace

import pytest

from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.system.service.user_ai_fields import user_ai_fields
from tests.modules.ai.test_authorization_gateway_reads import (
    completion_case as completion_case,
)
from tests.modules.ai.test_authorization_gateway_reads import (
    read_case as read_case,
)


@pytest.mark.parametrize(
    "missing,code",
    [
        ("identity", "FIELD_CONFIRMATION_FACTS_REQUIRED"),
        ("write", "FIELD_WRITE_DENIED"),
    ],
)
async def test_preflight_refuses_before_values_or_proposal(
    read_case, monkeypatch, missing, code
):
    original = user_ai_fields.context

    async def restricted(*values, **kwargs):
        context = await original(*values, **kwargs)
        return replace(
            context,
            grants=tuple(
                g
                for g in context.grants
                if not (
                    (
                        missing == "identity"
                        and g.field_id == "user_name"
                        and g.operation == "read"
                    )
                    or (
                        missing == "write"
                        and g.field_id == "user_phone"
                        and g.operation == "update"
                    )
                )
            ),
        )

    monkeypatch.setattr(user_ai_fields, "context", restricted)
    outcome = await executor._run_dry_run(
        ToolRegistry.get().find("user.update"),
        {"user_id": read_case.user.user_id, "user_phone": "13800000000"},
        read_case,
    )
    assert outcome.failure is not None and outcome.failure.error_code == code
    assert outcome.authorization_denied
    assert outcome.summary is None


@pytest.mark.parametrize(
    "tool,args",
    [
        ("user.count", {"filters": {"status": "1"}}),
        ("user.stats", {"group_by": "status"}),
        ("user.distinct", {"field": "status"}),
    ],
)
async def test_gateway_aggregate_denies_uncovered_base_range(
    read_case, monkeypatch, tool, args
):
    original = user_ai_fields.context

    async def restricted(*values, **kwargs):
        context = await original(*values, **kwargs)
        return replace(
            context, grants=tuple(g for g in context.grants if g.field_id != "status")
        )

    monkeypatch.setattr(user_ai_fields, "context", restricted)
    monkeypatch.setattr(executor, "_safe_write_query_cache", lambda *_a, **_k: None)
    result = await executor._invoke_tool_fn(
        ToolRegistry.get().find(tool), args, read_case, "a" * 64
    )
    assert not result.ok
    assert result.error_code == "FIELD_QUERY_DENIED"


async def test_hidden_field_is_removed_from_model_and_ui(read_case, monkeypatch):
    original = user_ai_fields.context

    async def restricted(*values, **kwargs):
        context = await original(*values, **kwargs)
        return replace(
            context, grants=tuple(g for g in context.grants if g.field_id != "nickname")
        )

    monkeypatch.setattr(user_ai_fields, "context", restricted)
    monkeypatch.setattr(executor, "_safe_write_query_cache", lambda *_a, **_k: None)
    result = await executor._invoke_tool_fn(
        ToolRegistry.get().find("user.list"), {}, read_case, "a" * 64
    )
    assert result.ok
    assert result.data["records"]
    assert all("nickname" not in row for row in result.data["records"])
    assert all("nickname" not in row for row in result.ui.view_data["rows"])
    assert all(column["key"] != "nickname" for column in result.ui.view_data["columns"])


async def test_lookup_copied_labels_and_relationships_are_projected(
    read_case, monkeypatch
):
    original = user_ai_fields.context
    hidden = {
        "user_name",
        "nickname",
        "user_phone",
        "user_email",
        "roles",
        "departments",
    }

    async def restricted(*values, **kwargs):
        context = await original(*values, **kwargs)
        return replace(
            context,
            grants=tuple(
                g
                for g in context.grants
                if g.operation != "read" or g.field_id not in hidden
            ),
        )

    monkeypatch.setattr(user_ai_fields, "context", restricted)
    monkeypatch.setattr(executor, "_safe_write_query_cache", lambda *_a, **_k: None)
    result = await executor._invoke_tool_fn(
        ToolRegistry.get().find("user.lookup"),
        {"user_name": "admin"},
        read_case,
        "a" * 64,
    )
    assert result.ok
    assert not set(result.data) & hidden
    assert "roleAssignments" not in result.data
    assert "departmentAssignments" not in result.data
    assert result.ui.view_data["title"] == "用户"
    assert result.ui.label_params == {"userName": "用户"}
    assert set(result.ui.audit) == {"user_id"}
    assert all(
        item["label"]
        not in {
            "page.system.user.userName",
            "page.system.user.nickname",
            "page.system.user.userPhone",
            "page.system.user.userEmail",
        }
        for item in result.ui.view_data["fields"]
    )
