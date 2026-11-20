"""Both delivery scopes spend one cumulative budget, including the encoder wait."""

import asyncio

import pytest

from app.core.exceptions import BusinessException
from app.db import authorization_parameter_read_owner as parameters
from app.db import authorization_transfer_read_owner as templates
from app.db.authorization_transfer_read_owner import TransferReadOwner
from app.modules.system.service import parameter_export
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_parameter_export_delivery import seed
from tests.modules.system.test_parameter_http import (
    http_system as http_system,
)
from tests.modules.system.test_parameter_http import (
    parameter_http as parameter_http,
)
from tests.modules.system.test_parameter_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_parameter_http import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


def exhaust_after_encoding(monkeypatch, owner, worker, resource):
    budgets = []
    original_budget = owner.DeliveryReadBudget
    original_worker = worker.run

    def budget(*args, **kwargs):
        result = original_budget(*args, **kwargs)
        budgets.append(result)
        return result

    async def encode(*args, **kwargs):
        payload = await original_worker(*args, **kwargs)
        assert len(budgets) == 1
        observed = budgets[0]
        assert (
            observed.rows > 0 and observed.statements > 0 and observed.decoded_bytes > 0
        )
        if resource == "statements":
            observed.max_statements = observed.statements
        elif resource == "rows":
            observed.max_rows = observed.rows
        elif resource == "bytes":
            observed.max_bytes = observed.decoded_bytes
        elif resource == "source-bytes":
            observed.source_budget.encoded_bytes = 4 * 1024 * 1024
        else:
            observed.phase_limits = dict(observed.phase_limits)
            observed.phase_limits["barrier"] = observed.phase_statements["barrier"]
        return payload

    monkeypatch.setattr(owner, "DeliveryReadBudget", budget)
    monkeypatch.setattr(worker, "run", encode)
    return budgets


@pytest.mark.parametrize("resource", ["statements", "rows", "bytes", "source-bytes"])
async def test_template_second_scope_cannot_reset_the_spent_budget(
    import_sessions, monkeypatch, resource
):
    factory, plain, tenant = import_sessions
    if resource == "source-bytes":
        await activate_relations((factory, plain), monkeypatch)
    budgets = exhaust_after_encoding(
        monkeypatch, templates, templates.file_transform_worker, resource
    )
    with pytest.raises(BusinessException) as error:
        await TransferReadOwner(factory).execute(
            tenant=tenant, actor_user_version=1, operation="template"
        )
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    assert len(budgets) == 1


@pytest.mark.parametrize(
    "resource", ["statements", "rows", "bytes", "phase", "source-bytes"]
)
async def test_configuration_second_scope_cannot_reset_the_spent_budget(
    parameter_http, monkeypatch, resource
):
    client, plain = parameter_http
    if resource == "source-bytes":
        await seed(plain)
    budgets = exhaust_after_encoding(
        monkeypatch, parameters, parameter_export._worker, resource
    )
    response = await client.get("/config/export")
    assert response.status_code == 422, response.status_code
    assert response.json()["errorCode"] == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    assert len(budgets) == 1


async def test_template_shared_deadline_covers_the_wait_outside_database_scopes(
    import_sessions, monkeypatch
):
    factory, _, tenant = import_sessions
    cancelled = []

    async def pending(*_args, **_kwargs):
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(templates.file_transform_worker, "run", pending)
    with pytest.raises(BusinessException) as error:
        await TransferReadOwner(factory).execute(
            tenant=tenant, actor_user_version=1, operation="template"
        )
    assert error.value.error_code == "AUTHORIZATION_TRANSACTION_TIMEOUT"
    assert cancelled == [True]


async def test_configuration_shared_deadline_covers_the_wait_outside_database_scopes(
    parameter_http, monkeypatch
):
    client, _ = parameter_http
    cancelled = []

    async def pending(*_args, **_kwargs):
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(parameter_export._worker, "run", pending)
    response = await client.get("/config/export")
    assert response.status_code == 503, response.status_code
    assert response.json()["errorCode"] == "AUTHORIZATION_TRANSACTION_TIMEOUT"
    assert cancelled == [True]
