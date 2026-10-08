"""P1-C saved Provider test contract, save validation, and quarantine."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from tenant_helpers import tenant_context

from app.core.exceptions import BusinessException
from app.core.security import encrypt_value
from app.core.tenant import PlatformContext
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.model import ModelCreate
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderModelTestDraftRequest,
    ProviderQuery,
    ProviderTestRequest,
)
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.platform.ai_api import router as platform_ai_router

PLATFORM = PlatformContext(
    actor_principal_id=1,
    actor_name="test-platform-admin",
    principal_type="human",
    permissions=frozenset({"platform:ai:read", "platform:ai:write"}),
    reason="provider service test",
    ticket_id="TEST-PROVIDER",
    correlation_id="plan3",
)
TENANT = tenant_context()


@pytest.fixture(autouse=True)
def _public_provider_dns(monkeypatch):
    async def resolve(_hostname: str) -> list[tuple]:
        return [(None, None, None, None, ("93.184.216.34", 0))]

    monkeypatch.setattr(
        "app.modules.ai.core.provider_egress.provider_egress._resolver",
        resolve,
    )


def _routes(path: str, method: str) -> list[APIRoute]:
    return [
        route
        for route in platform_ai_router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    ]


def test_provider_test_endpoints_validate_saved_ids_and_current_form() -> None:
    assert _routes("/test-model", "POST") == []
    assert len(_routes("/ai/providers/{provider_id}/test", "POST")) == 1
    assert len(_routes("/ai/providers/test", "POST")) == 1

    request = ProviderTestRequest.model_validate({"modelId": "123"})
    assert request.model_id == "123"
    with pytest.raises(ValueError):
        ProviderTestRequest.model_validate({"modelId": 123})
    with pytest.raises(ValueError):
        ProviderTestRequest.model_validate(
            {"modelId": "123", "baseUrl": "https://evil.example"}
        )

    draft = ProviderModelTestDraftRequest.model_validate(
        {
            "providerCode": "openai",
            "apiKey": "draft-key",
            "baseUrl": "https://api.openai.com/v1",
            "model": {"name": "draft-model", "capabilities": ["text"]},
        }
    )
    assert draft.provider_id is None
    assert draft.model.name == "draft-model"
    with pytest.raises(ValueError):
        ProviderModelTestDraftRequest.model_validate(
            {
                "providerId": 123,
                "providerCode": "openai",
                "model": {"name": "draft-model", "capabilities": ["text"]},
            }
        )
    with pytest.raises(ValueError):
        ProviderModelTestDraftRequest.model_validate(
            {
                "providerId": "9223372036854775808",
                "providerCode": "openai",
                "model": {"name": "draft-model", "capabilities": ["text"]},
            }
        )


async def test_current_form_probe_uses_unsaved_provider_and_model_without_writing(
    db_session, monkeypatch
) -> None:
    captured = {}
    marker = object()

    def build_model(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return marker

    async def probe(model_instance):
        assert model_instance is marker

    monkeypatch.setattr(
        "app.modules.ai.service.provider_service.create_model", build_model
    )
    monkeypatch.setattr(provider_service, "_probe_model", probe)
    request = ProviderModelTestDraftRequest.model_validate(
        {
            "providerCode": "openai",
            "apiKey": "draft-key",
            "baseUrl": "https://api.openai.com/v1",
            "model": {
                "name": "unsaved-model",
                "capabilities": ["text"],
                "baseUrl": "https://api.openai.com/v1",
                "config": {"generation": {"temperature": 0.4}},
            },
        }
    )

    result = await provider_service.test_draft_connection(
        db_session, request, platform=PLATFORM
    )

    assert result.model_dump(by_alias=True) == {"status": "ok"}
    assert captured["args"] == (
        "openai",
        "unsaved-model",
        "draft-key",
        "https://api.openai.com/v1",
    )
    assert captured["kwargs"]["model_settings"] is not None
    assert not db_session.new


async def test_current_form_probe_reuses_saved_key_but_tests_unsaved_values(
    db_session, monkeypatch
) -> None:
    provider, _model = await _seed_provider_and_model(db_session)
    captured = {}

    def build_model(*args, **_kwargs):
        captured["args"] = args
        return object()

    async def probe(_model_instance):
        return None

    monkeypatch.setattr(
        "app.modules.ai.service.provider_service.create_model", build_model
    )
    monkeypatch.setattr(provider_service, "_probe_model", probe)
    request = ProviderModelTestDraftRequest.model_validate(
        {
            "providerId": str(provider.provider_id),
            "providerCode": "deepseek",
            "apiKey": "",
            "baseUrl": "https://api.deepseek.com/v1",
            "model": {"name": "edited-model", "capabilities": ["text"]},
        }
    )

    await provider_service.test_draft_connection(db_session, request, platform=PLATFORM)

    assert captured["args"] == (
        "deepseek",
        "edited-model",
        "test-key",
        "https://api.deepseek.com/v1",
    )


async def test_current_form_probe_requires_key_and_rejects_private_draft_url(
    db_session, monkeypatch
) -> None:
    called = False

    async def probe(_model_instance):
        nonlocal called
        called = True

    monkeypatch.setattr(provider_service, "_probe_model", probe)
    base = {
        "providerCode": "openai",
        "model": {"name": "draft-model", "capabilities": ["text"]},
    }
    with pytest.raises(BusinessException) as no_key:
        await provider_service.test_draft_connection(
            db_session,
            ProviderModelTestDraftRequest.model_validate(base),
            platform=PLATFORM,
        )
    assert no_key.value.error_code == "AI_PROVIDER_TEST_KEY_REQUIRED"

    with pytest.raises(BusinessException) as blocked:
        await provider_service.test_draft_connection(
            db_session,
            ProviderModelTestDraftRequest.model_validate(
                {**base, "apiKey": "draft-key", "baseUrl": "http://127.0.0.1:11434"}
            ),
            platform=PLATFORM,
        )
    assert blocked.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"
    assert called is False


async def test_current_form_probe_checks_model_url_and_adapter_config(
    db_session, monkeypatch
) -> None:
    called = False

    async def probe(_model_instance):
        nonlocal called
        called = True

    monkeypatch.setattr(provider_service, "_probe_model", probe)
    base = {
        "providerCode": "openai",
        "apiKey": "draft-key",
        "model": {"name": "draft-model", "capabilities": ["text"]},
    }
    for draft in (
        {**base, "model": {**base["model"], "baseUrl": "http://169.254.169.254"}},
        {**base, "config": {"headers": {"Authorization": "Bearer secret"}}},
    ):
        with pytest.raises(BusinessException) as blocked:
            await provider_service.test_draft_connection(
                db_session,
                ProviderModelTestDraftRequest.model_validate(draft),
                platform=PLATFORM,
            )
        assert blocked.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"
    assert called is False


async def test_current_form_probe_requires_write_permission_and_redacts_upstream_failure(
    db_session, monkeypatch
) -> None:
    request = ProviderModelTestDraftRequest.model_validate(
        {
            "providerCode": "openai",
            "apiKey": "draft-key",
            "model": {"name": "draft-model", "capabilities": ["text"]},
        }
    )
    read_only = PlatformContext(
        actor_principal_id=1,
        actor_name="read-only",
        principal_type="human",
        permissions=frozenset({"platform:ai:read"}),
        reason="test",
        ticket_id="TEST-PROVIDER",
        correlation_id="draft-test",
    )
    with pytest.raises(BusinessException) as denied:
        await provider_service.test_draft_connection(
            db_session, request, platform=read_only
        )
    assert denied.value.error_code == "PLATFORM_PERMISSION_DENIED"

    async def probe(_model_instance):
        raise RuntimeError("upstream leaked sk-secret")

    monkeypatch.setattr(provider_service, "_probe_model", probe)
    with pytest.raises(BusinessException) as failed:
        await provider_service.test_draft_connection(
            db_session, request, platform=PLATFORM
        )
    assert failed.value.error_code == "AI_PROVIDER_UPSTREAM_ERROR"
    assert "secret" not in failed.value.message


async def _seed_provider_and_model(db_session) -> tuple[AiProvider, AiModel]:
    marker = uuid4().hex[:10]
    provider = AiProvider(
        provider_code=f"p1c_{marker}",
        name=f"Provider {marker}",
        api_key=encrypt_value("test-key"),
        base_url="https://api.openai.com/v1",
        is_enabled=True,
    )
    db_session.add(provider)
    await db_session.flush()
    model = AiModel(
        provider_id=provider.provider_id,
        name=f"model-{marker}",
        capabilities=["text"],
        is_enabled=True,
    )
    db_session.add(model)
    await db_session.flush()
    return provider, model


async def test_test_connection_rejects_cross_provider_model_without_probe(
    db_session, monkeypatch
) -> None:
    provider, _model = await _seed_provider_and_model(db_session)
    other_provider, other_model = await _seed_provider_and_model(db_session)
    called = False

    async def probe(_model_instance) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(provider_service, "_probe_model", probe)

    with pytest.raises(BusinessException) as exc_info:
        await provider_service.test_connection(
            db_session,
            provider.provider_id,
            other_model.model_id,
            platform=PLATFORM,
        )

    assert provider.provider_id != other_provider.provider_id
    assert exc_info.value.error_code == "AI_PROVIDER_MODEL_MISMATCH"
    assert called is False


async def test_test_connection_returns_ids_and_never_upstream_output(
    db_session, monkeypatch
) -> None:
    provider, model = await _seed_provider_and_model(db_session)

    async def probe(_model_instance) -> str:
        return "secret provider output"

    monkeypatch.setattr(provider_service, "_probe_model", probe)
    result = await provider_service.test_connection(
        db_session,
        provider.provider_id,
        model.model_id,
        platform=PLATFORM,
    )

    assert result.model_dump(by_alias=True) == {
        "providerId": str(provider.provider_id),
        "modelId": str(model.model_id),
        "status": "ok",
    }
    assert "secret" not in result.model_dump_json()


async def test_test_connection_redacts_arbitrary_upstream_failure(
    db_session, monkeypatch
) -> None:
    provider, model = await _seed_provider_and_model(db_session)

    async def probe(_model_instance) -> None:
        raise RuntimeError("upstream leaked sk-super-secret")

    monkeypatch.setattr(provider_service, "_probe_model", probe)
    with pytest.raises(BusinessException) as exc_info:
        await provider_service.test_connection(
            db_session,
            provider.provider_id,
            model.model_id,
            platform=PLATFORM,
        )

    assert exc_info.value.code == 502
    assert exc_info.value.error_code == "AI_PROVIDER_UPSTREAM_ERROR"
    assert "secret" not in exc_info.value.message


async def test_provider_and_model_save_reject_disallowed_destination(
    db_session,
) -> None:
    marker = uuid4().hex[:10]
    with pytest.raises(BusinessException) as provider_exc:
        await provider_service.create(
            db_session,
            ProviderCreate(
                provider_code=f"blocked_{marker}",
                name="Blocked",
                api_key="secret",
                base_url="http://169.254.169.254/latest/meta-data",
            ),
            platform=PLATFORM,
        )
    assert provider_exc.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"

    provider, _model = await _seed_provider_and_model(db_session)
    with pytest.raises(BusinessException) as model_exc:
        await model_service.create(
            db_session,
            provider.provider_id,
            ModelCreate(
                name=f"blocked-model-{marker}",
                capabilities=["text"],
                base_url="http://127.0.0.1:11434/v1",
            ),
            platform=PLATFORM,
        )
    assert model_exc.value.error_code == "AI_PROVIDER_URL_FORBIDDEN"


async def test_runtime_quarantine_removes_model_from_options(
    db_session, monkeypatch
) -> None:
    _provider, model = await _seed_provider_and_model(db_session)

    async def blocked(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "app.modules.ai.service.model_authorization_service.provider_egress.is_model_allowed",
        blocked,
    )

    options = await model_authorization_service.list_model_options(
        db_session,
        tenant=TENANT,
    )

    assert model.model_id not in {item.model_id for item in options}


async def test_provider_list_projects_stable_quarantine_status(
    db_session, monkeypatch
) -> None:
    provider, _model = await _seed_provider_and_model(db_session)

    async def allowed(provider_code: str, _base_url: str | None) -> bool:
        return provider_code != provider.provider_code

    monkeypatch.setattr(
        "app.modules.ai.service.provider_service.provider_egress.is_destination_allowed",
        allowed,
    )
    page = await provider_service.get_list(
        db_session,
        ProviderQuery(provider_code=provider.provider_code),
        platform=PLATFORM,
    )

    assert len(page.records) == 1
    assert page.records[0].egress_status == "EGRESS_POLICY_BLOCKED"
