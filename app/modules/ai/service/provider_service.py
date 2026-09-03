import asyncio
import logging

from pydantic_ai import Agent
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.security import decrypt_value, encrypt_value
from app.core.tenant import (
    PlatformContext,
    TenantContext,
    require_platform_permission,
)
from app.modules.ai.core.provider_egress import (
    provider_egress,
    provider_upstream_error,
)
from app.modules.ai.core.provider_registry import create_model
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderOut,
    ProviderTestResult,
    ProviderUpdate,
)
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_AI_WRITE
from app.utils.pagination import build_filters, paginate

logger = logging.getLogger(__name__)


class ProviderService:
    """AI 提供商管理服务"""

    async def get_list(self, db: AsyncSession, query, *, platform: PlatformContext):
        require_platform_permission(platform, PLATFORM_AI_READ)
        field_mapping = {
            "provider_code": "provider_code",
            "name": ("name", "contains"),
            "is_enabled": "is_enabled",
        }
        filters = build_filters(AiProvider, field_mapping, **query.model_dump())
        page = await paginate(
            db=db, model=AiProvider, query_params=query, filters=filters
        )
        semaphore = asyncio.Semaphore(10)

        async def check(provider: AiProvider) -> bool:
            async with semaphore:
                return await provider_egress.is_configuration_allowed(
                    provider.provider_code,
                    provider.base_url,
                    configs=(provider.config,),
                )

        checks = await asyncio.gather(*(check(provider) for provider in page.records))
        records: list[ProviderOut] = []
        for provider, allowed in zip(page.records, checks, strict=True):
            payload = ProviderOut.model_validate(provider).model_copy(
                update={"egress_status": None if allowed else "EGRESS_POLICY_BLOCKED"}
            )
            records.append(payload)
        page.records = records
        return page

    async def get_all_enabled(
        self, db: AsyncSession, *, platform: PlatformContext
    ) -> list[AiProvider]:
        require_platform_permission(platform, PLATFORM_AI_READ)
        stmt = select(AiProvider).where(AiProvider.is_enabled.is_(True))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(
        self,
        db: AsyncSession,
        provider_id: int,
        *,
        platform: PlatformContext,
    ) -> AiProvider:
        require_platform_permission(platform, PLATFORM_AI_READ)
        return await self._get_by_id(db, provider_id)

    @staticmethod
    async def _get_by_id(db: AsyncSession, provider_id: int) -> AiProvider:
        obj = await db.get(AiProvider, provider_id)
        if not obj:
            raise NotFoundException(
                resource_type="AI提供商", error_code="AI_PROVIDER_NOT_FOUND"
            )
        return obj

    async def create(
        self, db: AsyncSession, data: ProviderCreate, *, platform: PlatformContext
    ) -> AiProvider:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        existing = await db.execute(
            select(AiProvider).where(AiProvider.provider_code == data.provider_code)
        )
        if existing.scalar_one_or_none():
            raise DuplicateException(field="提供商标识", value=data.provider_code)

        provider_egress.validate_adapter_config(data.config)
        await provider_egress.validate_destination(data.provider_code, data.base_url)
        dump = data.model_dump()
        dump["api_key"] = encrypt_value(dump["api_key"])
        obj = AiProvider(**dump)
        db.add(obj)
        return obj

    async def update(
        self,
        db: AsyncSession,
        provider_id: int,
        data: ProviderUpdate,
        *,
        platform: PlatformContext,
    ) -> AiProvider:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        obj = await self._get_by_id(db, provider_id)
        update_data = data.model_dump(exclude_unset=True)

        if "provider_code" in update_data:
            existing = await db.execute(
                select(AiProvider).where(
                    and_(
                        AiProvider.provider_code == update_data["provider_code"],
                        AiProvider.provider_id != provider_id,
                    )
                )
            )
            if existing.scalar_one_or_none():
                raise DuplicateException(
                    field="提供商标识", value=update_data["provider_code"]
                )

        if "config" in update_data:
            provider_egress.validate_adapter_config(update_data["config"])
        next_provider_code = update_data.get("provider_code", obj.provider_code)
        next_base_url = (
            update_data["base_url"] if "base_url" in update_data else obj.base_url
        )
        await provider_egress.validate_destination(next_provider_code, next_base_url)

        if "api_key" in update_data:
            if update_data["api_key"]:
                update_data["api_key"] = encrypt_value(update_data["api_key"])
            else:
                del update_data["api_key"]

        for field, value in update_data.items():
            setattr(obj, field, value)
        return obj

    async def delete(
        self, db: AsyncSession, provider_id: int, *, platform: PlatformContext
    ) -> None:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        obj = await self._get_by_id(db, provider_id)
        await db.delete(obj)

    @staticmethod
    async def _probe_model(model_instance) -> None:  # noqa: ANN001
        agent = Agent(model_instance, instructions="Reply with OK")
        await agent.run("Say OK")

    async def test_connection(
        self,
        db: AsyncSession,
        provider_id: int,
        model_id: int,
        *,
        platform: PlatformContext,
    ) -> ProviderTestResult:
        require_platform_permission(platform, PLATFORM_AI_WRITE)
        provider = await self._get_by_id(db, provider_id)
        model = await db.get(AiModel, model_id)
        if model is None:
            raise BusinessException(
                code=404,
                message="AI模型不存在",
                error_code="AI_MODEL_NOT_FOUND",
            )
        if model.provider_id != provider.provider_id:
            raise BusinessRuleException(
                "模型不属于指定 Provider",
                error_code="AI_PROVIDER_MODEL_MISMATCH",
            )
        provider_egress.validate_adapter_config(provider.config)
        provider_egress.validate_adapter_config(model.config)
        await provider_egress.validate_destination(
            provider.provider_code, provider.base_url
        )
        if model.base_url:
            await provider_egress.validate_destination(
                provider.provider_code, model.base_url
            )
        try:
            instance = create_model(
                provider.provider_code,
                model.name,
                decrypt_value(provider.api_key),
                model.base_url or provider.base_url,
            )
            await self._probe_model(instance)
        except BusinessException:
            raise
        except Exception:
            logger.warning(
                "AI Provider test failed provider_id=%s model_id=%s category=upstream",
                provider.provider_id,
                model.model_id,
            )
            raise provider_upstream_error() from None
        return ProviderTestResult(
            provider_id=provider.provider_id,
            model_id=model.model_id,
        )

    async def resolve_model(
        self,
        db: AsyncSession,
        model_id: str | None = None,
        *,
        tenant: TenantContext,
    ):
        """兼容入口也委托统一 selector，不保留未隔离的旧 fallback。"""
        from app.modules.ai.service.model_authorization_service import (  # noqa: PLC0415
            model_authorization_service,
        )

        return await model_authorization_service.resolve_model_instance(
            db,
            model_id,
            tenant=tenant,
        )


provider_service = ProviderService()
