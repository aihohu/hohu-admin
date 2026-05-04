from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.security import decrypt_value, encrypt_value
from app.modules.ai.core.provider_registry import create_model, get_default_model
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
        # 检查 provider_code 唯一性
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

        # 如果更新 provider_code，检查唯一性
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

        # 如果提供了新的 api_key，加密后存储；否则跳过（保留原值）
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

    async def resolve_model(self, db: AsyncSession, model_name: str | None = None):
        """解析可用的 AI 模型实例

        优先级：
        1. 如果 model_name 含 provider:model 前缀且找到匹配提供商，使用指定模型
        2. 第一个启用的提供商的 config.default_model
        3. .env 中的默认配置
        4. 抛出异常
        """
        providers = await self.get_all_enabled(db)

        if model_name and providers:
            # 解析 model_name 中的 provider:model
            parts = model_name.split(":", 1)
            target_provider_code = parts[0] if len(parts) > 1 else None
            actual_model_name = parts[1] if len(parts) > 1 else model_name

            # 尝试匹配 provider_code 前缀
            if target_provider_code:
                for p in providers:
                    if p.provider_code == target_provider_code:
                        return create_model(
                            p.provider_code,
                            actual_model_name,
                            decrypt_value(p.api_key),
                            p.base_url,
                        )

            # 前缀未匹配到提供商，忽略 model_name，使用提供商默认模型

        # 使用第一个启用提供商的默认模型
        if providers:
            p = providers[0]
            actual_model = (p.config or {}).get("default_model")
            if not actual_model:
                raise BusinessRuleException(
                    message="AI 模型未配置，请先在模型管理中添加配置",
                    error_code="AI_MODEL_NOT_CONFIGURED",
                )
            return create_model(
                p.provider_code, actual_model, decrypt_value(p.api_key), p.base_url
            )

        # 回退到 .env 默认配置
        model = get_default_model()
        if model:
            return model

        raise BusinessRuleException(
            message="AI 模型未配置，请先在模型管理中添加配置",
            error_code="AI_MODEL_NOT_CONFIGURED",
        )


provider_service = ProviderService()
