from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.tenant import PlatformContext, require_platform_permission
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.model import ModelCreate, ModelOption, ModelUpdate
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_AI_WRITE


class ModelService:
    """AI 模型管理服务"""

    async def get_by_id(
        self, db: AsyncSession, model_id: int, *, platform: PlatformContext
    ) -> AiModel:
        require_platform_permission(platform, PLATFORM_AI_READ)
        return await self._get_by_id(db, model_id)

    async def get_by_id_for_write(
        self, db: AsyncSession, model_id: int, *, platform: PlatformContext
    ) -> AiModel:
        """Resolve a mutation target without requiring a second read permission."""
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        return await self._get_by_id(db, model_id)

    @staticmethod
    async def _get_by_id(db: AsyncSession, model_id: int) -> AiModel:
        obj = await db.get(AiModel, model_id)
        if not obj:
            raise NotFoundException(
                resource_type="AI模型", error_code="AI_MODEL_NOT_FOUND"
            )
        return obj

    async def get_by_provider(
        self, db: AsyncSession, provider_id: int, *, platform: PlatformContext
    ) -> list[AiModel]:
        require_platform_permission(platform, PLATFORM_AI_READ)
        stmt = (
            select(AiModel)
            .where(AiModel.provider_id == provider_id)
            .order_by(AiModel.sort_order, AiModel.model_id)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_enabled_by_capability(
        self, db: AsyncSession, capability: str, *, platform: PlatformContext
    ) -> list[AiModel]:
        """获取所有启用且包含指定能力的模型"""
        require_platform_permission(platform, PLATFORM_AI_READ)
        stmt = (
            select(AiModel)
            .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
            .where(
                AiModel.is_enabled.is_(True),
                AiProvider.is_enabled.is_(True),
                AiModel.capabilities.contains([capability]),
            )
            .order_by(AiModel.sort_order, AiModel.model_id)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_available_with_provider(
        self,
        db: AsyncSession,
        capability: str | None,
        *,
        platform: PlatformContext,
    ) -> list[tuple[AiModel, AiProvider]]:
        """Load the platform catalog behind the same read permission boundary."""
        require_platform_permission(platform, PLATFORM_AI_READ)
        rows = (
            await db.execute(
                select(AiModel, AiProvider)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(AiModel.is_enabled.is_(True), AiProvider.is_enabled.is_(True))
                .order_by(AiModel.sort_order, AiModel.model_id)
            )
        ).all()
        return [
            (model, provider)
            for model, provider in rows
            if capability is None or capability in (model.capabilities or [])
        ]

    async def list_options(
        self, db: AsyncSession, *, platform: PlatformContext
    ) -> list[ModelOption]:
        """Return the platform-global model catalog without a tenant policy view."""
        require_platform_permission(platform, PLATFORM_AI_READ)
        rows = (
            await db.execute(
                select(AiModel, AiProvider)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(AiModel.is_enabled.is_(True), AiProvider.is_enabled.is_(True))
                .order_by(AiModel.sort_order, AiModel.model_id)
            )
        ).all()
        return [
            ModelOption(
                model_id=model.model_id,
                label=f"{provider.name} / {model.name}",
                provider_code=provider.provider_code,
                capabilities=list(model.capabilities or []),
            )
            for model, provider in rows
        ]

    async def create(
        self,
        db: AsyncSession,
        provider_id: int,
        data: ModelCreate,
        create_by: str | None = None,
        *,
        platform: PlatformContext,
    ) -> AiModel:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        provider = await db.get(AiProvider, provider_id)
        if provider is None:
            raise NotFoundException(
                resource_type="AI提供商", error_code="AI_PROVIDER_NOT_FOUND"
            )
        provider_egress.validate_adapter_config(data.config)
        await provider_egress.validate_destination(
            provider.provider_code, provider.base_url
        )
        if data.base_url:
            await provider_egress.validate_destination(
                provider.provider_code, data.base_url
            )
        await self._check_duplicate_name(db, provider_id, data.name)
        dump = data.model_dump()
        dump["provider_id"] = provider_id
        if create_by:
            dump["create_by"] = create_by
        obj = AiModel(**dump)
        db.add(obj)
        return obj

    async def update(
        self,
        db: AsyncSession,
        model_id: int,
        data: ModelUpdate,
        *,
        platform: PlatformContext,
    ) -> AiModel:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        obj = await self._get_by_id(db, model_id)
        update_data = data.model_dump(exclude_unset=True)
        provider = await db.get(AiProvider, obj.provider_id)
        if provider is None:
            raise NotFoundException(
                resource_type="AI提供商", error_code="AI_PROVIDER_NOT_FOUND"
            )
        if "config" in update_data:
            provider_egress.validate_adapter_config(update_data["config"])
        effective_override = (
            update_data["base_url"] if "base_url" in update_data else obj.base_url
        )
        await provider_egress.validate_destination(
            provider.provider_code, provider.base_url
        )
        if effective_override:
            await provider_egress.validate_destination(
                provider.provider_code, effective_override
            )

        if "name" in update_data and update_data["name"] != obj.name:
            await self._check_duplicate_name(db, obj.provider_id, update_data["name"])

        for field, value in update_data.items():
            setattr(obj, field, value)
        return obj

    async def delete(
        self, db: AsyncSession, model_id: int, *, platform: PlatformContext
    ) -> None:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        obj = await db.scalar(
            select(AiModel).where(AiModel.model_id == model_id).with_for_update()
        )
        if obj is None:
            raise NotFoundException(
                resource_type="AI模型", error_code="AI_MODEL_NOT_FOUND"
            )
        referenced = await db.scalar(
            select(TenantAiModelPolicy.model_id)
            .where(TenantAiModelPolicy.model_id == model_id)
            .limit(1)
        )
        if referenced is not None:
            raise BusinessRuleException(
                "模型已被租户策略引用，请先移除租户授权",
                error_code="AI_MODEL_IN_USE_BY_TENANT_POLICY",
            )
        await db.delete(obj)

    async def _check_duplicate_name(
        self, db: AsyncSession, provider_id: int, name: str
    ) -> None:
        stmt = select(AiModel).where(
            and_(AiModel.provider_id == provider_id, AiModel.name == name)
        )
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            raise DuplicateException(field="模型名称", value=name)


model_service = ModelService()
