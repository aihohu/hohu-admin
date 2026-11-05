"""Audit identity copies use stable source IDs and current field projection."""

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import event, func, insert, select

from app.core.authorization_plan import LockMode, TenantLock
from app.core.exceptions import BusinessException
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.api import login_log, operation_log
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.schemas.login_log import LoginLogQuery
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.operation_log_service import operation_log_service
from app.modules.system.service.user_copy_read_service import user_copy_read_service
from app.modules.system.service.user_read_service import user_read_service
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_audit_copy_never_guesses_an_identity_from_historical_text(http_system):
    client, plain = http_system
    client._transport.app.include_router(operation_log.router, prefix="/operation-log")
    async with plain() as setup:
        await setup.execute(
            insert(SysOperationLog),
            [
                {
                    "tenant_id": 0,
                    "operation_log_id": 901,
                    "user_id": 101,
                    "username": "historical raw name",
                    "module": "system",
                    "action": "update",
                    "method": "PUT",
                    "path": "/user/101",
                    "request_params": "private historical payload",
                    "create_time": datetime(2026, 1, 1),
                },
                {
                    "tenant_id": 0,
                    "operation_log_id": 902,
                    "user_id": 999,
                    "username": "planner",
                    "module": "system",
                    "action": "update",
                    "method": "PUT",
                    "path": "/user/999",
                    "request_params": "unproven raw value",
                    "create_time": datetime(2026, 1, 2),
                },
            ],
        )
        await setup.commit()
    response = await client.get("/operation-log/list")
    assert response.status_code == 200, response.text
    rows = {row["operationLogId"]: row for row in response.json()["data"]["records"]}
    # Unactivated legacy/ready retains the original audit facts and display.
    assert rows["901"]["username"] == "historical raw name"
    assert rows["902"]["username"] == "planner"
    assert rows["901"]["requestParams"] == "private historical payload"
    runtime = AuthorizationRuntime(plain.kw["bind"])

    async def projected(query, mode=LockMode.EXCLUSIVE):
        async with runtime.sessions() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db, tenants=(TenantLock(0, mode),)
                    ) as tx:
                        actor = await user_read_service.actor(
                            db, tenant=TENANT, actor_user_version=1
                        )
                        context = await user_read_service.legacy_context(
                            db, tx, tenant=TENANT, actor_user_version=1, actor=actor
                        )
                        return await user_copy_read_service.projected_audit_page(
                            db,
                            tx,
                            tenant=TENANT,
                            context=context,
                            operation="operation-logs",
                            query=query,
                        )
            finally:
                await rollback_authorization_session(db)

    strict = await projected(OperationLogQuery())
    rows = {row["operationLogId"]: row for row in strict["records"]}
    assert rows["901"]["username"] == "historical raw name"
    assert "username" not in rows["902"]
    assert all("requestParams" not in row for row in rows.values())
    with pytest.raises(BusinessException) as error:
        await projected(OperationLogQuery(), mode=LockMode.SHARED)
    assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
    with pytest.raises(BusinessException) as error:
        await projected(OperationLogQuery(username="nobody matches"))
    assert error.value.error_code == "FIELD_QUERY_DENIED"


async def test_login_input_does_not_prove_a_canonical_username_source(http_system):
    client, plain = http_system
    client._transport.app.include_router(login_log.router, prefix="/login-log")
    async with plain() as setup:
        await setup.execute(
            insert(SysLoginLog).values(
                tenant_id=0,
                audit_scope="tenant",
                login_log_id=903,
                user_id=101,
                username="historical@example.com",
                message="unstructured private input",
            )
        )
        await setup.commit()
    response = await client.get("/login-log/list")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["records"][0]["username"] == "historical@example.com"
    runtime = AuthorizationRuntime(plain.kw["bind"])

    async def projected(query):
        async with runtime.sessions() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
                    ) as tx:
                        actor = await user_read_service.actor(
                            db, tenant=TENANT, actor_user_version=1
                        )
                        context = await user_read_service.legacy_context(
                            db,
                            tx,
                            tenant=TENANT,
                            actor_user_version=1,
                            actor=actor,
                        )
                        return await user_copy_read_service.projected_audit_page(
                            db,
                            tx,
                            tenant=TENANT,
                            context=context,
                            operation="login-logs",
                            query=query,
                        )
            finally:
                await rollback_authorization_session(db)

    strict = await projected(LoginLogQuery())
    assert strict["total"] == 1
    assert "username" not in strict["records"][0]
    assert "message" not in strict["records"][0]
    with pytest.raises(BusinessException) as error:
        await projected(LoginLogQuery(username="not present"))
    assert error.value.error_code == "FIELD_QUERY_DENIED"


async def test_audit_append_cannot_enter_between_coverage_and_filtered_page(
    system_sessions,
    monkeypatch,
):
    _, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            insert(SysOperationLog).values(
                tenant_id=0,
                operation_log_id=904,
                user_id=101,
                username="initial",
                module="system",
                action="update",
                method="PUT",
                path="/user/101",
            )
        )
        await setup.commit()
    runtime = AuthorizationRuntime(plain.kw["bind"])
    covered, release = asyncio.Event(), asyncio.Event()
    original = operation_log_service.get_list
    pids = {}
    engine = plain.kw["bind"]

    def observe(conn, _cursor, _sql, _params, _context, _many):
        name = asyncio.current_task().get_name()
        if name in {"strict-audit-reader", "strict-audit-writer"}:
            pids[name] = conn.connection.driver_connection.get_server_pid()

    event.listen(engine.sync_engine, "before_cursor_execute", observe)

    async def paused(*args, **kwargs):
        covered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(operation_log_service, "get_list", paused)

    async def projected():
        async with runtime.sessions() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(0, LockMode.EXCLUSIVE),),
                    ) as tx:
                        actor = await user_read_service.actor(
                            db, tenant=TENANT, actor_user_version=1
                        )
                        context = await user_read_service.legacy_context(
                            db,
                            tx,
                            tenant=TENANT,
                            actor_user_version=1,
                            actor=actor,
                        )
                        return await user_copy_read_service.projected_audit_page(
                            db,
                            tx,
                            tenant=TENANT,
                            context=context,
                            operation="operation-logs",
                            query=OperationLogQuery(username="initial"),
                        )
            finally:
                await rollback_authorization_session(db)

    reader = asyncio.create_task(projected(), name="strict-audit-reader")
    writer = None
    try:
        await asyncio.wait_for(covered.wait(), 3)
        writer = asyncio.create_task(
            runtime.http_audit.operation(
                tenant_id=0,
                user_id=999,
                username="initial hidden",
                module="system",
                action="update",
                method="PUT",
                path="/user/999",
                request_params=None,
                status_code=200,
                ip=None,
                duration=1,
            ),
            name="strict-audit-writer",
        )
        async with asyncio.timeout(0.4), plain() as probe:
            while True:
                if "strict-audit-writer" in pids:
                    blockers = await probe.scalar(
                        select(func.pg_blocking_pids(pids["strict-audit-writer"]))
                    )
                    if pids["strict-audit-reader"] in blockers:
                        break
                await asyncio.sleep(0)
        assert not writer.done()
        release.set()
        page = await asyncio.wait_for(reader, 3)
        await asyncio.wait_for(writer, 3)
        assert page["total"] == 1
        assert [row["operationLogId"] for row in page["records"]] == ["904"]
        with pytest.raises(BusinessException) as error:
            await projected()
        assert error.value.error_code == "FIELD_QUERY_DENIED"
    finally:
        release.set()
        for task in (reader, writer):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (reader, writer) if task is not None),
            return_exceptions=True,
        )
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
