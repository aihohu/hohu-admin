"""Phase caps share, rather than reset, the owner's cumulative budget."""

import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.modules.system.models.user import User
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    authorization_read_phase,
    bounded_authorization_reads,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_phase_limit_rejects_before_another_select(plan_sessions):
    factory, _ = plan_sessions
    budget = AuthorizationReadBudget(max_statements=4, phase_limits={"projection": 1})
    async with factory() as db:
        with bounded_authorization_reads(db, budget):
            with authorization_read_phase(db, "projection"):
                await db.execute(select(User.user_id).where(User.user_id == 101))
                with pytest.raises(BusinessException) as error:
                    await db.execute(select(User.user_id))
                assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        assert budget.statements == 1
        assert budget.phase_statements == {"projection": 1}
        assert budget.phase_rows == {"projection": 1}
        assert budget.phase_bytes["projection"] > 0
        await db.rollback()


async def test_phase_switches_do_not_reset_the_total_limit(plan_sessions):
    factory, _ = plan_sessions
    budget = AuthorizationReadBudget(
        max_statements=1, phase_limits={"identity": 2, "projection": 2}
    )
    async with factory() as db:
        with bounded_authorization_reads(db, budget):
            with authorization_read_phase(db, "identity"):
                await db.execute(select(User.user_id).where(User.user_id == 101))
            with authorization_read_phase(db, "projection"):
                with pytest.raises(BusinessException):
                    await db.execute(select(User.user_id))
        assert budget.statements == 1
        await db.rollback()


async def test_phase_cannot_be_borrowed_by_another_session(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as first, factory() as second:
        with bounded_authorization_reads(first):
            with pytest.raises(BusinessException):
                with authorization_read_phase(second, "projection"):
                    pass


async def test_nested_manifest_budget_keeps_the_outer_phase_and_limit(plan_sessions):
    factory, _ = plan_sessions
    budget = AuthorizationReadBudget(max_statements=1, phase_limits={"manifest": 1})
    async with factory() as db:
        with bounded_authorization_reads(db, budget):
            with authorization_read_phase(db, "manifest"):
                with bounded_authorization_reads(db):
                    await db.execute(select(User.user_id).where(User.user_id == 101))
        assert budget.phase_statements == {"manifest": 1}
        await db.rollback()
