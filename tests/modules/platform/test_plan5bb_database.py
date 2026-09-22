import importlib.util
from pathlib import Path


def _load_plan5bb_migration():
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "4f5a6b7c8d9e_add_tenant_bootstrap_marker.py"
    )
    spec = importlib.util.spec_from_file_location("plan5bb_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
