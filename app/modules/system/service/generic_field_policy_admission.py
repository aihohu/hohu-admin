"""Fail closed at old delivery boundaries once generic preparation has begun."""

from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.modules.system.models.field_policy import FieldPolicyHead


async def require_user_policy_compatibility(db, *, tenant_id):
    if (
        type(tenant_id) is not int
        or not 0 <= tenant_id < 2**63
        or await generic_deployment_started(db)
    ):
        raise BusinessException(
            409,
            "通用字段策略需要新版入口",
            error_code="FIELD_POLICY_CLIENT_UPGRADE_REQUIRED",
        )


async def generic_deployment_started(db):
    return (
        await db.scalar(
            select(FieldPolicyHead.tenant_id)
            .order_by(FieldPolicyHead.tenant_id)
            .limit(1)
        )
        is not None
    )


async def require_legacy_field_policy(db):
    if await generic_deployment_started(db):
        raise BusinessException(
            503, "通用字段策略缺少当前租户状态", error_code="FIELD_POLICY_UNAVAILABLE"
        )
