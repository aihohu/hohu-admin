"""Upgrade audit reports quarantine without mutating enabled state."""

from __future__ import annotations

from uuid import uuid4

from app.core.security import encrypt_value
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from scripts.audit_ai_provider_egress import audit_ai_provider_egress

pytest_plugins = ("tests.modules.ai.conftest",)


async def test_audit_reports_provider_and_model_without_disabling_rows(
    db_session, monkeypatch
) -> None:
    marker = uuid4().hex[:10]
    provider = AiProvider(
        provider_code=f"audit_{marker}",
        name="Audit Provider",
        api_key=encrypt_value("test-key"),
        base_url="https://blocked.example/v1",
        is_enabled=True,
    )
    db_session.add(provider)
    await db_session.flush()
    model = AiModel(
        provider_id=provider.provider_id,
        name="audit-model",
        capabilities=["text"],
        base_url="https://model-blocked.example/v1",
        is_enabled=True,
    )
    db_session.add(model)
    await db_session.flush()

    async def blocked(provider_code: str, base_url: str | None) -> bool:
        if provider_code == f"audit_{marker}":
            return False
        if base_url and "model-blocked.example" in base_url:
            return False
        return True

    monkeypatch.setattr(
        "scripts.audit_ai_provider_egress.provider_egress.is_destination_allowed",
        blocked,
    )
    report = await audit_ai_provider_egress(db_session)

    assert {(item.object_type, item.object_id) for item in report.findings} == {
        ("provider", provider.provider_id),
        ("model", model.model_id),
    }
    assert {item.status for item in report.findings} == {"EGRESS_POLICY_BLOCKED"}
    assert provider.is_enabled is True
    assert model.is_enabled is True
