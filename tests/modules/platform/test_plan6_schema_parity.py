import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def test_plan6_schema_parity_migration_is_linear_and_fails_on_null_timestamps():
    migration = _load_plan6_migration()
    result = MagicMock()
    result.scalar_one.return_value = True
    connection = MagicMock()
    connection.execute.return_value = result
    migration.op = SimpleNamespace(get_bind=lambda: connection)

    assert migration.down_revision == "4f5a6b7c8d9e"
    with pytest.raises(RuntimeError, match="PLAN6_AI_MODEL_TIMESTAMP_NULL"):
        migration._assert_ai_model_timestamps_present()

    assert connection.execute.call_count == 2
    assert "ACCESS EXCLUSIVE" in str(connection.execute.call_args_list[0].args[0])
    assert "create_time IS NULL" in str(connection.execute.call_args_list[1].args[0])


def test_existing_table_comments_are_declared_in_orm_metadata():
    assert AiAgent.__table__.comment == "AI Agent 注册中心"
    assert RoleAiAgent.__table__.comment == "角色 ↔ Agent RBAC 关联表"
    assert Tenant.__table__.comment == "平台全局租户注册表"
