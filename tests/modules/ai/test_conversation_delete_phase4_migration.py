"""Static migration contract for Phase 4 conversation soft deletion."""

from pathlib import Path

MIGRATION = Path("alembic/versions/a8b9c0d1e2f3_add_ai_conversation_deleted_at.py")


def test_migration_adds_and_removes_deleted_at() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "a8b9c0d1e2f3"' in source
    assert 'down_revision = "f7a8b9c0d1e2"' in source
    assert '"ai_conversation"' in source
    assert (
        'sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)' in source
    )
    assert 'op.drop_column("ai_conversation", "deleted_at")' in source
