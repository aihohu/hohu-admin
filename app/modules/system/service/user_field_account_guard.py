"""Own-account write proof for Auth; System owns model access and field policy."""

from sqlalchemy import select

from app.db.authorization_plan_session import _order_error
from app.db.base import user_roles
from app.modules.system.field_policy.dto import resolve_own_account_fields
from app.modules.system.field_policy.evaluator import validate_write_fields
from app.modules.system.models.role import Role
from app.modules.system.service.user_field_active import active_policy_grants


async def require_own_email_write(db, tx, current):
    # Current-password/email proof authenticates the subject; it does not
    # override an own-account field deny. No caller-supplied role IDs.
    account = current.account
    if account is None:
        raise _order_error()
    roles = tuple(
        str(value)
        for value in (
            await db.scalars(
                select(Role.role_id)
                .join(
                    user_roles,
                    (user_roles.c.tenant_id == Role.tenant_id)
                    & (user_roles.c.role_id == Role.role_id),
                )
                .where(
                    Role.tenant_id == current.tenant_id,
                    Role.status == "1",
                    user_roles.c.user_id == account.user_id,
                )
                .order_by(Role.role_id)
                .limit(1025)
            )
        ).all()
    )
    grants = await active_policy_grants(
        db, tx, tenant_id=current.tenant_id, role_ids=roles
    )
    if grants is not None:
        identity = (current.tenant_id, account.user_id)
        decision = resolve_own_account_fields(
            actor=identity,
            target=identity,
            grants=grants,
            applicable_role_ids=frozenset(roles),
            operation="update",
        )
        validate_write_fields(
            supplied_paths=("user_email",),
            decisions=decision,
            operation="update",
        )
