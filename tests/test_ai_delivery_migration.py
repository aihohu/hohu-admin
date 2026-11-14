"""An upgrade keeps unknown history and refuses destructive provenance loss."""

import pytest
from sqlalchemy import text

from tests.test_release_migration_roundtrip import apply_steps, release_chain
from tests.test_release_migration_roundtrip import (
    migration_connection as migration_connection,
)


async def seed_title(db, chain):
    await db.run_sync(apply_steps, chain, "upgrade")
    await db.execute(
        text(
            "INSERT INTO sys_user(user_id,tenant_id,user_name,hashed_password,status) VALUES(101,0,'delivery-owner','unused','1')"
        )
    )
    await db.execute(
        text(
            "INSERT INTO ai_conversation(conversation_id,tenant_id,user_id,title,model_name,status) VALUES(201,0,101,'keep historical bytes','1',0)"
        )
    )


async def test_unknown_title_upgrade_and_empty_provenance_roundtrip(
    migration_connection,
):
    db = migration_connection
    chain = release_chain()
    await seed_title(db, chain[:-1])
    await db.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await db.execute(
            text(
                "SELECT title,title_origin FROM ai_conversation WHERE conversation_id=201"
            )
        )
    ).one() == ("keep historical bytes", "legacy_unknown")
    for table in ("ai_operation_log", "ai_prepared_action"):
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=:table AND column_name='result_field_authority' AND data_type='jsonb'"
                ),
                {"table": table},
            )
            == 1
        )
    await db.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await db.scalar(
            text("SELECT title FROM ai_conversation WHERE conversation_id=201")
        )
        == "keep historical bytes"
    )
    await db.run_sync(apply_steps, chain[-1:], "upgrade")
    assert (
        await db.scalar(
            text("SELECT title_origin FROM ai_conversation WHERE conversation_id=201")
        )
        == "legacy_unknown"
    )


@pytest.mark.parametrize("origin", ["authored", "fixed"])
async def test_authored_title_prevents_downgrade(migration_connection, origin):
    db = migration_connection
    chain = release_chain()
    await seed_title(db, chain)
    await db.execute(
        text(
            "UPDATE ai_conversation SET title_origin=:origin WHERE conversation_id=201"
        ),
        {"origin": origin},
    )
    with pytest.raises(RuntimeError, match="FIELD_POLICY_DOWNGRADE_BLOCKED"):
        await db.run_sync(apply_steps, chain[-1:], "downgrade")
    assert (
        await db.scalar(
            text("SELECT title_origin FROM ai_conversation WHERE conversation_id=201")
        )
        == origin
    )
