"""Tenant model policy is the only bridge to platform-global AI models."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from tenant_helpers import create_test_tenant, tenant_context

from app.core.exceptions import BusinessRuleException
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)


async def test_model_policy_is_fail_closed_and_tenant_specific(db_session) -> None:
    tenant_b = await create_test_tenant(db_session, prefix="ai-model-b")
    marker = uuid4().hex[:10]
    provider = AiProvider(
        provider_code=f"policy_{marker}",
        name=f"Policy provider {marker}",
        api_key="encrypted-test-key",
        base_url="https://api.openai.com/v1",
        is_enabled=True,
    )
    db_session.add(provider)
    await db_session.flush()
    model = AiModel(
        provider_id=provider.provider_id,
        name=f"policy-model-{marker}",
        capabilities=["text"],
        is_enabled=True,
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(
        TenantAiModelPolicy(
            tenant_id=0,
            model_id=model.model_id,
            enabled=True,
            is_default=False,
        )
    )
    await db_session.flush()

    tenant_b_ctx = tenant_context(tenant_id=tenant_b.tenant_id)
    egress_check = AsyncMock(return_value=True)
    with patch.object(provider_egress, "is_model_allowed", egress_check):
        assert (
            await model_authorization_service.list_model_options(
                db_session, tenant=tenant_b_ctx
            )
            == []
        )
        with pytest.raises(BusinessRuleException) as exc_info:
            await model_authorization_service.authorize_chat_model(
                db_session,
                str(model.model_id),
                tenant=tenant_b_ctx,
            )

        assert exc_info.value.error_code == "AI_MODEL_NOT_AVAILABLE"
        egress_check.assert_not_awaited()

        db_session.add(
            TenantAiModelPolicy(
                tenant_id=tenant_b.tenant_id,
                model_id=model.model_id,
                enabled=True,
                is_default=True,
            )
        )
        await db_session.flush()

        selected = await model_authorization_service.authorize_chat_model(
            db_session,
            str(model.model_id),
            tenant=tenant_b_ctx,
        )

    assert selected.model.model_id == model.model_id
    assert selected.provider.provider_id == provider.provider_id
    egress_check.assert_awaited_once()
