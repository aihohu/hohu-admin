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


def test_plan2_migration_is_a_linear_shadow_revision():
    migration = _load_migration()

    assert migration.revision == "e9f0a1b2c3d4"
    assert migration.down_revision == "d8e9f0a1b2c3"
    assert migration.PLAN2_TENANT_TABLES
    assert migration.PLAN2_ASSOCIATION_TABLES


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


def test_plan2_replaces_legacy_relationship_and_lookup_indexes(monkeypatch):
    migration = _load_migration()
    dropped_constraints: list[tuple[str, str, str | None]] = []
    dropped_indexes: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table_name, type_=None: dropped_constraints.append(
            (name, table_name, type_)
        ),
    )
    monkeypatch.setattr(
        migration.op, "create_foreign_key", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, table_name=None: dropped_indexes.append((name, table_name)),
    )
    monkeypatch.setattr(
        migration.op, "create_check_constraint", lambda *_args, **_kwargs: None
    )

    migration._create_domain_relationships()
    migration._create_indexes_and_checks()

    assert (
        "sys_user_import_batch_log_batch_id_fkey",
        "sys_user_import_batch_log",
        "foreignkey",
    ) in dropped_constraints
    assert {
        ("ix_sys_job_log_status_start_time", "sys_job_log"),
        ("ix_login_log_login_time", "sys_login_log"),
        ("ix_operation_log_create_time", "sys_operation_log"),
        ("ix_operation_log_user_id", "sys_operation_log"),
    } <= set(dropped_indexes)
