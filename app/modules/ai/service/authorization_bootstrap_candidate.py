"""AI-owned bootstrap planning and typed writes; invoked by one platform owner."""

from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.authorization_plan import NewResource, ResourceKey, ResourceKind
from app.core.exceptions import BusinessException, BusinessRuleException
from app.modules.ai.constants import PUBLISHED_AGENT_CODES
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent


@dataclass(frozen=True, slots=True)
class AiBootstrapPlan:
    model_label: str
    agent_binding_count: int
    existing: tuple
    new: tuple
    records: tuple = field(repr=False)
    snapshot: tuple = field(repr=False)


class AiBootstrapCandidate:
    async def prepare(self, db, *, tenant_id, model_id, super_role_id, replayed):
        policies = (
            (
                await db.execute(
                    select(TenantAiModelPolicy.__table__)
                    .where(TenantAiModelPolicy.tenant_id == tenant_id)
                    .order_by(TenantAiModelPolicy.model_id)
                    .limit(10001)
                )
            )
            .mappings()
            .all()
        )
        bindings = (
            (
                await db.execute(
                    select(RoleAiAgent.__table__)
                    .where(RoleAiAgent.tenant_id == tenant_id)
                    .order_by(RoleAiAgent.role_id, RoleAiAgent.agent_id)
                    .limit(10001)
                )
            )
            .mappings()
            .all()
        )
        if len(policies) > 10000 or len(bindings) > 10000:
            raise BusinessException(
                422, "授权计划超过上限", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
            )
        if replayed:
            defaults = [row for row in policies if row["enabled"] and row["is_default"]]
            if len(defaults) != 1:
                raise BusinessException(
                    409,
                    "租户引导状态不完整",
                    error_code="PLATFORM_TENANT_BOOTSTRAP_STATE_INVALID",
                )
            model_id = defaults[0]["model_id"]
        elif policies or bindings:
            raise BusinessException(
                409,
                "prepared tenant 存在未受控 AI 初始化数据",
                error_code="PLATFORM_TENANT_BOOTSTRAP_DIRTY",
            )
        model = (
            (
                await db.execute(
                    select(AiModel.__table__).where(AiModel.model_id == model_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        provider = (
            None
            if model is None
            else (
                await db.execute(
                    select(AiProvider.__table__).where(
                        AiProvider.provider_id == model["provider_id"]
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            model is None
            or provider is None
            or (
                not replayed
                and (
                    not model["is_enabled"]
                    or not provider["is_enabled"]
                    or "text" not in (model["capabilities"] or [])
                )
            )
        ):
            raise BusinessRuleException(
                "所选 AI 模型当前不可用于租户引导",
                error_code="PLATFORM_TENANT_BOOTSTRAP_MODEL_UNAVAILABLE",
            )
        agents = (
            (
                await db.execute(
                    select(AiAgent.__table__)
                    .where(
                        AiAgent.code.in_(PUBLISHED_AGENT_CODES),
                        AiAgent.enabled.is_(True),
                    )
                    .order_by(AiAgent.agent_id)
                    .limit(257)
                )
            )
            .mappings()
            .all()
        )
        if len(agents) > 256:
            raise BusinessException(
                422, "授权计划超过上限", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
            )
        existing = [
            ResourceKey(ResourceKind.MODEL, None, (model_id,)),
            ResourceKey(ResourceKind.PROVIDER, None, (model["provider_id"],)),
        ]
        existing.extend(
            ResourceKey(ResourceKind.AGENT, None, (row["agent_id"],)) for row in agents
        )
        existing.extend(
            ResourceKey(ResourceKind.TENANT_MODEL_POLICY, tenant_id, (row["model_id"],))
            for row in policies
        )
        existing.extend(
            ResourceKey(
                ResourceKind.ROLE_AGENT, tenant_id, (row["role_id"], row["agent_id"])
            )
            for row in bindings
        )
        records = []
        if not replayed:
            records.append(
                (
                    ResourceKey(
                        ResourceKind.TENANT_MODEL_POLICY, tenant_id, (model_id,)
                    ),
                    {
                        "tenant_id": tenant_id,
                        "model_id": model_id,
                        "enabled": True,
                        "is_default": True,
                    },
                )
            )
            records.extend(
                (
                    ResourceKey(
                        ResourceKind.ROLE_AGENT,
                        tenant_id,
                        (super_role_id, row["agent_id"]),
                    ),
                    {
                        "tenant_id": tenant_id,
                        "role_id": super_role_id,
                        "agent_id": row["agent_id"],
                        "enabled": True,
                    },
                )
                for row in agents
            )
        return AiBootstrapPlan(
            f"{provider['name']} / {model['name']}",
            len(bindings) if replayed else len(agents),
            tuple(existing),
            tuple(NewResource(key) for key, _ in records),
            tuple(records),
            (
                dict(model),
                dict(provider),
                tuple(map(dict, agents)),
                tuple(map(dict, policies)),
                tuple(map(dict, bindings)),
            ),
        )

    async def apply(self, tx, plan):
        for key, values in plan.records:
            await tx.insert_reserved(key, values)


ai_bootstrap_candidate = AiBootstrapCandidate()
