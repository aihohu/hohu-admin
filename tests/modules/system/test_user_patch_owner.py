"""PATCH uses the real System transaction owner and preserves field presence."""

from sqlalchemy import select, update

from app.db.authorization_system_owner import system_candidate_owner
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserPatch
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


async def test_patch_commits_only_supplied_fields(system_sessions):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            update(User)
            .where(User.user_id == 101)
            .values(nickname="before", user_phone="13812341234")
        )
        await setup.commit()
    async with factory() as db:
        await system_candidate_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="user.update",
            target_id=101,
            payload=UserPatch(user_phone=None),
        )
    async with plain() as observer:
        row = (
            await observer.execute(
                select(
                    User.nickname, User.user_phone, User.user_name, User.status
                ).where(User.user_id == 101)
            )
        ).one()
        assert tuple(row) == ("before", None, "planner", "1")
