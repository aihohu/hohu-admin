from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.security import decrypt_value, encrypt_value
from app.modules.ai.core.provider_registry import create_model, get_default_model
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.provider import ProviderCreate, ProviderUpdate
from app.utils.pagination import build_filters, paginate


class ProviderService:
    """AI 提供商管理服务"""

    async def get_list(self, db: AsyncSession, query):
        field_mapping = {
            "provider_code": "provider_code",
            "name": ("name", "contains"),
            "is_enabled": "is_enabled",
        }
        filters = build_filters(AiProvider, field_mapping, **query.model_dump())
        return await paginate(
            db=db, model=AiProvider, query_params=query, filters=filters
        )

    async def get_all_enabled(self, db: AsyncSession) -> list[AiProvider]:
        stmt = select(AiProvider).where(AiProvider.is_enabled.is_(True))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, db: AsyncSession, provider_id: int) -> AiProvider:
        obj = await db.get(AiProvider, provider_id)
        if not obj:
            raise NotFoundException(
                resource_type="AI提供商", error_code="AI_PROVIDER_NOT_FOUND"
            )
        return obj

    async def create(self, db: AsyncSession, data: ProviderCreate) -> AiProvider:
        existing = await db.execute(
            select(AiProvider).where(AiProvider.provider_code == data.provider_code)
        )
        if existing.scalar_one_or_none():
            raise DuplicateException(field="提供商标识", value=data.provider_code)

        dump = data.model_dump()
        dump["api_key"] = encrypt_value(dump["api_key"])
        obj = AiProvider(**dump)
        db.add(obj)
        return obj

    async def update(
        self, db: AsyncSession, provider_id: int, data: ProviderUpdate
    ) -> AiProvider:
        obj = await self.get_by_id(db, provider_id)
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

        if "api_key" in update_data:
            if update_data["api_key"]:
                update_data["api_key"] = encrypt_value(update_data["api_key"])
            else:
                del update_data["api_key"]

        for field, value in update_data.items():
            setattr(obj, field, value)
        return obj

    async def delete(self, db: AsyncSession, provider_id: int) -> None:
        obj = await self.get_by_id(db, provider_id)
        await db.delete(obj)

    async def resolve_model(self, db: AsyncSession, model_id: str | None = None):
        """根据 model_id (Snowflake) 解析 AI 模型实例，回退到第一个文本模型"""
        model = await self._find_model(db, model_id)
        if model:
            return await self._build_model_instance(db, model)

        # 回退到 .env 默认配置
        fallback = get_default_model()
        if fallback:
            return fallback

        raise BusinessRuleException(
            message="AI 模型未配置，请先在模型管理中添加配置",
            error_code="AI_MODEL_NOT_CONFIGURED",
        )

    async def _find_model(self, db: AsyncSession, model_id: str | None):
        """按 model_id 查找，未指定则回退第一个文本模型"""
        if model_id:
            try:
                model = await db.get(AiModel, int(model_id))
                if model and model.is_enabled:
                    provider = await db.get(AiProvider, model.provider_id)
                    if provider and provider.is_enabled:
                        return model
            except (ValueError, TypeError):
                pass

        # 回退: 第一个启用的文本模型
        stmt = (
            select(AiModel)
            .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
            .where(
                AiModel.is_enabled.is_(True),
                AiProvider.is_enabled.is_(True),
                AiModel.capabilities.contains(["text"]),
            )
            .order_by(AiModel.sort_order, AiModel.model_id)
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def _build_model_instance(self, db: AsyncSession, model: AiModel):
        """根据模型记录构建 Pydantic AI Model 实例"""
        provider = await self.get_by_id(db, model.provider_id)
        return create_model(
            provider.provider_code,
            model.name,
            decrypt_value(provider.api_key),
            model.base_url or provider.base_url,
        )


provider_service = ProviderService()
