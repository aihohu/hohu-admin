"""AI-owned tenant policy and Agent bindings for platform bootstrap."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
)
from app.core.tenant import PlatformContext, require_platform_permission
from app.modules.ai.constants import PUBLISHED_AGENT_CODES
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.platform.constants import PLATFORM_TENANT_BOOTSTRAP


@dataclass(frozen=True, slots=True)
class AiTenantBootstrapSummary:
    model_label: str
    model_policy_count: int
    agent_binding_count: int


class AiTenantBootstrapService:
    @staticmethod
    def _authorize(platform: PlatformContext, tenant_id: int) -> None:
        require_platform_permission(platform, PLATFORM_TENANT_BOOTSTRAP)
        if platform.target_tenant_id != tenant_id:
            raise AuthorizationException(
                "平台目标租户不匹配",
                error_code="PLATFORM_TARGET_TENANT_MISMATCH",
            )

    @staticmethod
    async def validate_model(
        db: AsyncSession,
        *,
        model_id: int,
        tenant_id: int,
        platform: PlatformContext,
    ) -> tuple[AiModel, AiProvider]:
        AiTenantBootstrapService._authorize(platform, tenant_id)
        row = (
            await db.execute(
                select(AiModel, AiProvider)
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(
                    AiModel.model_id == model_id,
                    AiModel.is_enabled.is_(True),
                    AiProvider.is_enabled.is_(True),
                )
            )
        ).one_or_none()
        if row is None or "text" not in (row[0].capabilities or []):
            raise BusinessRuleException(
                "所选 AI 模型当前不可用于租户引导",
                error_code="PLATFORM_TENANT_BOOTSTRAP_MODEL_UNAVAILABLE",
            )
        return row[0], row[1]

    @staticmethod
    async def _assert_clean(db: AsyncSession, *, tenant_id: int) -> None:
        policy_count = (
            await db.scalar(
                select(func.count())
                .select_from(TenantAiModelPolicy)
                .where(TenantAiModelPolicy.tenant_id == tenant_id)
            )
            or 0
        )
        binding_count = (
            await db.scalar(
                select(func.count())
                .select_from(RoleAiAgent)
                .where(RoleAiAgent.tenant_id == tenant_id)
            )
            or 0
        )
        if policy_count or binding_count:
            raise BusinessException(
                code=409,
                message="prepared tenant 存在未受控 AI 初始化数据",
                error_code="PLATFORM_TENANT_BOOTSTRAP_DIRTY",
            )

    async def seed(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        super_role_id: int,
        model: AiModel,
        provider: AiProvider,
        platform: PlatformContext,
    ) -> AiTenantBootstrapSummary:
        self._authorize(platform, tenant_id)
        await self._assert_clean(db, tenant_id=tenant_id)
        policy = TenantAiModelPolicy(
            tenant_id=tenant_id,
            model_id=model.model_id,
            enabled=True,
            is_default=True,
        )
        db.add(policy)
        agents = (
            (
                await db.execute(
                    select(AiAgent).where(
                        AiAgent.code.in_(PUBLISHED_AGENT_CODES),
                        AiAgent.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        db.add_all(
            [
                RoleAiAgent(
                    tenant_id=tenant_id,
                    role_id=super_role_id,
                    agent_id=agent.agent_id,
                    enabled=True,
                )
                for agent in agents
            ]
        )
        await db.flush()
        return AiTenantBootstrapSummary(
            model_label=f"{provider.name} / {model.name}",
            model_policy_count=1,
            agent_binding_count=len(agents),
        )

    @staticmethod
    async def summarize(
        db: AsyncSession, *, tenant_id: int, platform: PlatformContext
    ) -> AiTenantBootstrapSummary:
        AiTenantBootstrapService._authorize(platform, tenant_id)
        row = (
            await db.execute(
                select(AiModel, AiProvider)
                .join(
                    TenantAiModelPolicy,
                    TenantAiModelPolicy.model_id == AiModel.model_id,
                )
                .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                .where(
                    TenantAiModelPolicy.tenant_id == tenant_id,
                    TenantAiModelPolicy.enabled.is_(True),
                    TenantAiModelPolicy.is_default.is_(True),
                )
            )
        ).one_or_none()
        if row is None:
            raise BusinessException(
                code=409,
                message="租户引导状态不完整",
                error_code="PLATFORM_TENANT_BOOTSTRAP_STATE_INVALID",
            )
        binding_count = (
            await db.scalar(
                select(func.count())
                .select_from(RoleAiAgent)
                .where(RoleAiAgent.tenant_id == tenant_id)
            )
            or 0
        )
        return AiTenantBootstrapSummary(
            model_label=f"{row[1].name} / {row[0].name}",
            model_policy_count=1,
            agent_binding_count=binding_count,
        )


ai_tenant_bootstrap_service = AiTenantBootstrapService()
