"""Nested snapshot reads share a source ledger until each operation finishes."""

import pytest

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
    charge_source,
)
from app.modules.system.service.transfer_delivery_snapshot import DeliveryReadBudget
from tests.modules.system.test_relation_field_reads import source_context


async def test_same_session_nested_reads_inherit_and_restore_the_source_ledger():
    parent, child = DeliveryReadBudget(), AuthorizationReadBudget()
    async with AuthorizationPlanSession() as db:
        with bounded_authorization_reads(db, parent):
            with bounded_authorization_reads(db, child):
                assert child.source_budget is parent.source_budget
                charge_source(b'"first"')
            assert child.source_budget is None
            charge_source(b'"second"')
        assert parent.source_budget.references == 2
    assert child.source_budget is None


async def test_different_sessions_do_not_inherit_an_implicit_source_ledger():
    parent, child = DeliveryReadBudget(), AuthorizationReadBudget()
    async with (
        AuthorizationPlanSession() as first,
        AuthorizationPlanSession() as second,
    ):
        with bounded_authorization_reads(first, parent):
            with bounded_authorization_reads(second, child):
                assert child.source_budget is None
                charge_source(b'"separate"')
            charge_source(b'"first"')
        # The same explicit operation budget is reused for the second snapshot.
        with bounded_authorization_reads(second, parent):
            charge_source(b'"second"')
    assert parent.source_budget.references == 2
    assert DeliveryReadBudget().source_budget.references == 0
    assert DeliveryReadBudget().source_budget is not parent.source_budget


async def test_a_nested_read_cannot_replace_the_active_source_ledger():
    parent, child = DeliveryReadBudget(), DeliveryReadBudget()
    async with AuthorizationPlanSession() as db:
        with bounded_authorization_reads(db, parent):
            with pytest.raises(BusinessException) as error:
                with bounded_authorization_reads(db, child):
                    pytest.fail(
                        "A new source ledger must not replace the active ledger"
                    )
            assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
            charge_source(b'"parent"')
    assert parent.source_budget.references == 1
    assert child.source_budget.references == 0


@pytest.mark.parametrize("resource", ["references", "encoded_bytes"])
async def test_second_snapshot_first_excess_source_is_rejected_before_append(resource):
    budget = DeliveryReadBudget()
    setattr(
        budget.source_budget,
        resource,
        16384 if resource == "references" else 4 * 1024 * 1024,
    )
    context = source_context()
    async with AuthorizationPlanSession() as db:
        with bounded_authorization_reads(db, budget):
            with pytest.raises(BusinessException) as error:
                context.source(
                    context.record("system.role", "301", ("201",)), ("role_name",)
                )
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    assert context.source_refs == []
