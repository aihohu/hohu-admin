"""Phase 4 AI Trace migration contract tests."""

import importlib.util
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "f7a8b9c0d1e2_add_phase4_ai_trace_fields.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("phase4_trace_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trace_migration_extends_the_single_current_head() -> None:
    migration = _load_migration()

    assert migration.revision == "f7a8b9c0d1e2"
    assert migration.down_revision == "e6b7f9a2d4c1"


def test_trace_migration_adds_nullable_fields_and_reliable_backfill() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert source.count("op.add_column(") == 2
    assert source.count('"ai_operation_log"') >= 4
    assert 'sa.Column("agent_code", sa.String(length=64), nullable=True)' in source
    assert 'sa.Column("target_summary", sa.Text(), nullable=True)' in source
    assert "UPDATE ai_operation_log AS operation" in source
    assert "FROM ai_prepared_action AS action" in source
    assert "action.execute_tool_call_id = operation.tool_call_id" in source
    assert "conversation" not in source.casefold()
