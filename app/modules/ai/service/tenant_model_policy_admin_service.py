"""Platform-only administration for tenant model eligibility policies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from sqlalchemy import and_, select, update
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


@dataclass(frozen=True, slots=True)
class TenantModelCatalogItem(TenantModelPolicyProjection):
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class TenantModelCatalog:
    models: list[TenantModelCatalogItem]
    revision: str


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
    async def _catalog(self, db: AsyncSession, tenant_id: int) -> TenantModelCatalog:
        rows = (
            await db.execute(
                select(AiModel, AiProvider, TenantAiModelPolicy)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .outerjoin(
                    TenantAiModelPolicy,
                    and_(
                        TenantAiModelPolicy.model_id == AiModel.model_id,
                        TenantAiModelPolicy.tenant_id == tenant_id,
                    ),
                )
                .order_by(AiProvider.provider_id, AiModel.model_id)
                .execution_options(populate_existing=True)
            )
        ).all()
        models = []
        for model, provider, policy in rows:
            reason = (
                "provider_disabled"
                if not provider.is_enabled
                else "model_disabled"
                if not model.is_enabled
                else "text_required"
                if "text" not in (model.capabilities or [])
                else None
            )
            models.append(
                TenantModelCatalogItem(
                    model_id=model.model_id,
                    provider_id=provider.provider_id,
                    provider_name=provider.name,
                    model_name=model.name,
                    capabilities=tuple(model.capabilities or []),
                    enabled=policy.enabled if policy else False,
                    is_default=policy.is_default if policy else False,
                    daily_quota_per_user=policy.daily_quota_per_user
                    if policy
                    else None,
                    model_available=reason is None,
                    unavailable_reason=reason,
                )
            )
        snapshot = json.dumps([asdict(row) for row in models], sort_keys=True)
        return TenantModelCatalog(models, hashlib.sha256(snapshot.encode()).hexdigest())

    async def catalog(
        self, db: AsyncSession, *, tenant_id: int, platform: PlatformContext
    ) -> TenantModelCatalog:
        _authorize(platform, tenant_id=tenant_id, write=False)
        await tenant_lifecycle_service.require_ai_policy_target(
            db, tenant_id=tenant_id, write=False, platform=platform
        )
        return await self._catalog(db, tenant_id)

    async def put_many(
        self, db: AsyncSession, *, tenant_id: int, data, platform: PlatformContext
    ) -> TenantModelCatalog:
        _authorize(platform, tenant_id=tenant_id, write=True)
        # Shared with the legacy single-policy write/delete and tenant lifecycle.
        await tenant_lifecycle_service.require_ai_policy_target(
            db, tenant_id=tenant_id, write=True, platform=platform
        )
        # Keep model/provider availability stable until the request transaction ends.
        await db.execute(
            select(AiModel.model_id)
            .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
            .order_by(AiProvider.provider_id, AiModel.model_id)
            .with_for_update(of=(AiModel, AiProvider), read=True)
        )
        catalog = await self._catalog(db, tenant_id)
        existing = {row.model_id: row for row in catalog.models}
        if data.revision != catalog.revision or {
            row.model_id for row in data.policies
        } != set(existing):
            raise BusinessRuleException(
                "模型或授权已变化，请重新加载后再保存",
                error_code="PLATFORM_TENANT_MODEL_POLICIES_STALE",
            )
        for item in data.policies:
            old = existing[item.model_id]
            if (
                item.enabled
                and not old.model_available
                and (
                    not old.enabled
                    or item.is_default
                    or item.daily_quota_per_user != old.daily_quota_per_user
                )
            ):
                raise BusinessRuleException(
                    "所选模型当前不可用于租户",
                    error_code="PLATFORM_TENANT_MODEL_UNAVAILABLE",
                )
        # A savepoint also keeps service callers safe if they catch a write error.
        async with db.begin_nested():
            # Flush the default reset before the new default (partial unique index).
            await db.execute(
                update(TenantAiModelPolicy)
                .where(TenantAiModelPolicy.tenant_id == tenant_id)
                .values(is_default=False)
            )
            stored = {
                row.model_id: row
                for row in await db.scalars(
                    select(TenantAiModelPolicy).where(
                        TenantAiModelPolicy.tenant_id == tenant_id
                    )
                )
            }
            for item in data.policies:
                if item.model_id not in stored and not item.enabled:
                    continue
                policy = stored.get(item.model_id)
                if policy is None:
                    policy = TenantAiModelPolicy(
                        tenant_id=tenant_id, model_id=item.model_id
                    )
                    db.add(policy)
                policy.enabled = item.enabled
                policy.is_default = item.is_default
                policy.daily_quota_per_user = item.daily_quota_per_user
            await db.flush()
        return await self._catalog(db, tenant_id)

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
