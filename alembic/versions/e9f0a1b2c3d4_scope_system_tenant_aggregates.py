"""Scope System aggregates, associations, jobs, and audit by tenant.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLAN2_TENANT_TABLES = (
    "sys_user",
    "sys_role",
    "sys_dept",
    "sys_menu",
    "sys_config",
    "sys_dict_type",
    "sys_dict_data",
    "sys_file",
    "sys_data_scope_demo",
    "sys_user_import_batch",
    "sys_user_import_batch_log",
    "sys_user_export_task",
    "sys_job",
    "sys_job_log",
    "sys_operation_log",
    "sys_login_log",
)

PLAN2_ASSOCIATION_TABLES = (
    "sys_user_role",
    "sys_user_dept",
    "sys_role_menu",
    "sys_role_dept",
    "role_ai_agent",
)

_LEGACY_UNSCOPED_INDEXES = (
    ("ix_sys_job_log_status_start_time", "sys_job_log", ["status", "start_time"]),
    ("ix_login_log_login_time", "sys_login_log", ["login_time"]),
    (
        "ix_operation_log_create_time",
        "sys_operation_log",
        ["create_time"],
    ),
    ("ix_operation_log_user_id", "sys_operation_log", ["user_id"]),
)

_TENANT_COLUMN_COMMENTS = {
    **{
        table_name: "租户ID；必须由可信 TenantContext 显式写入"
        for table_name in (
            "sys_user",
            "sys_role",
            "sys_dept",
            "sys_menu",
            "sys_config",
            "sys_dict_type",
            "sys_dict_data",
            "sys_file",
            "sys_data_scope_demo",
            "sys_user_import_batch",
            "sys_user_import_batch_log",
            "sys_user_export_task",
            "sys_job",
            "sys_job_log",
            "sys_user_role",
            "sys_user_dept",
            "sys_role_menu",
            "sys_role_dept",
        )
    },
    "role_ai_agent": "租户ID；Agent 本身保持 platform-global",
    "sys_operation_log": "租户审计归属",
}

_ROOT_SHADOW_TABLES = (
    "sys_role",
    "sys_dept",
    "sys_menu",
    "sys_config",
    "sys_dict_type",
    "sys_dict_data",
    "sys_data_scope_demo",
    "sys_job",
)

_DERIVED_BACKFILLS = {
    "sys_user_import_batch": """
        UPDATE sys_user_import_batch AS target
        SET tenant_id = parent.tenant_id
        FROM sys_user AS parent
        WHERE parent.user_id = target.operator_id
          AND target.tenant_id IS NULL
    """,
    "sys_user_import_batch_log": """
        UPDATE sys_user_import_batch_log AS target
        SET tenant_id = parent.tenant_id
        FROM sys_user_import_batch AS parent
        WHERE parent.batch_id = target.batch_id
          AND target.tenant_id IS NULL
    """,
    "sys_user_export_task": """
        UPDATE sys_user_export_task AS target
        SET tenant_id = parent.tenant_id
        FROM sys_user AS parent
        WHERE parent.user_id = target.operator_id
          AND target.tenant_id IS NULL
    """,
    "sys_job_log": """
        UPDATE sys_job_log AS target
        SET tenant_id = parent.tenant_id
        FROM sys_job AS parent
        WHERE parent.job_id = target.job_id
          AND target.tenant_id IS NULL
    """,
    "sys_operation_log": """
        UPDATE sys_operation_log AS target
        SET tenant_id = COALESCE(
            (SELECT parent.tenant_id
             FROM sys_user AS parent
             WHERE parent.user_id = target.user_id),
            0
        )
        WHERE target.tenant_id IS NULL
    """,
}

_ASSOCIATION_BACKFILLS = {
    "sys_user_role": """
        UPDATE sys_user_role AS target
        SET tenant_id = parent.tenant_id
        FROM sys_user AS parent
        WHERE parent.user_id = target.user_id
          AND target.tenant_id IS NULL
    """,
    "sys_user_dept": """
        UPDATE sys_user_dept AS target
        SET tenant_id = parent.tenant_id
        FROM sys_user AS parent
        WHERE parent.user_id = target.user_id
          AND target.tenant_id IS NULL
    """,
    "sys_role_menu": """
        UPDATE sys_role_menu AS target
        SET tenant_id = parent.tenant_id
        FROM sys_role AS parent
        WHERE parent.role_id = target.role_id
          AND target.tenant_id IS NULL
    """,
    "sys_role_dept": """
        UPDATE sys_role_dept AS target
        SET tenant_id = parent.tenant_id
        FROM sys_role AS parent
        WHERE parent.role_id = target.role_id
          AND target.tenant_id IS NULL
    """,
    "role_ai_agent": """
        UPDATE role_ai_agent AS target
        SET tenant_id = parent.tenant_id
        FROM sys_role AS parent
        WHERE parent.role_id = target.role_id
          AND target.tenant_id IS NULL
    """,
}


def _scalar_count(statement: str) -> int:
    return int(op.get_bind().execute(sa.text(statement)).scalar_one())


def _require_zero(statement: str, *, error_code: str) -> None:
    count = _scalar_count(statement)
    if count:
        raise RuntimeError(f"{error_code}: {count} row(s)")


def _add_required_shadow(table_name: str, backfill: str) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=True,
            comment="租户ID；Plan 2 shadow 迁移",
        ),
    )
    op.execute(sa.text(backfill))
    _require_zero(
        f"SELECT count(*) FROM {table_name} WHERE tenant_id IS NULL",
        error_code=f"TENANT_BACKFILL_NULL:{table_name}",
    )
    op.alter_column(
        table_name,
        "tenant_id",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=sa.text("0"),
        existing_comment="租户ID；Plan 2 shadow 迁移",
    )


def _create_tenant_fk(table_name: str) -> None:
    op.create_foreign_key(
        f"fk_{table_name}_tenant_id_sys_tenant",
        table_name,
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )


def _create_tenant_identity_unique(
    table_name: str, id_column: str, constraint_name: str
) -> None:
    op.create_unique_constraint(
        constraint_name,
        table_name,
        ["tenant_id", id_column],
    )


def _add_shadow_columns() -> None:
    for table_name in _ROOT_SHADOW_TABLES:
        _add_required_shadow(
            table_name,
            f"UPDATE {table_name} SET tenant_id = 0 WHERE tenant_id IS NULL",
        )

    for table_name, statement in _DERIVED_BACKFILLS.items():
        _add_required_shadow(table_name, statement)

    for table_name, statement in _ASSOCIATION_BACKFILLS.items():
        _add_required_shadow(table_name, statement)

    op.add_column(
        "sys_login_log",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=True,
            comment="已定位租户；unresolved 登录失败保持 NULL",
        ),
    )
    op.add_column(
        "sys_login_log",
        sa.Column(
            "audit_scope",
            sa.String(length=16),
            nullable=True,
            comment="tenant/platform/unresolved",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE sys_login_log AS target
            SET tenant_id = parent.tenant_id,
                audit_scope = 'tenant'
            FROM sys_user AS parent
            WHERE parent.user_id = target.user_id
              AND target.user_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE sys_login_log
            SET tenant_id = 0,
                audit_scope = 'tenant'
            WHERE audit_scope IS NULL
              AND user_id IS NOT NULL
              AND status = '1'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE sys_login_log
            SET tenant_id = NULL,
                audit_scope = 'unresolved'
            WHERE audit_scope IS NULL
            """
        )
    )
    op.alter_column(
        "sys_login_log",
        "audit_scope",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="unresolved",
        existing_comment="tenant/platform/unresolved",
    )

    op.add_column(
        "sys_operation_log",
        sa.Column(
            "audit_scope",
            sa.String(length=16),
            nullable=False,
            server_default="tenant",
            comment="tenant/platform",
        ),
    )


def _check_backfill_integrity() -> None:
    # Legacy menu seeds used 0 as a root sentinel. Canonical tree roots use NULL;
    # keeping 0 would be indistinguishable from a missing parent under a real FK.
    op.execute(sa.text("UPDATE sys_menu SET parent_id = NULL WHERE parent_id = 0"))
    checks = {
        "TENANT_CROSS_LINK:sys_user_role": """
            SELECT count(*) FROM sys_user_role link
            JOIN sys_user usr ON usr.user_id = link.user_id
            JOIN sys_role role ON role.role_id = link.role_id
            WHERE link.tenant_id <> usr.tenant_id
               OR link.tenant_id <> role.tenant_id
        """,
        "TENANT_CROSS_LINK:sys_user_dept": """
            SELECT count(*) FROM sys_user_dept link
            JOIN sys_user usr ON usr.user_id = link.user_id
            JOIN sys_dept dept ON dept.dept_id = link.dept_id
            WHERE link.tenant_id <> usr.tenant_id
               OR link.tenant_id <> dept.tenant_id
        """,
        "TENANT_CROSS_LINK:sys_role_menu": """
            SELECT count(*) FROM sys_role_menu link
            JOIN sys_role role ON role.role_id = link.role_id
            JOIN sys_menu menu ON menu.menu_id = link.menu_id
            WHERE link.tenant_id <> role.tenant_id
               OR link.tenant_id <> menu.tenant_id
        """,
        "TENANT_CROSS_LINK:sys_role_dept": """
            SELECT count(*) FROM sys_role_dept link
            JOIN sys_role role ON role.role_id = link.role_id
            JOIN sys_dept dept ON dept.dept_id = link.dept_id
            WHERE link.tenant_id <> role.tenant_id
               OR link.tenant_id <> dept.tenant_id
        """,
        "TENANT_CROSS_LINK:role_ai_agent": """
            SELECT count(*) FROM role_ai_agent link
            JOIN sys_role role ON role.role_id = link.role_id
            WHERE link.tenant_id <> role.tenant_id
        """,
        "TENANT_ORPHAN:sys_dept.parent_id": """
            SELECT count(*) FROM sys_dept child
            LEFT JOIN sys_dept parent
              ON parent.tenant_id = child.tenant_id
             AND parent.dept_id = child.parent_id
            WHERE child.parent_id IS NOT NULL AND parent.dept_id IS NULL
        """,
        "TENANT_ORPHAN:sys_menu.parent_id": """
            SELECT count(*) FROM sys_menu child
            LEFT JOIN sys_menu parent
              ON parent.tenant_id = child.tenant_id
             AND parent.menu_id = child.parent_id
            WHERE child.parent_id IS NOT NULL AND parent.menu_id IS NULL
        """,
    }
    for error_code, statement in checks.items():
        _require_zero(statement, error_code=error_code)


def _replace_business_uniques() -> None:
    op.drop_index("ix_sys_user_tenant_user_name", table_name="sys_user")
    op.drop_index("ix_sys_user_user_name", table_name="sys_user")
    op.drop_constraint("uq_sys_user_employee_no", "sys_user", type_="unique")
    op.create_unique_constraint(
        "uq_sys_user_tenant_user_name",
        "sys_user",
        ["tenant_id", "user_name"],
    )
    op.create_unique_constraint(
        "uq_sys_user_tenant_employee_no",
        "sys_user",
        ["tenant_id", "employee_no"],
    )

    replacements = (
        (
            "sys_role",
            "sys_role_role_name_key",
            "uq_sys_role_tenant_role_name",
            ["tenant_id", "role_name"],
        ),
        (
            "sys_role",
            "sys_role_role_code_key",
            "uq_sys_role_tenant_role_code",
            ["tenant_id", "role_code"],
        ),
        (
            "sys_config",
            "sys_config_config_key_key",
            "uq_sys_config_tenant_config_key",
            ["tenant_id", "config_key"],
        ),
        (
            "sys_dict_type",
            "sys_dict_type_dict_name_key",
            "uq_sys_dict_type_tenant_name",
            ["tenant_id", "dict_name"],
        ),
        (
            "sys_dict_type",
            "sys_dict_type_dict_type_key",
            "uq_sys_dict_type_tenant_type",
            ["tenant_id", "dict_type"],
        ),
        (
            "sys_job",
            "sys_job_job_key_key",
            "uq_sys_job_tenant_job_key",
            ["tenant_id", "job_key"],
        ),
    )
    for table_name, old_name, new_name, columns in replacements:
        op.drop_constraint(old_name, table_name, type_="unique")
        op.create_unique_constraint(new_name, table_name, columns)

    op.drop_index(
        "ix_sys_user_import_batch_preview_token",
        table_name="sys_user_import_batch",
    )
    op.create_unique_constraint(
        "uq_sys_user_import_batch_tenant_preview_token",
        "sys_user_import_batch",
        ["tenant_id", "preview_token"],
    )


def _create_root_constraints() -> None:
    identity_uniques = (
        ("sys_user", "user_id", "uq_sys_user_tenant_user_id"),
        ("sys_role", "role_id", "uq_sys_role_tenant_role_id"),
        ("sys_dept", "dept_id", "uq_sys_dept_tenant_dept_id"),
        ("sys_menu", "menu_id", "uq_sys_menu_tenant_menu_id"),
        ("sys_config", "config_id", "uq_sys_config_tenant_config_id"),
        (
            "sys_dict_type",
            "dict_type_id",
            "uq_sys_dict_type_tenant_type_id",
        ),
        ("sys_dict_data", "dict_code", "uq_sys_dict_data_tenant_code"),
        ("sys_file", "file_id", "uq_sys_file_tenant_file_id"),
        (
            "sys_data_scope_demo",
            "demo_id",
            "uq_sys_data_scope_demo_tenant_demo_id",
        ),
        (
            "sys_user_import_batch",
            "batch_id",
            "uq_sys_user_import_batch_tenant_batch",
        ),
        (
            "sys_user_import_batch_log",
            "log_id",
            "uq_sys_user_import_log_tenant_log",
        ),
        (
            "sys_user_export_task",
            "export_id",
            "uq_sys_user_export_tenant_export",
        ),
        ("sys_job", "job_id", "uq_sys_job_tenant_job_id"),
        ("sys_job_log", "job_log_id", "uq_sys_job_log_tenant_log_id"),
        (
            "sys_operation_log",
            "operation_log_id",
            "uq_sys_operation_log_tenant_log_id",
        ),
        (
            "sys_login_log",
            "login_log_id",
            "uq_sys_login_log_tenant_log_id",
        ),
    )
    for table_name, id_column, constraint_name in identity_uniques:
        _create_tenant_identity_unique(table_name, id_column, constraint_name)

    for table_name in PLAN2_TENANT_TABLES:
        if table_name != "sys_user":
            _create_tenant_fk(table_name)


def _create_domain_relationships() -> None:
    # Replace the legacy globally keyed FK. Keeping both constraints would make
    # ORM/schema parity drift and obscure which relationship is authoritative.
    op.drop_constraint(
        "sys_user_import_batch_log_batch_id_fkey",
        "sys_user_import_batch_log",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_sys_dept_tenant_parent",
        "sys_dept",
        "sys_dept",
        ["tenant_id", "parent_id"],
        ["tenant_id", "dept_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sys_menu_tenant_parent",
        "sys_menu",
        "sys_menu",
        ["tenant_id", "parent_id"],
        ["tenant_id", "menu_id"],
        ondelete="RESTRICT",
    )
    relationships = (
        (
            "fk_sys_dict_data_tenant_type",
            "sys_dict_data",
            "sys_dict_type",
            ["tenant_id", "dict_type"],
            ["tenant_id", "dict_type"],
            "RESTRICT",
        ),
        (
            "fk_sys_file_tenant_owner",
            "sys_file",
            "sys_user",
            ["tenant_id", "owner_user_id"],
            ["tenant_id", "user_id"],
            "RESTRICT",
        ),
        (
            "fk_sys_data_scope_demo_tenant_dept",
            "sys_data_scope_demo",
            "sys_dept",
            ["tenant_id", "dept_id"],
            ["tenant_id", "dept_id"],
            "RESTRICT",
        ),
        (
            "fk_sys_data_scope_demo_tenant_creator",
            "sys_data_scope_demo",
            "sys_user",
            ["tenant_id", "create_by"],
            ["tenant_id", "user_id"],
            "RESTRICT",
        ),
        (
            "fk_sys_user_import_batch_tenant_operator",
            "sys_user_import_batch",
            "sys_user",
            ["tenant_id", "operator_id"],
            ["tenant_id", "user_id"],
            "RESTRICT",
        ),
        (
            "fk_sys_user_import_log_tenant_batch",
            "sys_user_import_batch_log",
            "sys_user_import_batch",
            ["tenant_id", "batch_id"],
            ["tenant_id", "batch_id"],
            "CASCADE",
        ),
        (
            "fk_sys_user_import_log_tenant_operator",
            "sys_user_import_batch_log",
            "sys_user",
            ["tenant_id", "operator_id"],
            ["tenant_id", "user_id"],
            "RESTRICT",
        ),
        (
            "fk_sys_user_export_tenant_operator",
            "sys_user_export_task",
            "sys_user",
            ["tenant_id", "operator_id"],
            ["tenant_id", "user_id"],
            "RESTRICT",
        ),
        (
            "fk_sys_job_log_tenant_job",
            "sys_job_log",
            "sys_job",
            ["tenant_id", "job_id"],
            ["tenant_id", "job_id"],
            "CASCADE",
        ),
    )
    for name, source, target, local, remote, ondelete in relationships:
        op.create_foreign_key(
            name, source, target, local, remote, ondelete=ondelete
        )


def _replace_association_constraints() -> None:
    definitions = (
        (
            "sys_user_role",
            ("user_id", "sys_user", "user_id"),
            ("role_id", "sys_role", "role_id"),
        ),
        (
            "sys_user_dept",
            ("user_id", "sys_user", "user_id"),
            ("dept_id", "sys_dept", "dept_id"),
        ),
        (
            "sys_role_menu",
            ("role_id", "sys_role", "role_id"),
            ("menu_id", "sys_menu", "menu_id"),
        ),
        (
            "sys_role_dept",
            ("role_id", "sys_role", "role_id"),
            ("dept_id", "sys_dept", "dept_id"),
        ),
    )
    for table_name, left, right in definitions:
        op.drop_constraint(f"{table_name}_pkey", table_name, type_="primary")
        op.drop_constraint(
            f"{table_name}_{left[0]}_fkey", table_name, type_="foreignkey"
        )
        op.drop_constraint(
            f"{table_name}_{right[0]}_fkey", table_name, type_="foreignkey"
        )
        op.create_primary_key(
            f"{table_name}_pkey",
            table_name,
            ["tenant_id", left[0], right[0]],
        )
        op.create_foreign_key(
            f"fk_{table_name}_tenant_{left[0]}",
            table_name,
            left[1],
            ["tenant_id", left[0]],
            ["tenant_id", left[2]],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            f"fk_{table_name}_tenant_{right[0]}",
            table_name,
            right[1],
            ["tenant_id", right[0]],
            ["tenant_id", right[2]],
            ondelete="CASCADE",
        )

    op.drop_constraint("role_ai_agent_pkey", "role_ai_agent", type_="primary")
    op.drop_constraint(
        "role_ai_agent_role_id_fkey", "role_ai_agent", type_="foreignkey"
    )
    op.create_primary_key(
        "role_ai_agent_pkey",
        "role_ai_agent",
        ["tenant_id", "role_id", "agent_id"],
    )
    op.create_foreign_key(
        "fk_role_ai_agent_tenant_role",
        "role_ai_agent",
        "sys_role",
        ["tenant_id", "role_id"],
        ["tenant_id", "role_id"],
        ondelete="CASCADE",
    )


def _create_indexes_and_checks() -> None:
    for name, table_name, _columns in _LEGACY_UNSCOPED_INDEXES:
        op.drop_index(name, table_name=table_name)

    indexes = (
        ("ix_sys_user_tenant_status", "sys_user", ["tenant_id", "status"]),
        ("ix_sys_role_tenant_status", "sys_role", ["tenant_id", "status"]),
        ("ix_sys_dept_tenant_parent", "sys_dept", ["tenant_id", "parent_id"]),
        ("ix_sys_dept_tenant_status", "sys_dept", ["tenant_id", "status"]),
        ("ix_sys_menu_tenant_parent", "sys_menu", ["tenant_id", "parent_id"]),
        ("ix_sys_menu_tenant_status", "sys_menu", ["tenant_id", "status"]),
        (
            "ix_sys_config_tenant_group",
            "sys_config",
            ["tenant_id", "config_group"],
        ),
        (
            "ix_sys_dict_type_tenant_status",
            "sys_dict_type",
            ["tenant_id", "status"],
        ),
        (
            "ix_sys_dict_data_tenant_type",
            "sys_dict_data",
            ["tenant_id", "dict_type"],
        ),
        ("ix_sys_file_tenant_owner", "sys_file", ["tenant_id", "owner_user_id"]),
        ("ix_sys_file_tenant_deleted", "sys_file", ["tenant_id", "del_flag"]),
        (
            "ix_sys_data_scope_demo_tenant_dept",
            "sys_data_scope_demo",
            ["tenant_id", "dept_id"],
        ),
        (
            "ix_sys_data_scope_demo_tenant_creator",
            "sys_data_scope_demo",
            ["tenant_id", "create_by"],
        ),
        (
            "ix_sys_user_import_batch_tenant_status",
            "sys_user_import_batch",
            ["tenant_id", "status"],
        ),
        (
            "ix_sys_user_import_log_tenant_batch",
            "sys_user_import_batch_log",
            ["tenant_id", "batch_id"],
        ),
        (
            "ix_sys_user_export_tenant_status",
            "sys_user_export_task",
            ["tenant_id", "status"],
        ),
        ("ix_sys_job_tenant_status", "sys_job", ["tenant_id", "status"]),
        (
            "ix_sys_job_log_tenant_status_start",
            "sys_job_log",
            ["tenant_id", "status", "start_time"],
        ),
        (
            "ix_operation_log_tenant_time",
            "sys_operation_log",
            ["tenant_id", "create_time"],
        ),
        (
            "ix_operation_log_tenant_user",
            "sys_operation_log",
            ["tenant_id", "user_id"],
        ),
        (
            "ix_login_log_tenant_time",
            "sys_login_log",
            ["tenant_id", "login_time"],
        ),
        (
            "ix_login_log_scope_time",
            "sys_login_log",
            ["audit_scope", "login_time"],
        ),
        (
            "ix_sys_user_role_tenant_role",
            "sys_user_role",
            ["tenant_id", "role_id"],
        ),
        (
            "ix_sys_user_dept_tenant_dept",
            "sys_user_dept",
            ["tenant_id", "dept_id"],
        ),
        (
            "ix_sys_role_menu_tenant_menu",
            "sys_role_menu",
            ["tenant_id", "menu_id"],
        ),
        (
            "ix_sys_role_dept_tenant_dept",
            "sys_role_dept",
            ["tenant_id", "dept_id"],
        ),
        (
            "ix_role_ai_agent_tenant_agent",
            "role_ai_agent",
            ["tenant_id", "agent_id"],
        ),
    )
    for name, table_name, columns in indexes:
        op.create_index(name, table_name, columns, unique=False)

    op.create_check_constraint(
        "ck_sys_operation_log_audit_scope",
        "sys_operation_log",
        "audit_scope IN ('tenant', 'platform')",
    )
    op.create_check_constraint(
        "ck_sys_login_log_audit_scope",
        "sys_login_log",
        "(audit_scope = 'tenant' AND tenant_id IS NOT NULL) "
        "OR (audit_scope = 'unresolved' AND tenant_id IS NULL) "
        "OR audit_scope = 'platform'",
    )


def _drop_runtime_tenant_defaults() -> None:
    """Make every tenant-owned writer fail closed when tenant is omitted."""
    for table_name in (
        *(table for table in PLAN2_TENANT_TABLES if table != "sys_login_log"),
        *PLAN2_ASSOCIATION_TABLES,
    ):
        op.alter_column(
            table_name,
            "tenant_id",
            existing_type=sa.BigInteger(),
            server_default=None,
        )


def _finalize_column_comments() -> None:
    """Replace temporary migration comments with the canonical ORM contract."""
    for table_name, comment in _TENANT_COLUMN_COMMENTS.items():
        op.alter_column(
            table_name,
            "tenant_id",
            existing_type=sa.BigInteger(),
            comment=comment,
        )
    op.alter_column(
        "sys_operation_log",
        "audit_scope",
        existing_type=sa.String(length=16),
        comment="审计作用域",
    )
    transfer_comments = {
        ("sys_user_export_task", "reason"): (
            "导出的业务理由，与导入批次理由语义一致"
        ),
        ("sys_user_import_batch", "records_hash"): (
            "records 序列化后的 sha256，执行时比对以防预览后字段被修改"
        ),
        ("sys_user_import_batch", "preview_token"): (
            "执行时用于反查批次并保证幂等"
        ),
        ("sys_user_import_batch", "file_storage_key"): (
            "原始上传文件的 storage key"
        ),
        ("sys_user_import_batch", "status"): "导入批次状态",
    }
    for (table_name, column_name), comment in transfer_comments.items():
        op.alter_column(table_name, column_name, comment=comment)


def upgrade() -> None:
    """Backfill Plan 2 ownership and add tenant-safe integrity constraints."""
    _add_shadow_columns()
    _check_backfill_integrity()
    _replace_business_uniques()
    _create_root_constraints()
    _create_domain_relationships()
    _replace_association_constraints()
    _create_indexes_and_checks()
    _drop_runtime_tenant_defaults()
    _finalize_column_comments()


def downgrade() -> None:
    """Restore the M1 single-tenant schema."""
    for table_name in ("sys_user", "sys_file"):
        op.alter_column(
            table_name,
            "tenant_id",
            existing_type=sa.BigInteger(),
            server_default=sa.text("0"),
            comment=(
                "租户ID；M1 兼容期由服务端 Default Tenant 回填"
                if table_name == "sys_user"
                else "租户ID（当前单租户固定为0）"
            ),
        )

    legacy_transfer_comments = {
        ("sys_user_export_task", "reason"): (
            "导出操作的理由，与 import batch.reason 对称"
        ),
        ("sys_user_import_batch", "records_hash"): (
            "records 序列化后的 sha256，用于执行前一致性校验"
        ),
        ("sys_user_import_batch", "preview_token"): (
            "execute 时反查 batch 并保证幂等控制"
        ),
        ("sys_user_import_batch", "file_storage_key"): (
            "原始上传文件的 storage_key"
        ),
        ("sys_user_import_batch", "status"): "导入批次状态机",
    }
    for (table_name, column_name), comment in legacy_transfer_comments.items():
        op.alter_column(table_name, column_name, comment=comment)

    op.drop_constraint(
        "ck_sys_login_log_audit_scope", "sys_login_log", type_="check"
    )
    op.drop_constraint(
        "ck_sys_operation_log_audit_scope", "sys_operation_log", type_="check"
    )

    for name, table_name in reversed(
        (
            ("ix_sys_user_tenant_status", "sys_user"),
            ("ix_sys_role_tenant_status", "sys_role"),
            ("ix_sys_dept_tenant_parent", "sys_dept"),
            ("ix_sys_dept_tenant_status", "sys_dept"),
            ("ix_sys_menu_tenant_parent", "sys_menu"),
            ("ix_sys_menu_tenant_status", "sys_menu"),
            ("ix_sys_config_tenant_group", "sys_config"),
            ("ix_sys_dict_type_tenant_status", "sys_dict_type"),
            ("ix_sys_dict_data_tenant_type", "sys_dict_data"),
            ("ix_sys_file_tenant_owner", "sys_file"),
            ("ix_sys_file_tenant_deleted", "sys_file"),
            ("ix_sys_data_scope_demo_tenant_dept", "sys_data_scope_demo"),
            ("ix_sys_data_scope_demo_tenant_creator", "sys_data_scope_demo"),
            ("ix_sys_user_import_batch_tenant_status", "sys_user_import_batch"),
            ("ix_sys_user_import_log_tenant_batch", "sys_user_import_batch_log"),
            ("ix_sys_user_export_tenant_status", "sys_user_export_task"),
            ("ix_sys_job_tenant_status", "sys_job"),
            ("ix_sys_job_log_tenant_status_start", "sys_job_log"),
            ("ix_operation_log_tenant_time", "sys_operation_log"),
            ("ix_operation_log_tenant_user", "sys_operation_log"),
            ("ix_login_log_tenant_time", "sys_login_log"),
            ("ix_login_log_scope_time", "sys_login_log"),
            ("ix_sys_user_role_tenant_role", "sys_user_role"),
            ("ix_sys_user_dept_tenant_dept", "sys_user_dept"),
            ("ix_sys_role_menu_tenant_menu", "sys_role_menu"),
            ("ix_sys_role_dept_tenant_dept", "sys_role_dept"),
            ("ix_role_ai_agent_tenant_agent", "role_ai_agent"),
        )
    ):
        op.drop_index(name, table_name=table_name)

    for name, table_name, columns in _LEGACY_UNSCOPED_INDEXES:
        op.create_index(name, table_name, columns, unique=False)

    op.drop_constraint(
        "fk_role_ai_agent_tenant_role", "role_ai_agent", type_="foreignkey"
    )
    op.drop_constraint("role_ai_agent_pkey", "role_ai_agent", type_="primary")
    op.create_primary_key(
        "role_ai_agent_pkey", "role_ai_agent", ["role_id", "agent_id"]
    )
    op.create_foreign_key(
        "role_ai_agent_role_id_fkey",
        "role_ai_agent",
        "sys_role",
        ["role_id"],
        ["role_id"],
        ondelete="CASCADE",
    )

    definitions = (
        (
            "sys_user_role",
            ("user_id", "sys_user", "user_id"),
            ("role_id", "sys_role", "role_id"),
        ),
        (
            "sys_user_dept",
            ("user_id", "sys_user", "user_id"),
            ("dept_id", "sys_dept", "dept_id"),
        ),
        (
            "sys_role_menu",
            ("role_id", "sys_role", "role_id"),
            ("menu_id", "sys_menu", "menu_id"),
        ),
        (
            "sys_role_dept",
            ("role_id", "sys_role", "role_id"),
            ("dept_id", "sys_dept", "dept_id"),
        ),
    )
    for table_name, left, right in reversed(definitions):
        op.drop_constraint(
            f"fk_{table_name}_tenant_{right[0]}", table_name, type_="foreignkey"
        )
        op.drop_constraint(
            f"fk_{table_name}_tenant_{left[0]}", table_name, type_="foreignkey"
        )
        op.drop_constraint(f"{table_name}_pkey", table_name, type_="primary")
        op.create_primary_key(
            f"{table_name}_pkey", table_name, [left[0], right[0]]
        )
        op.create_foreign_key(
            f"{table_name}_{left[0]}_fkey",
            table_name,
            left[1],
            [left[0]],
            [left[2]],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            f"{table_name}_{right[0]}_fkey",
            table_name,
            right[1],
            [right[0]],
            [right[2]],
            ondelete="CASCADE",
        )

    relationship_names = (
        ("fk_sys_dept_tenant_parent", "sys_dept"),
        ("fk_sys_menu_tenant_parent", "sys_menu"),
        ("fk_sys_dict_data_tenant_type", "sys_dict_data"),
        ("fk_sys_file_tenant_owner", "sys_file"),
        ("fk_sys_data_scope_demo_tenant_dept", "sys_data_scope_demo"),
        ("fk_sys_data_scope_demo_tenant_creator", "sys_data_scope_demo"),
        ("fk_sys_user_import_batch_tenant_operator", "sys_user_import_batch"),
        ("fk_sys_user_import_log_tenant_batch", "sys_user_import_batch_log"),
        ("fk_sys_user_import_log_tenant_operator", "sys_user_import_batch_log"),
        ("fk_sys_user_export_tenant_operator", "sys_user_export_task"),
        ("fk_sys_job_log_tenant_job", "sys_job_log"),
    )
    for name, table_name in reversed(relationship_names):
        op.drop_constraint(name, table_name, type_="foreignkey")

    op.create_foreign_key(
        "sys_user_import_batch_log_batch_id_fkey",
        "sys_user_import_batch_log",
        "sys_user_import_batch",
        ["batch_id"],
        ["batch_id"],
        ondelete="CASCADE",
    )

    identity_uniques = (
        ("uq_sys_user_tenant_user_id", "sys_user"),
        ("uq_sys_role_tenant_role_id", "sys_role"),
        ("uq_sys_dept_tenant_dept_id", "sys_dept"),
        ("uq_sys_menu_tenant_menu_id", "sys_menu"),
        ("uq_sys_config_tenant_config_id", "sys_config"),
        ("uq_sys_dict_type_tenant_type_id", "sys_dict_type"),
        ("uq_sys_dict_data_tenant_code", "sys_dict_data"),
        ("uq_sys_file_tenant_file_id", "sys_file"),
        ("uq_sys_data_scope_demo_tenant_demo_id", "sys_data_scope_demo"),
        ("uq_sys_user_import_batch_tenant_batch", "sys_user_import_batch"),
        ("uq_sys_user_import_log_tenant_log", "sys_user_import_batch_log"),
        ("uq_sys_user_export_tenant_export", "sys_user_export_task"),
        ("uq_sys_job_tenant_job_id", "sys_job"),
        ("uq_sys_job_log_tenant_log_id", "sys_job_log"),
        ("uq_sys_operation_log_tenant_log_id", "sys_operation_log"),
        ("uq_sys_login_log_tenant_log_id", "sys_login_log"),
    )
    for name, table_name in reversed(identity_uniques):
        op.drop_constraint(name, table_name, type_="unique")

    for table_name in reversed(PLAN2_TENANT_TABLES):
        if table_name != "sys_user":
            op.drop_constraint(
                f"fk_{table_name}_tenant_id_sys_tenant",
                table_name,
                type_="foreignkey",
            )

    op.drop_constraint(
        "uq_sys_user_import_batch_tenant_preview_token",
        "sys_user_import_batch",
        type_="unique",
    )
    op.create_index(
        "ix_sys_user_import_batch_preview_token",
        "sys_user_import_batch",
        ["preview_token"],
        unique=True,
    )

    for table_name, tenant_name, legacy_name, column in reversed(
        (
            (
                "sys_role",
                "uq_sys_role_tenant_role_name",
                "sys_role_role_name_key",
                "role_name",
            ),
            (
                "sys_role",
                "uq_sys_role_tenant_role_code",
                "sys_role_role_code_key",
                "role_code",
            ),
            (
                "sys_config",
                "uq_sys_config_tenant_config_key",
                "sys_config_config_key_key",
                "config_key",
            ),
            (
                "sys_dict_type",
                "uq_sys_dict_type_tenant_name",
                "sys_dict_type_dict_name_key",
                "dict_name",
            ),
            (
                "sys_dict_type",
                "uq_sys_dict_type_tenant_type",
                "sys_dict_type_dict_type_key",
                "dict_type",
            ),
            (
                "sys_job",
                "uq_sys_job_tenant_job_key",
                "sys_job_job_key_key",
                "job_key",
            ),
        )
    ):
        op.drop_constraint(tenant_name, table_name, type_="unique")
        op.create_unique_constraint(legacy_name, table_name, [column])

    op.drop_constraint(
        "uq_sys_user_tenant_employee_no", "sys_user", type_="unique"
    )
    op.drop_constraint(
        "uq_sys_user_tenant_user_name", "sys_user", type_="unique"
    )
    op.create_unique_constraint(
        "uq_sys_user_employee_no", "sys_user", ["employee_no"]
    )
    op.create_index(
        "ix_sys_user_user_name", "sys_user", ["user_name"], unique=True
    )
    op.create_index(
        "ix_sys_user_tenant_user_name",
        "sys_user",
        ["tenant_id", "user_name"],
        unique=False,
    )

    op.drop_column("sys_operation_log", "audit_scope")
    op.drop_column("sys_login_log", "audit_scope")
    op.drop_column("sys_login_log", "tenant_id")
    for table_name in reversed((*_ROOT_SHADOW_TABLES, *_DERIVED_BACKFILLS)):
        op.drop_column(table_name, "tenant_id")
    for table_name in reversed(tuple(_ASSOCIATION_BACKFILLS)):
        op.drop_column(table_name, "tenant_id")
