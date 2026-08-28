"""Phase 4 AI Trace migration contract tests."""

import importlib.util
import inspect
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "c7d8e9f0a1b2_add_governed_ai_management_schema.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("phase4_trace_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trace_migration_is_part_of_the_governed_ai_schema() -> None:
    migration = _load_migration()

    assert migration.revision == "c7d8e9f0a1b2"
    assert migration.down_revision == "0b2165376771"
    assert callable(migration._upgrade_phase4_trace)


def test_trace_migration_adds_nullable_fields_and_reliable_backfill() -> None:
    migration = _load_migration()
    source = inspect.getsource(migration._upgrade_phase4_trace)

    assert source.count("op.add_column(") == 2
    assert source.count('"ai_operation_log"') == 2
    assert 'sa.Column("agent_code", sa.String(length=64), nullable=True)' in source
    assert 'sa.Column("target_summary", sa.Text(), nullable=True)' in source
    assert "UPDATE ai_operation_log AS operation" in source
    assert "FROM ai_prepared_action AS action" in source
    assert "action.execute_tool_call_id = operation.tool_call_id" in source
    assert "conversation" not in source.casefold()
