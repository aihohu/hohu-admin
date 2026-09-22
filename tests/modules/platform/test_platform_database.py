import importlib.util
from pathlib import Path


def _load_plan5a_migration():
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "2d3e4f5a6b7c_add_platform_identity_and_audit.py"
    )
    spec = importlib.util.spec_from_file_location("plan5a_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
