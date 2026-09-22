import importlib.util
from pathlib import Path

from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.tenant import Tenant


def _load_plan6_migration():
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "5a6b7c8d9e0f_reconcile_release_schema_parity.py"
    )
    assert path.exists()
    spec = importlib.util.spec_from_file_location("plan6_schema_parity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_table_comments_are_declared_in_orm_metadata():
    assert AiAgent.__table__.comment == "AI Agent 注册中心"
    assert RoleAiAgent.__table__.comment == "角色 ↔ Agent RBAC 关联表"
    assert Tenant.__table__.comment == "平台全局租户注册表"
