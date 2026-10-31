"""HTTP configuration writers use actual JWT, PostgreSQL and owner commits."""

import json
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import event, insert, select, update

from app.db import session
from app.modules.ai.api import role_agent
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.auth import service
from app.modules.system.api import config, menu, settings
from app.modules.system.models.config import Config
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.menu import Menu
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service import mail_service as transport
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def configuration_http(http_system):
    client, plain = http_system
    app = client._transport.app
    app.include_router(menu.router, prefix="/menu")
    app.include_router(config.router, prefix="/config")
    app.include_router(settings.router, prefix="/settings")
    async with plain() as db:
        await db.execute(insert(MailConfig).values(config_id=1, values={}, version=1))
        await db.execute(
            insert(Config).values(
                tenant_id=0,
                config_id=901,
                config_key="http.example",
                config_name="Original",
                config_value="before",
                config_type="text",
                config_group="custom",
                status="1",
                is_public=False,
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=902,
                menu_name="Original",
                menu_type="C",
                route_name="original",
                status="1",
            )
        )
        await db.commit()
    yield client, plain


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/menu/add", {"menuName": "Created", "menuType": "C", "status": "1"}),
        (
            "PUT",
            "/menu/902",
            {"menuName": "Renamed", "menuType": "C", "status": "1", "query": []},
        ),
        ("DELETE", "/menu/902", None),
        ("POST", "/menu/batch-delete", [902]),
        (
            "POST",
            "/config/add",
            {
                "configName": "Created",
                "configKey": "http.new",
                "configValue": "after",
                "configGroup": "custom",
                "status": "1",
            },
        ),
        ("PUT", "/config/901", {"configName": "Renamed", "configValue": "after"}),
        ("DELETE", "/config/901", None),
        ("POST", "/config/batch-delete", ["901"]),
    ],
)
async def test_configuration_http_one_commit_and_barrier(
    configuration_http, method, path, payload
):
    client, plain = configuration_http
    commits, locks = [], []
    engine = plain.kw["bind"].sync_engine

    def committed(connection):
        commits.append(connection)

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    event.listen(engine, "commit", committed)
    event.listen(engine, "before_cursor_execute", sql)
    try:
        response = await client.request(method, path, json=payload)
    finally:
        event.remove(engine, "commit", committed)
        event.remove(engine, "before_cursor_execute", sql)
    assert response.status_code == 200, response.text
    assert len(commits) == 1
    assert len(locks) >= 2 and "sys_user_field_policy_global_guard" in locks[0]
    assert "sys_user_field_policy_state" in locks[1]
    async with plain() as db:
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )
        if path == "/menu/add":
            assert (
                await db.scalar(
                    select(Menu.create_by).where(Menu.menu_name == "Created")
                )
                == "planner"
            )
        if path == "/menu/902" and method == "PUT":
            assert (
                await db.scalar(select(Menu.update_by).where(Menu.menu_id == 902))
                == "planner"
            )


async def test_configuration_import_preserves_counts(configuration_http):
    client, plain = configuration_http
    book = Workbook()
    book.active.append(["name", "key", "value", "type", "group", "status", "public"])
    book.active.append(
        ["Imported", "http.imported", "value", "text", "custom", "1", False]
    )
    stream = BytesIO()
    book.save(stream)
    response = await client.post(
        "/config/import",
        files={
            "file": (
                "config.xlsx",
                stream.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"success": 1, "skipped": 0}
    async with plain() as db:
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )


@pytest.mark.parametrize(
    "group,key,value",
    [
        ("security", "security:login_per_minute", 6),
        ("brand", "site_name", "HTTP brand"),
    ],
)
async def test_settings_http_contract_and_audit(configuration_http, group, key, value):
    client, plain = configuration_http
    current = await client.get("/settings/" + group)
    assert current.status_code == 200, current.text
    # Locate a real group field rather than rely on a mocked revision.
    values = current.json()["data"]["values"]
    if key not in values:
        pytest.fail(f"Fixture key missing: {group}/{key}; actual keys {list(values)}")
    response = await client.put(
        "/settings/" + group,
        json={"values": {key: value}, "revision": current.json()["data"]["revision"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["values"][key] == value
    assert "secretKeys" in response.json()["data"]
    async with plain() as db:
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )
        rows = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        if group == "security":
            assert [row.action for row in rows] == ["authorized", "completed"]
            assert all(
                row.path == "/settings/{group}" and row.method == "PUT" for row in rows
            )
            assert (
                json.loads(rows[-1].request_params)["authorization_audit_id"]
                == rows[0].operation_log_id
            )


async def test_mail_http_uses_actual_route_audit(configuration_http):
    client, plain = configuration_http
    current = await client.get("/settings/mail")
    assert current.status_code == 200, current.text
    data = current.json()["data"]
    response = await client.put(
        "/settings/mail",
        json={
            "values": {**data["values"], "host": "smtp.example.com"},
            "revision": data["revision"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["values"]["host"] == "smtp.example.com"
    async with plain() as db:
        rows = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in rows] == ["authorized", "completed"]
        assert all(row.path == "/settings/mail" and row.method == "PUT" for row in rows)


async def test_configuration_rechecks_revoked_token(configuration_http):

    client, plain = configuration_http
    original = service.get_current_user

    async def revoked(
        token=service.Depends(service.oauth2_scheme), db=service.Depends(session.get_db)
    ):
        actor = await original(token, db)
        async with plain() as change:
            await change.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await change.commit()
        return actor

    client._transport.app.dependency_overrides[original] = revoked
    response = await client.put("/config/901", json={"configValue": "must reject"})
    assert response.status_code == 401, response.text
    async with plain() as db:
        assert (
            await db.scalar(select(Config.config_value).where(Config.config_id == 901))
            == "before"
        )


async def test_mail_test_has_no_database_transaction_during_smtp(
    configuration_http, monkeypatch
):

    client, plain = configuration_http
    async with plain() as db:
        await db.execute(
            update(MailConfig).values(
                values={
                    "enabled": True,
                    "host": "smtp.example.com",
                    "port": 587,
                    "from_email": "sender@example.com",
                }
            )
        )
        await db.commit()
    active, sent = set(), []
    engine = plain.kw["bind"].sync_engine

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    def send(*_args):
        assert not active, "SMTP must run after all database transactions are released"
        sent.append(True)

    monkeypatch.setattr(transport, "_send", send)
    event.listen(engine, "begin", begin)
    event.listen(engine, "commit", end)
    event.listen(engine, "rollback", end)
    try:
        response = await client.post(
            "/settings/mail/test", json={"recipient": "receiver@example.com"}
        )
    finally:
        for name, callback in (("begin", begin), ("commit", end), ("rollback", end)):
            event.remove(engine, name, callback)
    assert response.status_code == 200, response.text
    assert sent == [True]
    assert response.json()["data"]["lastTestStatus"] == "accepted"


async def test_role_agent_http_uses_control_owner(http_system):

    client, plain = http_system
    client._transport.app.include_router(role_agent.router, prefix="/ai/role-agent")
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=303,
                role_name="member",
                role_code="HTTP_ROLE",
                status="1",
                data_scope="5",
            )
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=703,
                code="http_test",
                name="HTTP",
                description="A" * 50,
                enabled=True,
            )
        )
        await db.commit()
    response = await client.put("/ai/role-agent/303", json={"agentIds": ["703"]})
    assert response.status_code == 200, response.text
    assert response.json()["data"] is None
    async with plain() as db:
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )
        assert (
            await db.scalar(
                select(RoleAiAgent.enabled).where(
                    RoleAiAgent.tenant_id == 0,
                    RoleAiAgent.role_id == 303,
                    RoleAiAgent.agent_id == 703,
                )
            )
            is True
        )
