"""Shared creation boundaries and isolated PostgreSQL rollback regression."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.modules.system.api import user as user_api
from app.modules.system.api.user import add_user
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserCreate, UserDeptItem
from tests.tenant_helpers import tenant_context


def _operations():
    return import_module("app.modules.system.service.user_operations_service")


def _input(*, explicit_roles: bool = False) -> UserCreate:
    values = {"role_ids": ["17"]} if explicit_roles else {}
    return UserCreate(
        user_name="sharedcreate",
        password="SharedPass123",
        status="1",
        dept_ids=[UserDeptItem(dept_id="23", is_primary=True)],
        **values,
    )


def _mock_writers(monkeypatch, module):
    created = SimpleNamespace(user_id=123)
    create = AsyncMock(return_value=created)
    role_permission = AsyncMock()
    department_permission = AsyncMock()
    roles = AsyncMock()
    departments = AsyncMock()
    monkeypatch.setattr(module.user_service, "create_user", create)
    monkeypatch.setattr(
        module.user_role_assignment_service,
        "ensure_create_permissions",
        role_permission,
    )
    monkeypatch.setattr(
        module.user_department_assignment_service,
        "ensure_create_permissions",
        department_permission,
    )
    monkeypatch.setattr(
        module.user_role_assignment_service, "assign_created_user_roles", roles
    )
    monkeypatch.setattr(
        module.user_department_assignment_service,
        "assign_created_user_departments",
        departments,
    )
    return SimpleNamespace(
        created=created,
        create=create,
        role_permission=role_permission,
        department_permission=department_permission,
        roles=roles,
        departments=departments,
    )


@pytest.mark.parametrize("explicit_roles", [False, True])
async def test_shared_create_preserves_assignment_inputs_and_transaction_owner(
    monkeypatch, explicit_roles
) -> None:
    module = _operations()
    writers = _mock_writers(monkeypatch, module)
    db = AsyncMock()
    tenant = tenant_context(actor_user_id=42)
    user_in = _input(explicit_roles=explicit_roles)

    result = await module.user_operations_service.create_user(
        db, user_in, actor_user_id=42, tenant=tenant
    )

    assert result is writers.created
    writers.role_permission.assert_awaited_once_with(
        db, actor_user_id=42, explicit_roles=explicit_roles, tenant=tenant
    )
    writers.department_permission.assert_awaited_once_with(
        db, actor_user_id=42, has_departments=True, tenant=tenant
    )
    writers.roles.assert_awaited_once_with(
        db,
        actor_user_id=42,
        target_user_id=123,
        role_ids=["17"] if explicit_roles else None,
        dept_ids=[23],
        tenant=tenant,
    )
    writers.departments.assert_awaited_once_with(
        db,
        actor_user_id=42,
        target_user_id=123,
        dept_assignments=[(23, True)],
        tenant=tenant,
    )
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    "permission_check", ["role_permission", "department_permission"]
)
async def test_shared_create_denied_before_inserting_user(
    monkeypatch, permission_check
) -> None:
    module = _operations()
    writers = _mock_writers(monkeypatch, module)
    error = AuthorizationException("拒绝创建", error_code="TEST_CREATE_DENIED")
    getattr(writers, permission_check).side_effect = error
    db = AsyncMock()

    with pytest.raises(AuthorizationException) as rejected:
        await module.user_operations_service.create_user(
            db, _input(), actor_user_id=42, tenant=tenant_context(actor_user_id=42)
        )

    assert rejected.value is error
    writers.create.assert_not_awaited()
    writers.roles.assert_not_awaited()
    writers.departments.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("failing_writer", ["roles", "departments"])
async def test_shared_create_propagates_assignment_failure_for_outer_rollback(
    monkeypatch, failing_writer
) -> None:
    module = _operations()
    writers = _mock_writers(monkeypatch, module)
    error = BusinessRuleException("拒绝赋权", error_code="TEST_ASSIGNMENT_DENIED")
    getattr(writers, failing_writer).side_effect = error
    db = AsyncMock()

    with pytest.raises(BusinessRuleException) as rejected:
        await module.user_operations_service.create_user(
            db, _input(), actor_user_id=42, tenant=tenant_context(actor_user_id=42)
        )

    assert rejected.value is error
    writers.create.assert_awaited_once()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


async def test_http_create_delegates_full_input_to_fixed_owner(monkeypatch) -> None:
    db = AsyncMock()
    tenant = tenant_context(actor_user_id=42)
    actor = SimpleNamespace(user_id=42)
    user_in = _input(explicit_roles=True)
    owner = AsyncMock()
    monkeypatch.setattr(user_api, "system_write", owner)
    response = await add_user(user_in=user_in, db=db, current_user=actor, tenant=tenant)
    owner.assert_awaited_once_with(
        db, actor, operation="user.create", payload=user_in, tenant=tenant
    )
    db.commit.assert_not_awaited()
    assert response.code == 200


async def test_http_create_does_not_commit_failed_domain_operation(monkeypatch) -> None:
    db = AsyncMock()
    owner = AsyncMock(
        side_effect=BusinessRuleException(
            "赋权失败", error_code="TEST_ASSIGNMENT_DENIED"
        )
    )
    monkeypatch.setattr(user_api, "system_write", owner)
    with pytest.raises(BusinessRuleException):
        await add_user(
            user_in=_input(),
            db=db,
            current_user=SimpleNamespace(user_id=42),
            tenant=tenant_context(actor_user_id=42),
        )
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()
    owner.assert_awaited_once()


@pytest.mark.parametrize("role_assignment", ["DEFAULT", "NONE"])
async def test_ai_create_delegates_restricted_input_and_keeps_presentation(
    monkeypatch, role_assignment
) -> None:
    module = _operations()
    ai = import_module("app.modules.system.ai_tools.user_management")
    db = AsyncMock()
    tenant = tenant_context(actor_user_id=42)
    ctx = SimpleNamespace(db=db, user=SimpleNamespace(user_id=42), tenant=tenant)
    role = (
        SimpleNamespace(role_id=17, role_name="默认用户", role_code="R_USER")
        if role_assignment == "DEFAULT"
        else None
    )
    monkeypatch.setattr(
        ai,
        "_load_ai_create_policy",
        AsyncMock(return_value=(None, role, "SharedPass123")),
    )
    created = SimpleNamespace(user_id=123, user_name="sharedcreate", status="1")
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(module.user_operations_service, "create_user", create)

    result = await ai.user_create(
        ctx,
        user_name="sharedcreate",
        role_assignment=ai.AiUserRoleAssignment(role_assignment),
    )

    create.assert_awaited_once()
    args, kwargs = create.await_args
    assert args[0] is db
    user_in = args[1]
    assert isinstance(user_in, UserCreate)
    assert user_in.role_ids == (None if role_assignment == "DEFAULT" else [])
    assert user_in.dept_ids == []
    assert kwargs == {"actor_user_id": 42, "tenant": tenant}
    assert result.ok
    assert result.ui.view_type == "detail_card"
    assert result.projection.subject_refs == ({"type": "user", "id": "123"},)
    assert result.data["roleAssignment"] == role_assignment.lower()
    assert "SharedPass123" not in repr(result)
    db.commit.assert_not_awaited()


async def test_ai_count_delegates_scope_and_canonical_filters(monkeypatch) -> None:
    module = _operations()
    ai = import_module("app.modules.system.ai_tools.analytics")
    db = AsyncMock()
    tenant = tenant_context(actor_user_id=42)
    scope_filters = [User.user_id.in_([123, 456])]
    ctx = SimpleNamespace(
        db=db,
        tenant=tenant,
        data_scope=SimpleNamespace(filters=scope_filters),
        tool_meta=ai.user_count.__ai_tool_meta__,
    )
    count = AsyncMock(return_value=7)
    monkeypatch.setattr(module.user_operations_service, "count_users", count)

    result = await ai.user_count(ctx, filters={"status": "1", "user_gender": "2"})

    count.assert_awaited_once_with(
        db,
        tenant=tenant,
        scope_filters=scope_filters,
        status="1",
        user_gender="2",
    )
    assert result.data == {"count": 7}
    assert result.projection.scope_bound is True
    assert result.ui.view_type == "plain_json"
    assert result.ui.view_data == {"count": 7}
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("failing_writer", ["roles", "departments"])
async def test_assignment_failure_rolls_back_insert_with_callers_transaction(
    db_session, monkeypatch, failing_writer
) -> None:
    module = _operations()
    actor = await db_session.scalar(
        select(User).where(User.tenant_id == 0, User.user_name == "admin")
    )
    assert actor is not None
    tenant = tenant_context(actor_user_id=actor.user_id)
    writer = (
        module.user_role_assignment_service
        if failing_writer == "roles"
        else module.user_department_assignment_service
    )
    method = (
        "assign_created_user_roles"
        if failing_writer == "roles"
        else "assign_created_user_departments"
    )
    monkeypatch.setattr(
        writer,
        method,
        AsyncMock(
            side_effect=BusinessRuleException(
                "赋权失败", error_code="TEST_ASSIGNMENT_DENIED"
            )
        ),
    )
    user_in = UserCreate(
        user_name="rollbackshared", password="SharedPass123", status="1"
    )

    with pytest.raises(BusinessRuleException) as rejected:
        async with db_session.begin_nested():
            await module.user_operations_service.create_user(
                db_session, user_in, actor_user_id=actor.user_id, tenant=tenant
            )

    assert rejected.value.error_code == "TEST_ASSIGNMENT_DENIED"
    assert (
        await db_session.scalar(
            select(User.user_id).where(
                User.tenant_id == tenant.tenant_id,
                User.user_name == user_in.user_name,
            )
        )
        is None
    )
