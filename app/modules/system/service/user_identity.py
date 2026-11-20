"""Current tenant/user identity facts shared by planned owners and projections."""

from sqlalchemy import select
from sqlalchemy.orm import noload

from app.core.exceptions import AuthenticationException
from app.db.authorization_plan_session import _order_error
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


async def require_current_identity(db, tenant, user_version):
    if type(user_version) is not int or user_version < 1:
        raise _order_error()
    live_tenant = await db.scalar(
        select(Tenant)
        .where(Tenant.tenant_id == tenant.tenant_id)
        .execution_options(populate_existing=True)
    )
    actor = await db.scalar(
        select(User)
        .where(User.tenant_id == tenant.tenant_id, User.user_id == tenant.actor_user_id)
        .options(noload("*"))
        .execution_options(populate_existing=True)
    )
    if (
        live_tenant is None
        or live_tenant.status != "1"
        or live_tenant.row_version != tenant.tenant_version
        or live_tenant.tenant_code != tenant.tenant_code
        or actor is None
        or actor.status != "1"
        or actor.auth_version != user_version
    ):
        raise AuthenticationException(
            "当前身份已经失效", error_code="INVALID_CREDENTIALS"
        )
    return actor
