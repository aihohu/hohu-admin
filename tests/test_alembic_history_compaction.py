"""Contracts for the consolidated release boundary and unreleased head."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STEPS = (
    "schema",
    "data_fixes",
    "builtin_translation",
    "system_settings",
    "account_self_service",
)


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))


def test_history_preserves_release_boundary_and_current_head() -> None:
    scripts = _scripts()
    assert scripts.get_heads() == ["d01010100101"]
    assert scripts.get_bases() == ["bf244f9a8b76"]
    assert [
        (item.revision, item.down_revision) for item in scripts.walk_revisions()
    ] == [
        ("d01010100101", "bf244f9a8b76"),
        ("bf244f9a8b76", None),
    ]
    assert {path.name for path in (PROJECT_ROOT / "alembic/versions").glob("*.py")} == {
        "bf244f9a8b76_baseline.py",
        "d01010100101_squash_to_head.py",
    }


def test_upgrade_preserves_original_step_order(monkeypatch) -> None:
    module = _scripts().get_revision("head").module
    called = []
    for step in STEPS:
        monkeypatch.setattr(
            module, f"_upgrade_{step}", lambda step=step: called.append(step)
        )
    module.upgrade()
    assert called == list(STEPS)


def test_downgrade_reverses_original_step_order(monkeypatch) -> None:
    module = _scripts().get_revision("head").module
    called = []
    for step in STEPS:
        monkeypatch.setattr(
            module, f"_downgrade_{step}", lambda step=step: called.append(step)
        )
    module.downgrade()
    assert called == list(reversed(STEPS))
