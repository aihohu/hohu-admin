"""Development diagnostics for direct confirmation argument visibility."""

from dataclasses import replace

import pytest

from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import RegisteredTool
from tools.checks import check_ai_tools as checks


async def _write(ctx, *, title: str):
    return ctx, title


async def _no_args(ctx):
    return ctx


def _tool(**overrides):
    meta = AiToolMeta(
        name="example.create",
        agent="example",
        summary="Create an example",
        required_perms=(),
        risk="high",
        hitl_always=True,
    )
    return RegisteredTool(meta=replace(meta, **overrides), fn=_write)


@pytest.mark.parametrize(
    "risk,hitl", [("high", False), ("destructive", False), ("low", True)]
)
def test_missing_fields_warn_for_direct_confirmation(risk, hitl):
    violations = checks.check_direct_confirmation_fields_declared(
        _tool(risk=risk, hitl_always=hitl)
    )
    assert len(violations) == 1
    assert violations[0].tool_name == "example.create"
    assert violations[0].severity == "warning"
    assert violations[0].check == "direct_confirmation_fields_declared"
    assert "args_summary_fields" in violations[0].detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"readonly": True},
        {"interaction_flow": "prepared", "prepared_execute_tool": "example.execute"},
        {"llm_visible": False},
        {"risk": "low", "hitl_always": False},
        {"args_summary_fields": ("title",)},
    ],
)
def test_valid_confirmation_paths_do_not_warn(overrides):
    assert checks.check_direct_confirmation_fields_declared(_tool(**overrides)) == []


def test_parameterless_action_does_not_warn():
    tool = _tool()
    tool.fn = _no_args
    assert checks.check_direct_confirmation_fields_declared(tool) == []


def test_check_is_included_in_scan_and_warning_does_not_fail_command(
    monkeypatch, capsys
):
    monkeypatch.setattr(checks, "load_all_tools", lambda: [_tool()])
    violations = checks.run_all_checks()
    warnings = [
        v for v in violations if v.check == "direct_confirmation_fields_declared"
    ]
    assert len(warnings) == 1
    monkeypatch.setattr(checks, "run_all_checks", lambda: warnings)
    assert checks.main() == 0
    assert "[WARNING] example.create" in capsys.readouterr().out
