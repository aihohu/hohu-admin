"""Verified leader provenance upgrades preserve history and enforce actual FK rules."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_release_migration_roundtrip import apply_steps, release_chain
from tests.test_release_migration_roundtrip import (
    migration_connection as migration_connection,
)


async def test_leader_source_upgrade_fk_and_roundtrip(migration_connection):
    db = migration_connection
    chain = release_chain()
    await db.run_sync(apply_steps, chain[:-1], "upgrade")
    await db.execute(
        text(
            "INSERT INTO sys_dept(dept_id,tenant_id,dept_name,order_num,status,leader) VALUES(401,0,'history',0,'1','old user')"
        )
    )
    await db.run_sync(apply_steps, chain[-1:], "upgrade")
    old = (
        await db.execute(
            text("SELECT leader,leader_user_id FROM sys_dept WHERE dept_id=401")
        )
    ).one()
    assert old == ("old user", None)
    await db.execute(
        text(
            "INSERT INTO sys_user(user_id,tenant_id,user_name,hashed_password,status) VALUES(501,0,'leader','unused','1')"
        )
    )
    await db.execute(text("UPDATE sys_dept SET leader_user_id=501 WHERE dept_id=401"))
    await db.execute(
        text(
            "INSERT INTO sys_tenant(tenant_id,tenant_code,tenant_name,status,lifecycle_state,bootstrap_version,row_version,bootstrap_key_hash,bootstrap_fingerprint) VALUES(1,'other','other','1','active',1,1,:hash,:fingerprint)"
        ),
        {"hash": "a" * 64, "fingerprint": "b" * 64},
    )
    for statement in (
        "UPDATE sys_dept SET tenant_id=1 WHERE dept_id=401",
        "UPDATE sys_dept SET leader='' WHERE dept_id=401",
        "DELETE FROM sys_user WHERE user_id=501",
    ):
        async with db.begin_nested() as savepoint:
            with pytest.raises(IntegrityError):
                await db.execute(text(statement))
            await savepoint.rollback()
    await db.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await db.scalar(text("SELECT leader FROM sys_dept WHERE dept_id=401"))
        == "old user"
    )
    await db.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await db.scalar(text("SELECT leader_user_id FROM sys_dept WHERE dept_id=401"))
        is None
    )


async def test_upgrade_adds_no_guessed_leader_and_no_new_lock_rank(
    migration_connection,
):
    db = migration_connection
    await db.run_sync(apply_steps, release_chain(), "upgrade")
    keys = set(
        await db.scalars(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid='sys_dept'::regclass"
            )
        )
    )
    assert {"fk_sys_dept_tenant_leader", "ck_sys_dept_leader_source"} <= keys
    assert await db.scalar(
        text("SELECT to_regclass(current_schema() || '.ix_sys_dept_tenant_leader')")
    )
