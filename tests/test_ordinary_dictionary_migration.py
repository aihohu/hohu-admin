"""The new dictionary boundary refuses dirty defaults and is reversible."""

import pytest
from sqlalchemy import text

from tests.test_release_migration_roundtrip import apply_steps, release_chain
from tests.test_release_migration_roundtrip import (
    migration_connection as migration_connection,
)


async def test_default_conflict_blocks_migration_without_rewriting_data(
    migration_connection,
):
    connection = migration_connection
    chain = release_chain()
    await connection.run_sync(apply_steps, chain[:-1], "upgrade")
    await connection.execute(
        text(
            "INSERT INTO sys_dict_type (dict_type_id,tenant_id,dict_name,dict_type,status) VALUES (601,0,'migration','migration','1')"
        )
    )
    for key in (701, 702):
        await connection.execute(
            text(
                "INSERT INTO sys_dict_data (dict_code,tenant_id,dict_type,dict_sort,dict_label,dict_value,is_default,status) VALUES (:key,0,'migration',1,'label','value','Y','1')"
            ),
            {"key": key},
        )
    with pytest.raises(RuntimeError, match="DICT_DEFAULT_CONFLICT"):
        await connection.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await connection.scalar(
            text("SELECT count(*) FROM sys_dict_data WHERE is_default='Y'")
        )
        == 2
    )
    assert (
        await connection.scalar(
            text(
                "SELECT confupdtype::text FROM pg_constraint WHERE conname='fk_sys_dict_data_tenant_type' AND connamespace=current_schema()::regnamespace"
            )
        )
        == "a"
    )
    await connection.execute(
        text("UPDATE sys_dict_data SET is_default='N' WHERE dict_code=702")
    )
    await connection.run_sync(apply_steps, chain[-1:], "upgrade")
    await connection.execute(
        text("UPDATE sys_dict_type SET dict_type='renamed' WHERE dict_type_id=601")
    )
    assert (
        await connection.scalar(
            text("SELECT count(*) FROM sys_dict_data WHERE dict_type='renamed'")
        )
        == 2
    )
    await connection.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await connection.scalar(
            text(
                "SELECT confupdtype::text FROM pg_constraint WHERE conname='fk_sys_dict_data_tenant_type' AND connamespace=current_schema()::regnamespace"
            )
        )
        == "a"
    )
    assert (
        await connection.scalar(
            text(
                "SELECT to_regclass(current_schema() || '.uq_sys_dict_data_one_default')"
            )
        )
        is None
    )
