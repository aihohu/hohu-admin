import importlib.util
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint

from app.db.base import role_depts, role_menus, user_depts, user_roles
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "e9f0a1b2c3d4_scope_system_tenant_aggregates.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("plan2_tenant_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_unique(table, columns: tuple[str, ...]) -> bool:
    return any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == columns
        for constraint in table.constraints
    )


def _has_composite_fk(table, local: tuple[str, ...], remote: tuple[str, ...]) -> bool:
    for constraint in table.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        if tuple(constraint.columns.keys()) != local:
            continue
        if tuple(element.target_fullname for element in constraint.elements) == remote:
            return True
    return False


def test_plan2_models_use_tenant_composite_uniques_and_same_tenant_relations():
    assert _has_unique(User.__table__, ("tenant_id", "user_id"))
    assert _has_unique(User.__table__, ("tenant_id", "user_name"))
    assert _has_unique(Role.__table__, ("tenant_id", "role_id"))
    assert _has_unique(Role.__table__, ("tenant_id", "role_code"))
    assert _has_unique(Dept.__table__, ("tenant_id", "dept_id"))
    assert _has_unique(Menu.__table__, ("tenant_id", "menu_id"))
    assert _has_composite_fk(
        Dept.__table__,
        ("tenant_id", "parent_id"),
        ("sys_dept.tenant_id", "sys_dept.dept_id"),
    )
    assert _has_composite_fk(
        Menu.__table__,
        ("tenant_id", "parent_id"),
        ("sys_menu.tenant_id", "sys_menu.menu_id"),
    )


def test_plan2_associations_freeze_tenant_in_primary_key_and_foreign_keys():
    cases = (
        (user_roles, "user_id", "sys_user.user_id", "role_id", "sys_role.role_id"),
        (user_depts, "user_id", "sys_user.user_id", "dept_id", "sys_dept.dept_id"),
        (role_menus, "role_id", "sys_role.role_id", "menu_id", "sys_menu.menu_id"),
        (role_depts, "role_id", "sys_role.role_id", "dept_id", "sys_dept.dept_id"),
        (
            RoleAiAgent.__table__,
            "role_id",
            "sys_role.role_id",
            "agent_id",
            "ai_agent.agent_id",
        ),
    )
    for table, left, left_remote, right, right_remote in cases:
        primary_key = next(
            constraint
            for constraint in table.constraints
            if isinstance(constraint, PrimaryKeyConstraint)
        )
        assert tuple(primary_key.columns.keys())[0] == "tenant_id", table.name
        assert _has_composite_fk(
            table,
            ("tenant_id", left),
            (
                f"{left_remote.rsplit('.', maxsplit=1)[0]}.tenant_id",
                left_remote,
            ),
        ), table.name
        if table is RoleAiAgent.__table__:
            assert any(
                element.target_fullname == right_remote
                for constraint in table.foreign_key_constraints
                for element in constraint.elements
            )
        else:
            assert _has_composite_fk(
                table,
                ("tenant_id", right),
                (
                    f"{right_remote.rsplit('.', maxsplit=1)[0]}.tenant_id",
                    right_remote,
                ),
            ), table.name


def test_role_agent_ownership_is_derived_from_the_composite_role_fk():
    """Do not declare an ORM-only tenant FK that the migration never creates."""
    direct_tenant_fks = [
        constraint
        for constraint in RoleAiAgent.__table__.foreign_key_constraints
        if tuple(constraint.columns.keys()) == ("tenant_id",)
    ]

    assert direct_tenant_fks == []
