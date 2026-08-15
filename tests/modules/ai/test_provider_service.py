"""P1-C saved Provider test contract, save validation, and quarantine."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from app.core.exceptions import BusinessException
from app.core.security import encrypt_value
from app.modules.ai.api.provider import router as provider_router
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.model import ModelCreate
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderQuery,
    ProviderTestRequest,
)
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service


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
        for route in provider_router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    ]


def test_provider_test_endpoint_references_only_saved_objects() -> None:
    assert _routes("/test-model", "POST") == []
    assert len(_routes("/{provider_id}/test", "POST")) == 1

    request = ProviderTestRequest.model_validate({"modelId": "123"})
    assert request.model_id == "123"
    with pytest.raises(ValueError):
        ProviderTestRequest.model_validate({"modelId": 123})
    with pytest.raises(ValueError):
        ProviderTestRequest.model_validate(
            {"modelId": "123", "baseUrl": "https://evil.example"}
        )


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
            tenant_id=0,
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
        tenant_id=0,
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
            tenant_id=0,
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
        tenant_id=0,
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
    )

    assert len(page.records) == 1
    assert page.records[0].egress_status == "EGRESS_POLICY_BLOCKED"
