"""Phase 3 Role Agent Tool inventory and delegation contracts."""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.modules.ai.agents.gateway.executor import _build_direct_confirmation_fields
from app.modules.ai.agents.hitl.events import DryRunSummary
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.ai.schemas.confirm import ConfirmationPresentation
from app.modules.system import ai_tools as system_ai_tools
from app.modules.system.models.role import Role
from scripts.check_ai_tools import EXPECTED_BUILTIN_TOOL_NAMES


@pytest.mark.parametrize(
    ("attribute", "tool_name", "permissions", "readonly"),
    [
        ("role_lookup", "role.lookup", ("system:role:list",), True),
        ("role_create", "role.create", ("system:role:add",), False),
        ("role_update", "role.update", ("system:role:edit",), False),
        (
            "role_update_menus",
            "role.update_menus",
            ("system:role:menu-auth",),
            False,
        ),
        (
            "role_update_agents",
            "role.update_agents",
            ("system:role:ai-agent-auth",),
            False,
        ),
    ],
)
def test_role_tools_declare_the_phase3_gateway_contract(
    attribute: str,
    tool_name: str,
    permissions: tuple[str, ...],
    readonly: bool,
) -> None:
    tool = getattr(system_ai_tools, attribute, None)

    assert tool is not None
    meta = tool.__ai_tool_meta__
    assert meta.name == tool_name
    assert meta.agent == "role_mgmt"
    assert meta.required_perms == permissions
    assert meta.readonly is readonly
    if readonly:
        assert meta.risk == "low"
        assert meta.idempotent is True
    else:
        assert meta.risk == "high"
        assert meta.hitl_always is True
        assert meta.dry_run_supported is True


def test_static_inventory_contains_the_complete_role_slice() -> None:
    assert {
        "role.count",
        "role.list",
        "role.lookup",
        "role.create",
        "role.update",
        "role.update_menus",
        "role.update_agents",
    } <= EXPECTED_BUILTIN_TOOL_NAMES


def test_role_write_result_uses_translatable_field_labels() -> None:
    role = Role(
        role_id=next_id(),
        role_name="Phase 3 result role",
        role_code=f"R_PHASE3_RESULT_{next_id()}",
        data_scope="5",
        status="1",
    )

    result = system_ai_tools._role_result(action="update", role=role)

    assert [field["label"] for field in result.ui.view_data["fields"]] == [
        "ai.tool.field.roleId",
        "ai.tool.field.roleCode",
        "ai.tool.field.action",
    ]
    assert result.projection.subject_refs == (
        {"type": "managed_role", "id": str(role.role_id)},
    )
    assert result.data["dataScope"] == "SELF"
    assert result.data["dataScopeCode"] == "5"


def test_role_data_scope_model_contract_uses_named_values() -> None:
    create_scope = (
        inspect.signature(system_ai_tools.role_create)
        .parameters["data_scope"]
        .annotation
    )
    update_scope = (
        inspect.signature(system_ai_tools.role_update)
        .parameters["data_scope"]
        .annotation
    )

    assert create_scope is system_ai_tools.AiRoleDataScope
    assert set(TypeAdapter(create_scope).json_schema()["enum"]) == {
        "ALL",
        "CUSTOM",
        "DEPT",
        "DEPT_AND_SUB",
        "SELF",
    }
    with pytest.raises(ValidationError):
        TypeAdapter(create_scope).validate_python("4")
    assert "AiRoleDataScope" in str(update_scope)


async def test_role_update_dry_run_rejects_noncanonical_status(
    db_session: AsyncSession,
) -> None:
    tool = system_ai_tools.role_update
    ctx = AiToolContext(
        user=MagicMock(user_id=next_id()),
        perms=set(tool.__ai_tool_meta__.required_perms),
        db=db_session,
        data_scope=DataScopeContext(None, None, []),
        trace_id="tr_role_invalid_status",
        tool_meta=tool.__ai_tool_meta__,
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await system_ai_tools._dry_run_role_update(
            ctx,
            role_id=next_id(),
            status="0",
        )

    assert exc_info.value.error_code == "AI_ENABLE_STATUS_INVALID"


async def test_role_create_dry_run_freezes_canonical_data_scope_code(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = None

    async def preview(_db, payload, **_kwargs):
        nonlocal captured
        captured = payload
        return SimpleNamespace(member_user_ids=(), snapshot={"version": "test"})

    monkeypatch.setattr(
        system_ai_tools.role_management_service,
        "preview_create",
        preview,
    )
    tool = system_ai_tools.role_create
    ctx = AiToolContext(
        user=MagicMock(user_id=next_id()),
        perms=set(tool.__ai_tool_meta__.required_perms),
        db=db_session,
        data_scope=DataScopeContext(None, None, []),
        trace_id="tr_role_named_scope",
        tool_meta=tool.__ai_tool_meta__,
    )

    result = await system_ai_tools._dry_run_role_create(
        ctx,
        role_name="Named scope role",
        role_code=f"R_NAMED_SCOPE_{next_id()}",
        data_scope=system_ai_tools.AiRoleDataScope.SELF,
        status="1",
    )

    assert captured is not None
    assert captured.data_scope == "5"
    assert result.execution_args["data_scope"] == "5"
    scope_field = next(
        field for field in result.confirmation_fields if field["label"] == "data_scope"
    )
    assert scope_field == {
        "label": "data_scope",
        "value": "5",
        "display_value": "SELF (5)",
    }


@pytest.mark.parametrize(
    "value",
    [[True], [0], [-1], ["1"], [1, 1]],
)
def test_role_related_ids_are_strict_positive_unique_integers(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(system_ai_tools.AiRoleRelatedIds).validate_python(value)


async def test_approved_role_drift_maps_to_prepared_snapshot_stale(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_create(*_args, **_kwargs):
        raise AuthorizationException(error_code="PERMISSION_DENIED")

    monkeypatch.setattr(
        system_ai_tools.role_management_service,
        "create",
        reject_create,
    )
    tool = system_ai_tools.role_create
    ctx = AiToolContext(
        user=type("Actor", (), {"user_id": next_id()})(),
        perms=set(tool.__ai_tool_meta__.required_perms),
        db=db_session,
        data_scope=DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_scope=None,
            filters=[],
        ),
        trace_id="tr_phase3_role_stale",
        tool_meta=tool.__ai_tool_meta__,
        approved_business_snapshot={"version": "old"},
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await tool(
            ctx,
            role_name="Scoped auditor",
            role_code="R_SCOPED_AUDITOR",
            data_scope="5",
            status="1",
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"


@pytest.mark.parametrize(
    "action", ["create", "update", "update_menus", "update_agents"]
)
def test_role_write_tools_and_dry_runs_delegate_to_shared_service(action: str) -> None:
    tool = getattr(system_ai_tools, f"role_{action}", None)
    dry_run = getattr(system_ai_tools, f"_dry_run_role_{action}", None)

    assert tool is not None
    assert dry_run is not None
    assert "role_management_service." in inspect.getsource(tool)
    assert "role_management_service." in inspect.getsource(dry_run)
    assert ".commit(" not in inspect.getsource(tool)
    assert ".commit(" not in inspect.getsource(dry_run)


async def test_role_menu_dry_run_counts_indirectly_affected_members(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_ids = (next_id(), next_id(), next_id())

    async def preview(*_args, **_kwargs):
        return SimpleNamespace(member_user_ids=member_ids, snapshot={"version": "test"})

    monkeypatch.setattr(
        system_ai_tools.role_management_service,
        "preview_update_menus",
        preview,
    )
    ctx = AiToolContext(
        user=MagicMock(user_id=next_id()),
        perms={"system:role:menu-auth"},
        db=db_session,
        data_scope=DataScopeContext(None, None, []),
        trace_id="tr_phase3_role_impact",
        tool_meta=system_ai_tools.role_update_menus.__ai_tool_meta__,
    )

    result = await system_ai_tools._dry_run_role_update_menus(
        ctx,
        role_id=next_id(),
        menu_ids=[next_id()],
    )

    assert result.count == len(member_ids)


async def test_role_write_dry_runs_build_scalar_gateway_presentations(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def preview(*_args, **_kwargs):
        return SimpleNamespace(member_user_ids=(), snapshot={"version": "test"})

    for name in ("preview_update", "preview_update_menus", "preview_update_agents"):
        monkeypatch.setattr(system_ai_tools.role_management_service, name, preview)

    role_id = next_id()
    cases = [
        (
            system_ai_tools.role_update,
            system_ai_tools._dry_run_role_update,
            {"role_id": role_id, "role_desc": None},
        ),
        (
            system_ai_tools.role_update_menus,
            system_ai_tools._dry_run_role_update_menus,
            {"role_id": role_id, "menu_ids": []},
        ),
        (
            system_ai_tools.role_update_agents,
            system_ai_tools._dry_run_role_update_agents,
            {"role_id": role_id, "agent_ids": []},
        ),
    ]
    for tool, dry_run, args in cases:
        ctx = AiToolContext(
            user=MagicMock(user_id=next_id()),
            perms=set(tool.__ai_tool_meta__.required_perms),
            db=db_session,
            data_scope=DataScopeContext(None, None, []),
            trace_id=f"tr_{tool.__ai_tool_meta__.name}",
            tool_meta=tool.__ai_tool_meta__,
        )
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


async def test_role_update_preserves_explicit_null_for_nullable_description(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = None
    role = Role(
        role_id=next_id(),
        role_name="Nullable role",
        role_code=f"R_NULLABLE_{next_id()}",
        data_scope="5",
        status="1",
    )

    async def update(_db, _role_id, payload, **_kwargs):
        nonlocal captured
        captured = payload
        return role

    monkeypatch.setattr(system_ai_tools.role_management_service, "update", update)
    tool = system_ai_tools.role_update
    ctx = AiToolContext(
        user=MagicMock(user_id=next_id()),
        perms=set(tool.__ai_tool_meta__.required_perms),
        db=db_session,
        data_scope=DataScopeContext(None, None, []),
        trace_id="tr_role_nullable_clear",
        tool_meta=tool.__ai_tool_meta__,
        approved_business_snapshot={"version": "test"},
    )

    await tool(ctx, role_id=role.role_id, role_desc=None)

    assert captured is not None
    assert captured.model_fields_set == {"role_desc"}
    assert captured.role_desc is None
