"""Regressions from real browser role creation and account disable flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.constants import EnableStatus
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.system.ai_tools import dept as tools
from app.modules.system.ai_tools import user_assignment
from app.modules.system.ai_tools.role import (
    AiRoleDataScope,
    _dry_run_role_create,
)
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)


@pytest.mark.parametrize("operation", ["update", "move"])
async def test_empty_department_change_still_previews_one_department(
    monkeypatch, operation
):
    monkeypatch.setattr(
        tools.dept_service,
        f"preview_{operation}",
        AsyncMock(
            return_value=SimpleNamespace(
                affected_user_ids=[],
                target_dept_name="空部门",
                parent_dept_name="总公司",
                snapshot={},
            )
        ),
    )
    args = {"dept_id": 1}
    args.update(
        {"dept_name": "新名称"} if operation == "update" else {"new_parent_id": 2}
    )
    result = await getattr(tools, f"_dry_run_dept_{operation}")(
        SimpleNamespace(db=None, user=SimpleNamespace(user_id=1), tenant=None),
        **args,
    )
    assert result.count == 1


@pytest.mark.asyncio
async def test_role_create_preview_fields_match_the_registered_contract(monkeypatch):
    load_builtin_tools()
    monkeypatch.setattr(
        role_management_service,
        "preview_create",
        AsyncMock(return_value=SimpleNamespace(snapshot={"version": "test"})),
    )
    preview = await _dry_run_role_create(
        SimpleNamespace(db=None, user=SimpleNamespace(user_id=1), tenant=None),
        role_name="体验只读员",
        role_code="exp_readonly",
        data_scope=AiRoleDataScope.SELF,
        status=EnableStatus.ENABLED,
    )
    meta = ToolRegistry.get().find("role.create").meta
    fields = _build_direct_confirmation_fields(
        meta,
        preview.execution_args,
        DryRunSummary(
            summary=preview.reason,
            affected_count=preview.count,
            confirmation_fields=preview.confirmation_fields,
        ),
    )
    assert {item["label"]: item["value"] for item in fields}["status"] == "1"


def test_disable_preview_accepts_the_same_wire_value_from_enum_and_string():
    load_builtin_tools()
    fields = _build_direct_confirmation_fields(
        ToolRegistry.get().find("user.update").meta,
        {"user_id": 123, "status": EnableStatus.DISABLED},
        DryRunSummary(
            summary="将停用测试用户",
            affected_count=1,
            confirmation_fields=[{"label": "status", "value": "2"}],
        ),
    )
    assert {item["label"]: item["value"] for item in fields}["status"] == "2"


def test_disable_preview_still_rejects_a_different_status():
    load_builtin_tools()
    with pytest.raises(ValueError, match="does not match"):
        _build_direct_confirmation_fields(
            ToolRegistry.get().find("user.update").meta,
            {"user_id": 123, "status": EnableStatus.ENABLED},
            DryRunSummary(
                summary="wrong",
                affected_count=1,
                confirmation_fields=[{"label": "status", "value": "2"}],
            ),
        )


@pytest.mark.parametrize("kind", ["roles", "dept"])
async def test_complete_assignment_preview_binds_canonical_order(monkeypatch, kind):
    load_builtin_tools()
    ctx = SimpleNamespace(db=None, user=SimpleNamespace(user_id=1), tenant=None)
    preview = SimpleNamespace(
        user_id=123,
        user_name="test-user",
        old_display=("旧集合",),
        new_display=("新集合甲", "新集合乙"),
        new_role_ids=(10, 20),
        new_assignments=((10, False), (20, True)),
        snapshot={},
    )
    if kind == "roles":
        monkeypatch.setattr(
            user_role_assignment_service,
            "preview_roles",
            AsyncMock(return_value=preview),
        )
        args = {"user_id": 123, "role_ids": [20, 10]}
    else:
        monkeypatch.setattr(
            user_department_assignment_service,
            "preview_departments",
            AsyncMock(return_value=preview),
        )
        args = {
            "user_id": 123,
            "dept_assignments": [
                {"dept_id": 20, "is_primary": True},
                {"dept_id": 10, "is_primary": False},
            ],
        }
    result = await getattr(user_assignment, f"_dry_run_user_update_{kind}")(ctx, **args)
    fields = _build_direct_confirmation_fields(
        ToolRegistry.get().find(f"user.update_{kind}").meta,
        result.execution_args,
        DryRunSummary(
            summary=result.reason,
            affected_count=1,
            confirmation_fields=result.confirmation_fields,
        ),
    )
    assert fields[1]["value"] == "旧集合 → 新集合甲; 新集合乙"
    # An actual change to the frozen set must still be refused.
    altered = dict(result.execution_args)
    key = "role_ids" if kind == "roles" else "dept_assignments"
    altered[key] = altered[key][:1]
    with pytest.raises(ValueError, match="does not match"):
        _build_direct_confirmation_fields(
            ToolRegistry.get().find(f"user.update_{kind}").meta,
            altered,
            DryRunSummary(
                summary=result.reason,
                affected_count=1,
                confirmation_fields=result.confirmation_fields,
            ),
        )
