"""Actual platform adapters close config and never decode unknown stored JSON."""

import pytest
from sqlalchemy import update

from app.core.exceptions import BusinessException
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.provider import ProviderUpdate
from app.modules.ai.service.authorization_catalog_inputs import freeze_request
from app.modules.system.service import authorization_read_budget as read_budget
from tests.modules.platform.test_authorization_catalog_http import (
    catalog_http as catalog_http,
)
from tests.modules.platform.test_authorization_catalog_http import (
    catalog_sessions as catalog_sessions,
)
from tests.modules.platform.test_authorization_catalog_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_catalog_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_catalog_http import (
    system_sessions as system_sessions,
)
from tests.test_authorization_catalog_candidate import TENANT


async def test_unknown_config_cannot_be_used_for_runtime_egress(monkeypatch):
    async def allowed(*_args):
        return True

    monkeypatch.setattr(provider_egress, "is_destination_allowed", allowed)
    assert not await provider_egress.is_configuration_allowed(
        "openai", None, configs=({"ordinary": "private"}, {})
    )
    assert not await provider_egress.is_configuration_allowed(
        "openai",
        None,
        configs=({}, {"generation": {"temperature": 0.5}, "ordinary": "private"}),
    )


async def test_metadata_update_preserves_unknown_stored_extensions(catalog_http):
    client, plain = catalog_http
    raw = {"headers": {"Authorization": "must-remain-stored"}}
    async with plain() as db:
        await db.execute(
            update(AiProvider).where(AiProvider.provider_id == 501).values(config=raw)
        )
        await db.commit()
    saved = await client.put(
        "/platform/ai/providers/501", json={"name": "Repaired name"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["config"] == {}
    async with plain() as db:
        assert (await db.get(AiProvider, 501)).config == raw


def test_catalog_prepare_rejects_unknown_config_even_without_http_schema():
    with pytest.raises(BusinessException):
        freeze_request(
            tenant=TENANT,
            actor_user_version=1,
            operation="provider.update",
            target_id=501,
            payload=ProviderUpdate(config={"innocent": "sentinel"}),
        )


async def test_actual_catalog_projection_never_decodes_unknown_config(
    catalog_http, monkeypatch
):
    client, plain = catalog_http
    secret = "unknown-content-sentinel"
    async with plain() as db:
        await db.execute(
            update(AiProvider)
            .where(AiProvider.provider_id == 501)
            .values(config={"ordinary": secret})
        )
        await db.execute(
            update(AiModel)
            .where(AiModel.model_id == 601)
            .values(config={"generation": {"temperature": 0.5}, "ordinary": secret})
        )
        await db.commit()
    original = read_budget._size

    def observe(value):
        assert secret not in repr(value)
        return original(value)

    monkeypatch.setattr(read_budget, "_size", observe)
    providers = await client.get("/platform/ai/providers")
    models = await client.get("/platform/ai/providers/501/models")
    assert providers.status_code == models.status_code == 200, (
        providers.text,
        models.text,
    )
    assert secret not in providers.text + models.text
    assert all(
        row["config"] in ({}, None) for row in providers.json()["data"]["records"]
    )
    assert all(row["config"] in ({}, None) for row in models.json()["data"])
    for path in (
        "/platform/ai/providers/models",
        "/platform/ai/agents/model-options",
        "/platform/tenants/0/ai/model-policies",
        "/platform/tenants/0/ai/model-policies/catalog",
    ):
        result = await client.get(path)
        assert result.status_code == 200, result.text
        assert secret not in result.text
