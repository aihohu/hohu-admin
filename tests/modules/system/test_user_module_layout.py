"""Architecture contracts for the System user horizontal layout."""

from __future__ import annotations

import importlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYSTEM_PACKAGE = PROJECT_ROOT / "app" / "modules" / "system"
EXPECTED_MODULES = (
    "app.modules.system.models.user",
    "app.modules.system.models.user_transfer",
    "app.modules.system.schemas.user",
    "app.modules.system.schemas.user_transfer",
    "app.modules.system.service.user_service",
    "app.modules.system.service.user_import_service",
    "app.modules.system.service.user_import_parser",
    "app.modules.system.service.user_import_state",
    "app.modules.system.service.user_import_validator",
    "app.modules.system.service.user_import_template_service",
    "app.modules.system.service.user_export_service",
)


def test_horizontal_user_modules_are_importable() -> None:
    for module_name in EXPECTED_MODULES:
        importlib.import_module(module_name)


def test_legacy_user_package_is_removed() -> None:
    assert not (SYSTEM_PACKAGE / "user").exists()


def test_repository_code_has_no_legacy_user_imports() -> None:
    offenders = []
    source_roots = (
        PROJECT_ROOT / "alembic",
        PROJECT_ROOT / "app",
        PROJECT_ROOT / "scripts",
        PROJECT_ROOT / "tests",
    )
    for source_root in source_roots:
        for path in source_root.rglob("*.py"):
            if path == Path(__file__):
                continue
            if "app.modules.system.user" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert offenders == []
