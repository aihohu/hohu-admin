"""Advisory buttons never infer import or run from ordinary write fields."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from app.core.authorization_plan import TenantLock
from app.core.field_policy import Grant, compile_rules
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as control_catalog,
)
from app.modules.system.schemas.parameter_projection import ParameterActions
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.parameter_context import (
    ParameterContext,
    load_parameter_context,
)
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_parameter_read_owner import parameters as parameters
from tests.modules.system.test_parameter_read_owner import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_parameter_read_owner import (
    system_sessions as system_sessions,
)


def context(*, missing=(), permission=None, obj="system.config"):
    catalog = control_catalog()
    grants = tuple(
        Grant(0, "201", ref, access)
        for ref, field in catalog.fields.items()
        for access in field.accesses
        if (ref.object_id, ref.field_id, access.operation, access.purpose)
        not in missing
    )
    permissions = frozenset(
        p
        for action in catalog.actions.values()
        for p in action.permission_codes
        if p != permission
    )
    result = ParameterContext(
        SimpleNamespace(tenant_id=0),
        None,
        compile_rules(catalog, grants),
        {"201": None},
        permissions,
        "config.list" if obj == "system.config" else "job.list",
    )
    return result, result.record(obj, "page", result.scopes)


def test_ordinary_create_does_not_grant_import_and_actions_need_explicit_functions():
    current, record = context(
        missing=(("system.config", "config_value", "create", "import"),)
    )
    policy = current.capabilities(record)
    assert "config_value" in policy["create"]
    assert policy["control_actions"] == {
        "can_import": False,
        "can_export": True,
        "can_run": False,
    }
    current, record = context(permission="system:config:import")
    assert current.capabilities(record)["control_actions"]["can_import"] is False


def test_import_match_and_read_export_are_independent_footprints():
    current, record = context(
        missing=(("system.config", "config_key", "exact", "import"),)
    )
    assert current.capabilities(record)["control_actions"]["can_import"] is False
    current, record = context(permission="system:config:export")
    assert current.capabilities(record)["control_actions"]["can_export"] is False
    current, record = context()
    assert current.capabilities(record)["control_actions"]["can_import"] is True
    assert (
        current.capabilities(
            record,
            hidden=tuple(
                f.field_id for f in current.catalog.objects["system.config"].fields
            ),
        )["control_actions"]["can_export"]
        is False
    )


@pytest.mark.parametrize("hidden", [(), ("job_args",), ("status",)])
def test_job_run_requires_its_read_footprint_and_intrinsic_projection_ceiling(hidden):
    current, record = context(obj="job.definition")
    actions = current.capabilities(record, hidden=hidden)["control_actions"]
    assert actions == {"can_import": False, "can_export": False, "can_run": not hidden}
    current, record = context(obj="job.definition", permission="system:job:run")
    assert current.capabilities(record)["control_actions"]["can_run"] is False


def test_action_output_is_closed_and_requires_actual_booleans():
    for values in (
        {"canImport": 1, "canExport": True, "canRun": False},
        {"canImport": True, "canExport": True, "canRun": False, "grant": True},
    ):
        with pytest.raises(ValidationError):
            ParameterActions.model_validate(values)


async def test_action_hints_add_zero_sql_inside_the_existing_real_owner_barriers(
    parameters,
):
    factory, plain = parameters
    sql = []

    def capture(_connection, _cursor, statement, _params, _context, _many):
        sql.append(statement)

    async with factory() as db:
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                current = await load_parameter_context(
                    db, tx, tenant=TENANT, actor_user_version=1, action="config.list"
                )
                event.listen(
                    plain.kw["bind"].sync_engine, "before_cursor_execute", capture
                )
                try:
                    policy = current.capabilities(
                        current.record("system.config", "page", current.scopes)
                    )
                finally:
                    event.remove(
                        plain.kw["bind"].sync_engine, "before_cursor_execute", capture
                    )
                assert sql == []
                assert policy["control_actions"] == {
                    "can_import": True,
                    "can_export": True,
                    "can_run": False,
                }
