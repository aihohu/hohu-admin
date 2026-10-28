"""HTTP read sessions cannot re-enter after handing off to a fixed owner."""

import pytest
from sqlalchemy import select, update

from app.core.exceptions import BusinessException
from app.db.authorization_http import release_request_reads
from app.db.request_session import RequestSession, request_reads_released
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_handoff_detaches_releases_and_permanently_rejects_reentry(
    system_sessions,
):
    _, plain = system_sessions
    async with RequestSession(bind=plain.kw["bind"], expire_on_commit=False) as db:
        user = await db.get(User, 101)
        assert db.in_transaction()
        await release_request_reads(db)
        assert user.user_name == "planner"
        assert not db.in_transaction()
        assert request_reads_released(db)
        for operation in (lambda: db.execute(select(User)), db.commit, db.begin):
            with pytest.raises(BusinessException) as error:
                await operation()
            assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


@pytest.mark.parametrize("mutation", ["dirty", "flushed", "core"])
async def test_handoff_refuses_any_prior_write_even_after_flush(
    system_sessions, mutation
):
    _, plain = system_sessions
    async with RequestSession(bind=plain.kw["bind"], expire_on_commit=False) as db:
        if mutation == "core":
            await db.execute(
                update(User).where(User.user_id == 101).values(nickname="x")
            )
        else:
            user = await db.get(User, 101)
            user.nickname = "x"
            if mutation == "flushed":
                await db.flush()
        with pytest.raises(BusinessException):
            await release_request_reads(db)
        await db.rollback()
        if mutation != "dirty":
            with pytest.raises(BusinessException):
                await release_request_reads(db)
