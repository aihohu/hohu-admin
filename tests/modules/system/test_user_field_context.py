"""Per-record field applicability must come from each enabled role's own scope."""

from sqlalchemy import insert, update

from app.core.authorization_plan import TenantLock
from app.db.base import user_roles
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_field_context import user_field_context
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


async def test_self_field_grant_does_not_borrow_all_scope(system_sessions):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            update(Role)
            .where(Role.role_id == 201)
            .values(role_code="SELF", data_scope="5")
        )
        await setup.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="All",
                role_code="ALL",
                data_scope="1",
                status="1",
            )
        )
        await setup.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=301)
        )
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="target",
                hashed_password="unused",
                status="1",
            )
        )
        await setup.commit()
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            context = await user_field_context.load(
                db,
                tx,
                tenant=TENANT,
                actor_user_version=1,
                grants=(FieldGrant("201", "nickname", "read", "management.read"),),
            )
            result = await context.decisions(
                db,
                tx,
                user_ids=(101, 501),
                operation="read",
                purpose="management.read",
                entrypoint="user-list",
            )
            assert result[101].fields["nickname"].allowed
            assert not result[501].fields["nickname"].allowed
            assert result[501].fields["user_id"].allowed
