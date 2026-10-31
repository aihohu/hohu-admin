"""Legacy adapters cannot silently become enforcement adapters after activation."""

from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.modules.system.models.user_field_policy import UserFieldPolicyState


async def require_legacy_field_policy(db, tx, *, tenant_id):
    tx.assert_session(db)
    tx.assert_planning(tenant_id)
    state = (
        await db.execute(
            select(
                UserFieldPolicyState.mode,
                UserFieldPolicyState.ever_enforced,
                UserFieldPolicyState.active_manifest_digest,
            ).where(UserFieldPolicyState.tenant_id == tenant_id)
        )
    ).one_or_none()
    if (
        state is None
        or state.mode not in {"legacy", "ready"}
        or state.ever_enforced
        or state.active_manifest_digest is not None
    ):
        raise BusinessRuleException(
            "字段策略入口尚不可交付", error_code="FIELD_POLICY_UNAVAILABLE"
        )
