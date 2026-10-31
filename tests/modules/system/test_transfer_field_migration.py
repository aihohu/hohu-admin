import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_release_migration_roundtrip import (
    migration_connection as migration_connection,
)
from tests.test_release_migration_roundtrip import release_chain


async def test_lineage_upgrade_keeps_historical_null_and_enforces_budget(
    migration_connection,
):
    connection = migration_connection
    chain = release_chain()

    def migrate(sync, modules, method):
        with Operations.context(MigrationContext.configure(sync)):
            for module in modules:
                getattr(module, method)()

    await connection.run_sync(migrate, chain[:-1], "upgrade")
    await connection.execute(
        text(
            "INSERT INTO sys_user (tenant_id,user_id,user_name,hashed_password,status) VALUES (0,101,'historic','unused','1')"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO sys_user_export_task (tenant_id,export_id,operator_id,filter_snapshot,reason,status) VALUES (0,'old',101,'{}','audit','SUCCESS')"
        )
    )
    await connection.run_sync(migrate, chain[-1:], "upgrade")
    assert (
        await connection.execute(
            text("SELECT field_lineage FROM sys_user_export_task WHERE export_id='old'")
        )
    ).one() == (None,)
    for value in ("[]", '{"large":"' + "x" * 2097152 + '"}'):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "UPDATE sys_user_export_task SET field_lineage=CAST(:value AS jsonb) WHERE export_id='old'"
                    ),
                    {"value": value},
                )
    await connection.run_sync(migrate, chain[-1:], "downgrade")
    assert (
        await connection.scalar(
            text("SELECT reason FROM sys_user_export_task WHERE export_id='old'")
        )
        == "audit"
    )
    await connection.run_sync(migrate, chain[-1:], "upgrade")
    assert (
        await connection.execute(
            text("SELECT field_lineage FROM sys_user_export_task WHERE export_id='old'")
        )
    ).one() == (None,)
