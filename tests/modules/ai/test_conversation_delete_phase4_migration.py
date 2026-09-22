"""Static migration contract for Phase 4 conversation soft deletion."""

import ast
from pathlib import Path

MIGRATION = Path("alembic/versions/e7cc9aa08769_squash_to_head.py")


def _deleted_at_column_calls(source: str) -> list[str]:
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("add_column", "drop_column"):
                calls.append(ast.unparse(node))
    return calls


def test_migration_adds_and_removes_deleted_at() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    calls = _deleted_at_column_calls(source)

    added = [c for c in calls if "add_column" in c and "deleted_at" in c]
    assert added, "ai_conversation.deleted_at add_column missing"
    assert any("DateTime(timezone=True), nullable=True" in c for c in added), (
        "deleted_at must stay timezone-aware and nullable"
    )

    assert any("drop_column" in c and "deleted_at" in c for c in calls), (
        "downgrade must drop deleted_at"
    )
