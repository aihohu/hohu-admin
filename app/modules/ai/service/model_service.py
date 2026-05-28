from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DuplicateException, NotFoundException
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.model import ModelCreate, ModelUpdate


class ModelService:
    """AI 模型管理服务"""

    async def get_by_id(self, db: AsyncSession, model_id: int) -> AiModel:
        obj = await db.get(AiModel, model_id)
        if not obj:
            raise NotFoundException(
                resource_type="AI模型", error_code="AI_MODEL_NOT_FOUND"
            )
        return obj

    async def get_by_provider(
        self, db: AsyncSession, provider_id: int
    ) -> list[AiModel]:
        stmt = (
            select(AiModel)
            .where(AiModel.provider_id == provider_id)
            .order_by(AiModel.sort_order, AiModel.model_id)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_enabled_by_capability(
        self, db: AsyncSession, capability: str
    ) -> list[AiModel]:
        """获取所有启用且包含指定能力的模型"""
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

    async def create(
        self,
        db: AsyncSession,
        provider_id: int,
        data: ModelCreate,
        create_by: str | None = None,
    ) -> AiModel:
        await self._check_duplicate_name(db, provider_id, data.name)
        dump = data.model_dump()
        dump["provider_id"] = provider_id
        if create_by:
            dump["create_by"] = create_by
        obj = AiModel(**dump)
        db.add(obj)
        return obj

    async def update(
        self, db: AsyncSession, model_id: int, data: ModelUpdate
    ) -> AiModel:
        obj = await self.get_by_id(db, model_id)
        update_data = data.model_dump(exclude_unset=True)

        if "name" in update_data and update_data["name"] != obj.name:
            await self._check_duplicate_name(db, obj.provider_id, update_data["name"])

        for field, value in update_data.items():
            setattr(obj, field, value)
        return obj

    async def delete(self, db: AsyncSession, model_id: int) -> None:
        obj = await self.get_by_id(db, model_id)
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
