"""Phase 3 scoped Department Agent Tool contracts."""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tenant_helpers import tenant_context

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.exceptions import BusinessRuleException
from app.core.id_generator import next_id
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.ai.schemas.confirm import ConfirmationPresentation
from app.modules.system import ai_tools as system_ai_tools
from app.modules.system.models.dept import Dept
from scripts.check_ai_tools import EXPECTED_BUILTIN_TOOL_NAMES


def _context(
    db: AsyncSession,
    *,
    tool,
    accessible_dept_ids: set[int] | None,
) -> AiToolContext:
    user_id = next_id()
    user = MagicMock(
        user_id=user_id,
        tenant_id=0,
        user_name="phase3-dept-actor",
        roles=[],
    )
    tenant = tenant_context(actor_user_id=user_id)
    user._tenant_context = tenant
    return AiToolContext(
        user=user,
        perms=set(tool.__ai_tool_meta__.required_perms),
        db=db,
        data_scope=DataScopeContext(
            tenant=tenant,
            accessible_dept_ids=accessible_dept_ids,
            accessible_user_scope=None,
            filters=[],
        ),
        trace_id="tr_phase3_dept_tools",
        tool_meta=tool.__ai_tool_meta__,
        tenant=tenant,
    )


def _department(
    name: str,
    *,
    parent: Dept | None = None,
) -> Dept:
    dept_id = next_id()
    return Dept(
        dept_id=dept_id,
        tenant_id=0,
        parent_id=parent.dept_id if parent is not None else None,
        ancestors=("0" if parent is None else f"{parent.ancestors},{parent.dept_id}"),
        dept_name=f"{name}-{dept_id}",
        order_num=0,
        status=STATUS_ENABLED,
    )


async def test_department_count_and_list_treat_empty_scope_as_no_rows(
    db_session: AsyncSession,
) -> None:
    department = _department("phase3-empty-scope")
    db_session.add(department)
    await db_session.flush()

    count_result = await system_ai_tools.dept_count(
        _context(
            db_session,
            tool=system_ai_tools.dept_count,
            accessible_dept_ids=set(),
        )
    )
    list_result = await system_ai_tools.dept_list(
        _context(
            db_session,
            tool=system_ai_tools.dept_list,
            accessible_dept_ids=set(),
        )
    )

    assert count_result.data["count"] == 0
    assert list_result.data["total"] == 0
    assert list_result.ui.view_data["rows"] == []


@pytest.mark.parametrize(
    ("attribute", "tool_name", "permissions", "readonly"),
    [
        ("dept_lookup", "dept.lookup", ("system:dept:list",), True),
        (
            "dept_create",
            "dept.create",
            ("system:dept:add", "system:dept:list"),
            False,
        ),
        (
            "dept_update",
            "dept.update",
            ("system:dept:edit", "system:dept:list"),
            False,
        ),
        (
            "dept_move",
            "dept.move",
            ("system:dept:move", "system:dept:list"),
            False,
        ),
    ],
)
def test_department_tools_declare_the_phase3_gateway_contract(
    attribute: str,
    tool_name: str,
    permissions: tuple[str, ...],
    readonly: bool,
) -> None:
    tool = getattr(system_ai_tools, attribute, None)

    assert tool is not None
    meta = tool.__ai_tool_meta__
    assert meta.name == tool_name
    assert meta.agent == "dept_mgmt"
    assert meta.required_perms == permissions
    assert meta.readonly is readonly
    if readonly:
        assert meta.risk == "low"
        assert meta.idempotent is True
        assert meta.hitl_always is False
        assert meta.dry_run_supported is False
    else:
        assert meta.risk == "high"
        assert meta.idempotent is False
        assert meta.hitl_always is True
        assert meta.dry_run_supported is True


def test_static_inventory_contains_the_complete_department_slice() -> None:
    assert {
        "dept.count",
        "dept.list",
        "dept.lookup",
        "dept.create",
        "dept.update",
        "dept.move",
    } <= EXPECTED_BUILTIN_TOOL_NAMES


def test_department_write_result_separates_business_and_audit_values() -> None:
    department = _department("phase3-result-label")
    department.parent_id = next_id()
    department.status = STATUS_DISABLED
    affected_user_id = next_id()

    result = system_ai_tools._department_result(
        action="update",
        department=department,
        affected_user_ids=(affected_user_id,),
    )

    assert result.data == {
        "action": "update",
        "deptName": department.dept_name,
        "status": "disabled",
        "affectedUserCount": 1,
    }
    assert [field["label"] for field in result.ui.view_data["fields"]] == [
        "ai.tool.field.status",
        "ai.tool.field.affectedUserCount",
    ]
    assert result.ui.view_data["fields"][0]["value"] == (
        "page.system.common.status.disable"
    )
    assert result.ui.audit == {
        "dept_id": str(department.dept_id),
        "parent_id": str(department.parent_id),
        "status": STATUS_DISABLED,
        "action": "update",
        "affected_user_ids": [str(affected_user_id)],
    }


async def test_department_lookup_uses_only_visible_nodes_for_path_and_ambiguity(
    db_session: AsyncSession,
) -> None:
    hidden_root = _department("phase3-hidden-root")
    visible = _department("phase3-shared-name", parent=hidden_root)
    hidden_match = _department("phase3-shared-name")
    db_session.add_all([hidden_root, visible, hidden_match])
    await db_session.flush()
    tool = getattr(system_ai_tools, "dept_lookup", None)

    assert tool is not None
    result = await tool(
        _context(
            db_session,
            tool=tool,
            accessible_dept_ids={visible.dept_id},
        ),
        query="phase3-shared-name",
        limit=20,
    )

    assert result.data["matchCount"] == 1
    assert result.data["matches"][0]["deptId"] == str(visible.dept_id)
    assert result.data["matches"][0]["path"] == visible.dept_name
    assert hidden_root.dept_name not in result.data["matches"][0]["path"]
    assert result.projection.subject_refs == (
        {"type": "dept", "id": str(visible.dept_id)},
    )


async def test_department_lookup_includes_visible_disabled_management_targets(
    db_session: AsyncSession,
) -> None:
    disabled = _department("phase3-disabled-lookup")
    disabled.status = STATUS_DISABLED
    db_session.add(disabled)
    await db_session.flush()

    department_result = await system_ai_tools.dept_lookup(
        _context(
            db_session,
            tool=system_ai_tools.dept_lookup,
            accessible_dept_ids={disabled.dept_id},
        ),
        query=disabled.dept_name,
    )
    assignment_result = await system_ai_tools.user_dept_lookup(
        _context(
            db_session,
            tool=system_ai_tools.user_dept_lookup,
            accessible_dept_ids={disabled.dept_id},
        ),
        query=disabled.dept_name,
    )

    assert department_result.data["matchCount"] == 1
    assert department_result.data["matches"][0]["deptId"] == str(disabled.dept_id)
    assert assignment_result.data["matchCount"] == 0


@pytest.mark.parametrize("action", ["create", "update", "move"])
def test_department_write_tools_and_dry_runs_delegate_to_shared_service(
    action: str,
) -> None:
    tool = getattr(system_ai_tools, f"dept_{action}", None)
    dry_run = getattr(system_ai_tools, f"_dry_run_dept_{action}", None)

    assert tool is not None
    assert dry_run is not None
    assert "dept_service." in inspect.getsource(tool)
    assert "dept_service." in inspect.getsource(dry_run)
    assert ".commit(" not in inspect.getsource(tool)
    assert ".commit(" not in inspect.getsource(dry_run)


async def test_department_move_dry_run_counts_indirectly_affected_users(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    affected_user_ids = (next_id(), next_id(), next_id(), next_id())

    async def preview(*_args, **_kwargs):
        return SimpleNamespace(
            affected_user_ids=affected_user_ids,
            snapshot={"version": "test"},
        )

    monkeypatch.setattr(system_ai_tools.dept_service, "preview_move", preview)
    ctx = _context(
        db_session,
        tool=system_ai_tools.dept_move,
        accessible_dept_ids=None,
    )

    result = await system_ai_tools._dry_run_dept_move(
        ctx,
        dept_id=next_id(),
        new_parent_id=next_id(),
    )

    assert result.count == len(affected_user_ids)


async def test_department_write_dry_runs_build_scalar_gateway_presentations(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def preview(*_args, **_kwargs):
        return SimpleNamespace(
            affected_user_ids=(),
            snapshot={"version": "test"},
            target_dept_name="华东-客服组",
            parent_dept_name="总公司",
        )

    monkeypatch.setattr(system_ai_tools.dept_service, "preview_create", preview)
    monkeypatch.setattr(system_ai_tools.dept_service, "preview_update", preview)
    monkeypatch.setattr(system_ai_tools.dept_service, "preview_move", preview)
    cases = [
        (
            system_ai_tools.dept_create,
            system_ai_tools._dry_run_dept_create,
            {
                "parent_id": None,
                "dept_name": "Phase 3 root",
                "status": STATUS_ENABLED,
            },
        ),
        (
            system_ai_tools.dept_update,
            system_ai_tools._dry_run_dept_update,
            {"dept_id": next_id(), "leader": None, "status": STATUS_DISABLED},
        ),
        (
            system_ai_tools.dept_move,
            system_ai_tools._dry_run_dept_move,
            {"dept_id": next_id(), "new_parent_id": None},
        ),
    ]
    for tool, dry_run, args in cases:
        ctx = _context(db_session, tool=tool, accessible_dept_ids=None)
        result = await dry_run(ctx, **args)
        frozen_args = result.execution_args
        assert frozen_args is not None
        summary = DryRunSummary(
            summary=result.reason or "confirm",
            affected_count=result.count,
            confirmation_fields=result.confirmation_fields,
        )
        fields = _build_direct_confirmation_fields(
            tool.__ai_tool_meta__,
            frozen_args,
            summary,
        )

        presentation = ConfirmationPresentation(title="Confirm", fields=fields)

        assert presentation.fields
        if tool is system_ai_tools.dept_update:
            assert result.summary_params == {"deptName": "华东-客服组"}
            assert fields[0] == {
                "label": "dept_id",
                "value": "华东-客服组",
                "rawValue": frozen_args["dept_id"],
            }
            assert {"label": "leader", "value": "—"} in fields
            assert {"label": "status", "value": STATUS_DISABLED} in fields


async def test_department_update_preserves_explicit_null_for_nullable_fields(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = None
    department = _department("phase3-nullable-clear")

    async def update(_db, _dept_id, payload, **_kwargs):
        nonlocal captured
        captured = payload
        return department

    monkeypatch.setattr(system_ai_tools.dept_service, "update", update)
    tool = system_ai_tools.dept_update
    ctx = _context(db_session, tool=tool, accessible_dept_ids=None)
    ctx.approved_business_snapshot = {"version": "test"}

    await tool(
        ctx,
        dept_id=department.dept_id,
        leader=None,
        phone=None,
        email=None,
    )

    assert captured is not None
    assert captured.model_fields_set == {"leader", "phone", "email"}
    assert captured.leader is None
    assert captured.phone is None
    assert captured.email is None


async def test_department_update_dry_run_rejects_noncanonical_status(
    db_session: AsyncSession,
) -> None:
    tool = system_ai_tools.dept_update
    ctx = _context(db_session, tool=tool, accessible_dept_ids=None)

    with pytest.raises(BusinessRuleException) as exc_info:
        await system_ai_tools._dry_run_dept_update(
            ctx,
            dept_id=next_id(),
            status="0",
        )

    assert exc_info.value.error_code == "AI_ENABLE_STATUS_INVALID"
