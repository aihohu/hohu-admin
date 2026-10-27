"""Model selection snapshots separate DB authority from lock-free DNS checks."""

from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.service.authorization_catalog_inputs import wire


@dataclass(frozen=True, slots=True, repr=False)
class ChatModelSelection:
    tenant_id: int
    model_id: int
    provider_id: int
    keys: tuple
    facts: bytes = field(repr=False)
    egress: dict = field(repr=False)


async def select_model(db, tenant, model_ref):
    rows = (
        await db.execute(
            select(AiModel, AiProvider, TenantAiModelPolicy)
            .join(AiProvider, AiProvider.provider_id == AiModel.provider_id)
            .join(TenantAiModelPolicy, TenantAiModelPolicy.model_id == AiModel.model_id)
            .where(
                TenantAiModelPolicy.tenant_id == tenant.tenant_id,
                TenantAiModelPolicy.enabled.is_(True),
                AiModel.is_enabled.is_(True),
                AiProvider.is_enabled.is_(True),
            )
            .order_by(
                TenantAiModelPolicy.is_default.desc(),
                AiModel.sort_order,
                AiModel.model_id,
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    for model, provider, policy in rows:
        if "text" not in (model.capabilities or []) or (
            model_ref is not None
            and str(model_ref)
            not in {str(model.model_id), provider.provider_code + ":" + model.name}
        ):
            continue
        facts = wire(
            [
                {c.name: getattr(row, c.name) for c in row.__table__.c}
                for row in (model, provider, policy)
            ]
        )
        return ChatModelSelection(
            tenant.tenant_id,
            model.model_id,
            provider.provider_id,
            (
                ResourceKey(K.PROVIDER, None, provider.provider_id),
                ResourceKey(K.MODEL, None, model.model_id),
                ResourceKey(K.TENANT_MODEL_POLICY, tenant.tenant_id, model.model_id),
            ),
            facts,
            {
                "provider_code": provider.provider_code,
                "base_url": provider.base_url,
                "model_base_url": model.base_url,
                "provider_config": provider.config,
                "model_config": model.config,
                "provider_id": provider.provider_id,
                "model_id": model.model_id,
            },
        )
    raise _order_error()


async def recheck_model(db, tx, tenant, selection):
    if (
        type(selection) is not ChatModelSelection
        or selection.tenant_id != tenant.tenant_id
    ):
        raise _order_error()
    tx.assert_held(*selection.keys)
    if await select_model(db, tenant, str(selection.model_id)) != selection:
        raise _order_error()
