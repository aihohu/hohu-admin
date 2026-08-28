"""Static migration contract for Phase 4 conversation soft deletion."""

import importlib.util
import inspect
from pathlib import Path

MIGRATION = Path("alembic/versions/c7d8e9f0a1b2_add_governed_ai_management_schema.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("governed_ai_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_and_removes_deleted_at() -> None:
    migration = _load_migration()
    upgrade_source = inspect.getsource(migration._upgrade_conversation_soft_delete)
    downgrade_source = inspect.getsource(migration._downgrade_conversation_soft_delete)

    assert '"ai_conversation"' in upgrade_source
    assert (
        'sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)'
        in upgrade_source
    )
    assert 'op.drop_column("ai_conversation", "deleted_at")' in downgrade_source
