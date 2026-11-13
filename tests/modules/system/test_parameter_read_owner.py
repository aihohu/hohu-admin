"""The live parameter boundary masks in SQL and proves queries before paging."""

import pytest
from sqlalchemy import event, insert

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.db import authorization_parameter_read_owner as owner
from app.db.authorization_parameter_read_owner import parameter_read_owner
from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.models.config import Config
from app.modules.system.schemas.config import ConfigQuery
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.parameter_context import load_parameter_context
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


@pytest.fixture
async def parameters(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        for key, sensitivity, tenant in (
            (301, "private", 0),
            (302, "unknown", 0),
            (303, "secret", 0),
            (304, "private", 1),
        ):
            await db.execute(
                insert(Config).values(
                    config_id=key,
                    tenant_id=tenant,
                    config_name="configuration",
                    config_key=f"business.{key}",
                    config_value=f"value-{key}",
                    config_type="text",
                    config_group="business",
                    status="1",
                    value_sensitivity=sensitivity,
                )
            )
        await db.commit()
    return factory, plain


async def read(factory, query=None, version=1):
    async with factory() as db:
        return await parameter_read_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=version,
            operation="config.list",
            query=query or ConfigQuery(),
        )


async def test_unknown_secret_and_other_tenant_values_are_never_projected(parameters):
    factory, plain = parameters
    sql = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if "sys_config.config_value" in statement:
            sql.append(statement)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", capture)
    try:
        result = await read(factory)
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", capture)
    assert result["total"] == 3
    rows = {row["configId"]: row for row in result["records"]}
    assert rows["301"]["configValue"] == "value-301"
    assert "configValue" not in rows["302"] and "configValue" not in rows["303"]
    assert "304" not in rows
    assert all(
        "createBy" not in row and "valueSensitivity" not in row for row in rows.values()
    )
    assert sql and all(
        "CASE WHEN" in statement and "value_sensitivity" in statement
        for statement in sql
    )
    with pytest.raises(BusinessException):
        await read(factory, version=99)


async def test_query_proof_runs_over_original_scope_before_filter_and_page(
    parameters, monkeypatch
):
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("system.config", "config_name", "contains"),),
    )
    with pytest.raises(BusinessException) as denied:
        await read(
            parameters[0], ConfigQuery(config_name="not-present", current=999, size=1)
        )
    assert denied.value.error_code == "FIELD_POLICY_DENIED"


async def test_read_grant_never_infers_write_or_query(parameters, monkeypatch):
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(
            ("system.config", "config_value", "read"),
            ("system.config", "remark", "update"),
        ),
    )
    result = await read(parameters[0])
    assert all("configValue" not in row for row in result["records"])
    assert all(
        "remark" not in row["fieldPolicy"]["update"] for row in result["records"]
    )


async def test_rollback_ack_failure_cannot_return_prepared_records(
    parameters, monkeypatch
):

    async def failed(_db):
        raise BusinessException(
            503, "Unknown rollback", error_code="FIELD_POLICY_ROLLBACK_UNKNOWN"
        )

    monkeypatch.setattr(owner, "rollback_authorization_session", failed)
    with pytest.raises(BusinessException) as error:
        await read(parameters[0])
    assert error.value.error_code == "FIELD_POLICY_ROLLBACK_UNKNOWN"


async def test_export_headers_and_cells_require_both_read_and_export(
    parameters, monkeypatch
):
    await activate_relations(
        parameters,
        monkeypatch,
        catalog=control_catalog(),
        missing=(
            ("system.config", "config_name", "export"),
            ("system.config", "config_key", "read"),
        ),
    )
    async with parameters[0]() as db:
        result = await parameter_read_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="config.export",
            query=ConfigQuery(),
        )
    assert (
        "config_name" not in result["headers"] and "config_key" not in result["headers"]
    )
    assert len(result["records"]) == 3
    assert [row.get("config_value") for row in result["records"]] == [
        "value-301",
        None,
        None,
    ]


async def test_builtin_and_internal_legacy_rows_are_not_custom_parameters(parameters):
    factory, plain = parameters
    async with plain() as db:
        for identifier, key in enumerate(
            ("site_name", "ai:budget", "security:custom", "upload:private"), 800
        ):
            await db.execute(
                insert(Config).values(
                    tenant_id=0,
                    config_id=identifier,
                    config_name="Internal",
                    config_key=key,
                    config_value="internal bytes",
                    config_group="legacy",
                    status="1",
                    value_sensitivity="private",
                )
            )
        await db.commit()
    result = await read(factory)
    assert result["total"] == 3 and all(
        row["configName"] != "Internal" for row in result["records"]
    )


async def test_parameter_scope_adapter_cannot_be_reused_for_user_or_department_scope(
    parameters,
):
    for action in ("user.list-read", "dept.list"):
        async with parameters[0]() as db:
            with (
                bounded_authorization_reads(db),
                pytest.raises(BusinessException) as error,
            ):
                async with authorization_transactions.scope(
                    db, tenants=(TenantLock(0),)
                ) as tx:
                    await load_parameter_context(
                        db, tx, tenant=TENANT, actor_user_version=1, action=action
                    )
            assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
            await db.rollback()


async def test_query_payload_budget_is_checked_before_a_database_scope(parameters):
    with pytest.raises(BusinessException) as error:
        await read(parameters[0], ConfigQuery(config_name="x" * 8193))
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
