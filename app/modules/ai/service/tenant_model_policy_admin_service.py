"""Platform-only administration for tenant model eligibility policies."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import PlatformContext, require_platform_permission
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_AI_WRITE
from app.modules.system.service.tenant_lifecycle_service import tenant_lifecycle_service


@dataclass(frozen=True, slots=True)
class TenantModelPolicyProjection:
    model_id: int
    provider_id: int
    provider_name: str
    model_name: str
    capabilities: tuple[str, ...]
    enabled: bool
    is_default: bool
    daily_quota_per_user: int | None
    model_available: bool


def _authorize(platform: PlatformContext, *, tenant_id: int, write: bool) -> None:
    require_platform_permission(
        platform, PLATFORM_AI_WRITE if write else PLATFORM_AI_READ
    )
    if platform.target_tenant_id != tenant_id:
        raise AuthorizationException(
            "平台目标租户不匹配",
            error_code="PLATFORM_TARGET_TENANT_MISMATCH",
        )


def _project(
    policy: TenantAiModelPolicy,
    model: AiModel,
    provider: AiProvider,
) -> TenantModelPolicyProjection:
    return TenantModelPolicyProjection(
        model_id=model.model_id,
        provider_id=provider.provider_id,
        provider_name=provider.name,
        model_name=model.name,
        capabilities=tuple(model.capabilities or []),
        enabled=policy.enabled,
        is_default=policy.is_default,
        daily_quota_per_user=policy.daily_quota_per_user,
        model_available=(
            model.is_enabled
            and provider.is_enabled
            and "text" in (model.capabilities or [])
        ),
    )


class TenantModelPolicyAdminService:
    async def list(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        platform: PlatformContext,
    ) -> list[TenantModelPolicyProjection]:
        _authorize(platform, tenant_id=tenant_id, write=False)
        await tenant_lifecycle_service.require_ai_policy_target(
            db, tenant_id=tenant_id, write=False, platform=platform
        )
        rows = (
            await db.execute(
                select(TenantAiModelPolicy, AiModel, AiProvider)
                .join(AiModel, TenantAiModelPolicy.model_id == AiModel.model_id)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(TenantAiModelPolicy.tenant_id == tenant_id)
                .order_by(
                    TenantAiModelPolicy.is_default.desc(),
                    AiProvider.provider_id,
                    AiModel.model_id,
                )
            )
        ).all()
        return [_project(policy, model, provider) for policy, model, provider in rows]

    async def put(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        model_id: int,
        data,
        platform: PlatformContext,
    ) -> TenantModelPolicyProjection:
        _authorize(platform, tenant_id=tenant_id, write=True)
        await tenant_lifecycle_service.require_ai_policy_target(
            db, tenant_id=tenant_id, write=True, platform=platform
        )
        row = (
            await db.execute(
                select(AiModel, AiProvider)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(AiModel.model_id == model_id)
                .with_for_update(of=AiModel, read=True, key_share=True)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundException("AI模型", error_code="AI_MODEL_NOT_FOUND")
        model, provider = row
        model_available = (
            model.is_enabled
            and provider.is_enabled
            and "text" in (model.capabilities or [])
        )
        if data.enabled and not model_available:
            raise BusinessRuleException(
                "所选模型当前不可用于租户",
                error_code="PLATFORM_TENANT_MODEL_UNAVAILABLE",
            )

        policy = await db.scalar(
            select(TenantAiModelPolicy)
            .where(
                TenantAiModelPolicy.tenant_id == tenant_id,
                TenantAiModelPolicy.model_id == model_id,
            )
            .with_for_update()
        )
        if data.is_default:
            await db.execute(
                update(TenantAiModelPolicy)
                .where(
                    TenantAiModelPolicy.tenant_id == tenant_id,
                    TenantAiModelPolicy.model_id != model_id,
                    TenantAiModelPolicy.is_default.is_(True),
                )
                .values(is_default=False)
            )
        if policy is None:
            policy = TenantAiModelPolicy(
                tenant_id=tenant_id,
                model_id=model_id,
                enabled=data.enabled,
                is_default=data.is_default,
                daily_quota_per_user=data.daily_quota_per_user,
            )
            db.add(policy)
        else:
            policy.enabled = data.enabled
            policy.is_default = data.is_default
            policy.daily_quota_per_user = data.daily_quota_per_user
        await db.flush()
        return _project(policy, model, provider)

    async def delete(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        model_id: int,
        platform: PlatformContext,
    ) -> None:
        _authorize(platform, tenant_id=tenant_id, write=True)
        await tenant_lifecycle_service.require_ai_policy_target(
            db, tenant_id=tenant_id, write=True, platform=platform
        )
        policy = await db.scalar(
            select(TenantAiModelPolicy)
            .where(
                TenantAiModelPolicy.tenant_id == tenant_id,
                TenantAiModelPolicy.model_id == model_id,
            )
            .with_for_update()
        )
        if policy is None:
            raise NotFoundException(
                "租户模型策略", error_code="PLATFORM_TENANT_MODEL_POLICY_NOT_FOUND"
            )
        await db.delete(policy)


tenant_model_policy_admin_service = TenantModelPolicyAdminService()
