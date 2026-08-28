"""Contracts for compacting only unreleased Alembic history."""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_REVISIONS = {
    "7035c014fc57",
    "6565b01411c3",
    "ef3d6a37d688",
    "6957ad20070b",
    "e80b847c61f4",
    "a6a5322fd1b6",
    "0d45e1d2870d",
    "bef2b72de182",
    "a1b2c3d4e5f6",
    "b3c4d5e6f7a8",
    "47538f0c11ae",
    "fbb2836b2e4b",
    "bf244f9a8b76",
}
COMPACTED_REVISIONS = {
    "fba0cf4a5e82": "bf244f9a8b76",
    "0b2165376771": "fba0cf4a5e82",
    "c7d8e9f0a1b2": "0b2165376771",
}
REMOVED_REVISIONS = {
    "3b03d2eccf39",
    "50db0bf83047",
    "51e74fc18eb0",
    "654cab643a43",
    "00558ec10892",
    "a6f4d2c8e1b9",
    "d4b7c9e2f1a0",
    "e8a1f4c2d7b6",
    "f2c4a6b8d0e1",
    "a7d3e9f1c5b2",
    "b8e4c7d2a1f0",
    "c9f5d8e3b2a1",
    "d4a6e8f1c3b2",
    "e6b7f9a2d4c1",
    "f7a8b9c0d1e2",
    "a8b9c0d1e2f3",
}
AI_STEPS = (
    "tool_gateway",
    "operation_timing",
    "agent_quota",
    "agent_risk",
    "supervisor_routing",
    "chat_causality",
    "prepared_action",
    "prepared_action_runtime",
    "prepared_action_execution_lease",
    "operation_tenant",
    "authorization_lineage",
    "message_projection_dependencies",
    "prepared_projection_dependencies",
    "phase4_trace",
    "conversation_soft_delete",
)
USER_STEPS = (
    "user_import_export",
    "import_records_hash",
    "sys_file_owner_tenant",
)


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))


def _load_migration(filename: str) -> ModuleType:
    path = PROJECT_ROOT / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record_step_order(module: ModuleType, steps: tuple[str, ...]) -> list[str]:
    called: list[str] = []
    for step in steps:
        setattr(module, f"_upgrade_{step}", lambda step=step: called.append(step))
    module.upgrade()
    return called


def _record_reverse_step_order(module: ModuleType, steps: tuple[str, ...]) -> list[str]:
    called: list[str] = []
    for step in steps:
        setattr(module, f"_downgrade_{step}", lambda step=step: called.append(step))
    module.downgrade()
    return called


def test_history_preserves_the_release_boundary_and_has_one_compacted_head() -> None:
    scripts = _script_directory()
    revisions = {revision.revision: revision for revision in scripts.walk_revisions()}

    assert set(revisions) == PUBLISHED_REVISIONS | set(COMPACTED_REVISIONS)
    assert scripts.get_heads() == ["c7d8e9f0a1b2"]
    for revision, down_revision in COMPACTED_REVISIONS.items():
        assert revisions[revision].down_revision == down_revision


def test_removed_unreleased_revision_files_do_not_remain_as_aliases() -> None:
    version_files = tuple((PROJECT_ROOT / "alembic" / "versions").glob("*.py"))

    for revision in REMOVED_REVISIONS:
        assert not any(path.name.startswith(revision) for path in version_files)


def test_compacted_domain_steps_preserve_upgrade_and_reverse_downgrade_order() -> None:
    user_migration = _load_migration(
        "0b2165376771_add_user_transfer_security_schema.py"
    )
    ai_migration = _load_migration("c7d8e9f0a1b2_add_governed_ai_management_schema.py")

    assert _record_step_order(user_migration, USER_STEPS) == list(USER_STEPS)
    assert _record_reverse_step_order(user_migration, USER_STEPS) == list(
        reversed(USER_STEPS)
    )
    assert _record_step_order(ai_migration, AI_STEPS) == list(AI_STEPS)
    assert _record_reverse_step_order(ai_migration, AI_STEPS) == list(
        reversed(AI_STEPS)
    )
