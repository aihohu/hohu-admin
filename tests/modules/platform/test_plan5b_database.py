import importlib.util
from pathlib import Path


def _load_plan5b_migration():
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "3e4f5a6b7c8d_add_platform_tenant_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("plan5b_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
