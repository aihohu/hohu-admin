"""P1-B 三模型端点与统一 chat model selector。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.conversation import ConversationCreate, ConversationUpdate
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.system.models.user import User


def _tenant(actor_user_id: int = 1) -> TenantContext:
    return TenantContext(0, "default", actor_user_id, 1, "access_token")


@pytest.fixture(autouse=True)
def _public_provider_dns(monkeypatch):
    async def resolve(_hostname: str) -> list[tuple]:
        return [(None, None, None, None, ("93.184.216.34", 0))]

    monkeypatch.setattr(
        "app.modules.ai.core.provider_egress.provider_egress._resolver",
        resolve,
    )


@pytest.fixture
async def committed_model_id() -> int:
    """为独立 HTTP session 提供可见数据，并在测试后精准清理。"""
    async with AsyncSessionLocal() as db:
        provider, model = await _seed_model(db)
        await db.commit()
        provider_id = provider.provider_id
        model_id = model.model_id
    yield model_id
    async with AsyncSessionLocal() as db:
        await db.execute(delete(AiModel).where(AiModel.model_id == model_id))
        await db.execute(
            delete(AiProvider).where(AiProvider.provider_id == provider_id)
        )
        await db.commit()
    try:
        await engine.dispose()
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise


async def _seed_model(
    db: AsyncSession,
    *,
    provider_enabled: bool = True,
    model_enabled: bool = True,
    capabilities: list[str] | None = None,
) -> tuple[AiProvider, AiModel]:
    marker = uuid4().hex[:10]
    provider = AiProvider(
        provider_code=f"p1b_{marker}",
        name=f"Provider {marker}",
        api_key="encrypted-test-key",
        base_url="https://api.openai.com/v1",
        is_enabled=provider_enabled,
    )
    model = AiModel(
        provider_id=0,
        name=f"model-{marker}",
        capabilities=capabilities or ["text"],
        is_enabled=model_enabled,
    )
    db.add(provider)
    await db.flush()
    model.provider_id = provider.provider_id
    db.add(model)
    await db.flush()
    db.add(
        TenantAiModelPolicy(
            tenant_id=0,
            model_id=model.model_id,
            enabled=True,
            is_default=False,
        )
    )
    await db.flush()
    return provider, model


async def test_selector_accepts_only_enabled_text_model(db_session) -> None:
    provider, model = await _seed_model(db_session)

    selected = await model_authorization_service.authorize_chat_model(
        db_session,
        str(model.model_id),
        tenant=_tenant(),
    )

    assert selected.model.model_id == model.model_id
    assert selected.provider.provider_id == provider.provider_id


@pytest.mark.parametrize(
    ("provider_enabled", "model_enabled", "capabilities"),
    [
        (False, True, ["text"]),
        (True, False, ["text"]),
        (True, True, ["vision"]),
    ],
)
async def test_selector_rejects_non_chat_safe_model(
    db_session,
    provider_enabled: bool,
    model_enabled: bool,
    capabilities: list[str],
) -> None:
    _provider, model = await _seed_model(
        db_session,
        provider_enabled=provider_enabled,
        model_enabled=model_enabled,
        capabilities=capabilities,
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await model_authorization_service.authorize_chat_model(
            db_session,
            str(model.model_id),
            tenant=_tenant(),
        )

    assert exc_info.value.error_code == "AI_MODEL_NOT_AVAILABLE"


async def test_model_options_expose_only_safe_allowlist_fields(db_session) -> None:
    provider, model = await _seed_model(db_session, capabilities=["text", "vision"])

    options = await model_authorization_service.list_model_options(
        db_session,
        tenant=_tenant(),
    )
    option = next(item for item in options if item.model_id == model.model_id)
    payload = option.model_dump(by_alias=True)

    assert payload == {
        "modelId": str(model.model_id),
        "label": f"{provider.name} / {model.name}",
        "providerCode": provider.provider_code,
        "capabilities": ["text", "vision"],
    }
    assert "baseUrl" not in payload
    assert "providerId" not in payload


async def test_chat_model_options_are_tenant_scoped_and_agent_admin_is_platform_only(
    authed_client,
    committed_model_id: int,
) -> None:
    client, _token = authed_client

    chat_response = await client.get("/ai/chat/models")
    agent_response = await client.get("/platform/ai/agents/model-options")

    assert chat_response.status_code == 200
    assert agent_response.status_code == 403
    assert agent_response.json()["errorCode"] == "PLATFORM_ADMIN_REQUIRED"
    row = next(
        item
        for item in chat_response.json()["data"]
        if item["modelId"] == str(committed_model_id)
    )
    assert set(row) == {"modelId", "label", "providerCode", "capabilities"}


async def test_conversation_create_rejects_unavailable_model_before_persist(
    db_session,
) -> None:
    _provider, model = await _seed_model(db_session, model_enabled=False)
    user_id = await db_session.scalar(
        select(User.user_id).where(User.user_name == "admin")
    )
    before = await db_session.scalar(select(func.count(AiConversation.conversation_id)))
    tenant = _tenant(user_id)

    with pytest.raises(BusinessRuleException) as exc_info:
        await conversation_service.create(
            db_session,
            ConversationCreate(model_name=str(model.model_id)),
            user_id,
            tenant=tenant,
        )

    after = await db_session.scalar(select(func.count(AiConversation.conversation_id)))
    assert exc_info.value.error_code == "AI_MODEL_NOT_AVAILABLE"
    assert after == before


async def test_conversation_update_rejects_unavailable_model_atomically(
    db_session,
) -> None:
    _provider, model = await _seed_model(db_session, model_enabled=False)
    user_id = await db_session.scalar(
        select(User.user_id).where(User.user_name == "admin")
    )
    conversation = AiConversation(
        tenant_id=0,
        user_id=user_id,
        title="unchanged",
        model_name="legacy-model",
    )
    db_session.add(conversation)
    await db_session.flush()
    tenant = _tenant(user_id)

    with pytest.raises(BusinessRuleException) as exc_info:
        await conversation_service.update(
            db_session,
            conversation.conversation_id,
            ConversationUpdate(
                title="must-not-change",
                model_name=str(model.model_id),
            ),
            user_id,
            tenant=tenant,
        )

    assert exc_info.value.error_code == "AI_MODEL_NOT_AVAILABLE"
    assert conversation.title == "unchanged"
    assert conversation.model_name == "legacy-model"
