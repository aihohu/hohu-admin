"""对话模型授权与安全选项投影。"""

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.security import decrypt_value
from app.core.tenant import DEFAULT_TENANT_ID
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.core.provider_registry import create_model
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.model import ModelOption


@dataclass(frozen=True)
class AuthorizedChatModel:
    model: AiModel
    provider: AiProvider


class ModelAuthorizationService:
    """所有新 LLM 运行共用的模型选择器。"""

    @staticmethod
    def _not_available() -> BusinessRuleException:
        return BusinessRuleException(
            "所选 AI 模型当前不可用",
            error_code="AI_MODEL_NOT_AVAILABLE",
        )

    async def _available_rows(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
    ) -> list[tuple[AiModel, AiProvider]]:
        # 当前认证域为单租户；拒绝客户端或非可信上下文注入其它租户。
        if tenant_id != DEFAULT_TENANT_ID:
            return []
        rows = (
            await db.execute(
                select(AiModel, AiProvider)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(
                    AiModel.is_enabled.is_(True),
                    AiProvider.is_enabled.is_(True),
                )
                .order_by(AiModel.sort_order, AiModel.model_id)
            )
        ).all()
        text_rows = [
            (model, provider)
            for model, provider in rows
            if "text" in (model.capabilities or [])
        ]
        semaphore = asyncio.Semaphore(10)

        async def check(row: tuple[AiModel, AiProvider]) -> bool:
            model, provider = row
            async with semaphore:
                return await provider_egress.is_model_allowed(
                    provider.provider_code,
                    provider.base_url,
                    model_base_url=model.base_url,
                    provider_config=provider.config,
                    model_config=model.config,
                    provider_id=provider.provider_id,
                    model_id=model.model_id,
                )

        checks = await asyncio.gather(*(check(row) for row in text_rows))
        return [row for row, allowed in zip(text_rows, checks, strict=True) if allowed]

    async def authorize_chat_model(
        self,
        db: AsyncSession,
        model_ref: str | None,
        *,
        tenant_id: int,
    ) -> AuthorizedChatModel:
        """选择启用且具 text 能力的模型；显式无效值绝不降级。"""
        rows = await self._available_rows(db, tenant_id=tenant_id)
        if model_ref is None:
            if not rows:
                raise self._not_available()
            model, provider = rows[0]
            return AuthorizedChatModel(model=model, provider=provider)

        normalized = str(model_ref)
        for model, provider in rows:
            if normalized in {
                str(model.model_id),
                f"{provider.provider_code}:{model.name}",
            }:
                return AuthorizedChatModel(model=model, provider=provider)
        raise self._not_available()

    async def list_model_options(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
    ) -> list[ModelOption]:
        rows = await self._available_rows(db, tenant_id=tenant_id)
        return [
            ModelOption(
                model_id=model.model_id,
                label=f"{provider.name} / {model.name}",
                provider_code=provider.provider_code,
                capabilities=list(model.capabilities or []),
            )
            for model, provider in rows
        ]

    def create_model_instance(self, selected: AuthorizedChatModel):
        model = selected.model
        provider = selected.provider
        return create_model(
            provider.provider_code,
            model.name,
            decrypt_value(provider.api_key),
            model.base_url or provider.base_url,
        )

    async def resolve_model_instance(
        self,
        db: AsyncSession,
        model_ref: str | None,
        *,
        tenant_id: int,
    ):
        selected = await self.authorize_chat_model(
            db,
            model_ref,
            tenant_id=tenant_id,
        )
        return self.create_model_instance(selected)


model_authorization_service = ModelAuthorizationService()
