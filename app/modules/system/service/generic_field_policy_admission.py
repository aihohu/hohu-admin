"""Fail closed at old delivery boundaries once generic preparation has begun."""

from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.modules.system.models.field_policy import FieldPolicyHead


async def require_user_policy_compatibility(db, *, tenant_id):
    if (
        await db.scalar(
            select(FieldPolicyHead.tenant_id).where(
                FieldPolicyHead.tenant_id == tenant_id
            )
        )
        is not None
    ):
        raise BusinessException(
            409,
            "通用字段策略需要新版入口",
            error_code="FIELD_POLICY_CLIENT_UPGRADE_REQUIRED",
        )
