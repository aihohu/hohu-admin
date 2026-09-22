"""Plan 3 schema must make AI lineage tenant-verifiable in PostgreSQL."""

import importlib.util
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, Index, UniqueConstraint

from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.ai.models.routing_log import AiRoutingLog

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "f0a1b2c3d4e5_scope_ai_tenant_lineage.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("plan3_tenant_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_unique(table, columns: tuple[str, ...]) -> bool:
    return any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == columns
        for constraint in table.constraints
    )


def _has_fk(table, local: tuple[str, ...], remote: tuple[str, ...]) -> bool:
    return any(
        isinstance(constraint, ForeignKeyConstraint)
        and tuple(constraint.columns.keys()) == local
        and tuple(item.target_fullname for item in constraint.elements) == remote
        for constraint in table.constraints
    )


def _has_tenant_leading_index(table) -> bool:
    return any(
        isinstance(index, Index) and tuple(index.columns.keys())[:1] == ("tenant_id",)
        for index in table.indexes
    )


def test_conversation_and_message_use_same_tenant_relationships():
    assert AiConversation.tenant_id.nullable is False
    assert AiMessage.tenant_id.nullable is False
    assert _has_unique(AiConversation.__table__, ("tenant_id", "conversation_id"))
    assert _has_fk(
        AiConversation.__table__,
        ("tenant_id", "user_id"),
        ("sys_user.tenant_id", "sys_user.user_id"),
    )
    assert _has_fk(
        AiMessage.__table__,
        ("tenant_id", "conversation_id"),
        ("ai_conversation.tenant_id", "ai_conversation.conversation_id"),
    )


def test_routing_facts_are_non_null_and_tenant_indexed():
    for model in (AiRoutingLog, AiRoutingFeedback):
        assert model.tenant_id.nullable is False
        assert _has_tenant_leading_index(model.__table__)


def test_tenant_model_policy_has_one_tenant_model_identity():
    table = TenantAiModelPolicy.__table__

    assert tuple(table.primary_key.columns.keys()) == ("tenant_id", "model_id")
    assert _has_fk(table, ("tenant_id",), ("sys_tenant.tenant_id",))
    assert _has_fk(table, ("model_id",), ("ai_model.model_id",))
    assert _has_tenant_leading_index(table)
