"""Upgrade preserves unknown files; provenance cannot be silently discarded."""

import pytest
from sqlalchemy import text

from tests.test_release_migration_roundtrip import apply_steps, release_chain
from tests.test_release_migration_roundtrip import (
    migration_connection as migration_connection,
)


def file_chain():
    chain = release_chain()
    boundary = next(
        i for i, step in enumerate(chain) if step.revision == "e020f3a00001"
    )
    return chain[: boundary + 1]


async def test_file_upgrade_preserves_history_and_empty_roundtrip(migration_connection):
    db = migration_connection
    chain = file_chain()
    await db.run_sync(apply_steps, chain[:-1], "upgrade")
    await db.execute(
        text(
            "INSERT INTO sys_user(user_id,tenant_id,user_name,hashed_password,status) VALUES(101,0,'file-owner','unused','1')"
        )
    )
    await db.execute(
        text(
            "INSERT INTO sys_file(file_id, tenant_id, owner_user_id, original_name, file_name, file_path, file_url, file_size, file_ext,del_flag) VALUES(801,0,101,'old.txt','old','old/old.txt','/uploads/old.txt',4,'.txt','0')"
        )
    )
    await db.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await db.execute(
            text(
                "SELECT original_name, delivery_class, content_sha256, source_manifest FROM sys_file WHERE file_id=801"
            )
        )
    ).one() == ("old.txt", "legacy-unknown", None, None)
    await db.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await db.scalar(text("SELECT file_path FROM sys_file WHERE file_id=801"))
        == "old/old.txt"
    )
    await db.run_sync(apply_steps, chain[-1:], "upgrade")


async def test_downgrade_refuses_to_discard_delivered_file_proof(migration_connection):
    db = migration_connection
    chain = file_chain()
    await db.run_sync(apply_steps, chain, "upgrade")
    await db.execute(
        text(
            "INSERT INTO sys_user(user_id,tenant_id,user_name,hashed_password,status) VALUES(101,0,'file-owner','unused','1')"
        )
    )
    await db.execute(
        text(
            "INSERT INTO sys_file(file_id, tenant_id, owner_user_id, original_name, file_name, file_path, file_url, file_size, file_ext, delivery_class, content_sha256, source_manifest,del_flag) VALUES(802,0,101,'new.txt','new','private/new.txt','',4,'.txt','owned-private',:digest,'{}'::jsonb,'0')"
        ),
        {"digest": "a" * 64},
    )
    with pytest.raises(RuntimeError, match="FIELD_POLICY_DOWNGRADE_BLOCKED"):
        await db.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await db.scalar(text("SELECT delivery_class FROM sys_file WHERE file_id=802"))
        == "owned-private"
    )
