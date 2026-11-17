"""Configuration XLSX delivery rechecks current authority after the encoder."""

from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import delete, event, insert, update

from app.core.exceptions import BusinessException
from app.db import authorization_parameter_read_owner as parameter_owner
from app.modules.system.models.config import Config
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
from app.modules.system.service import parameter_export
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


async def seed(plain):
    values = {
        "tenant_id": 0,
        "config_name": "Export source",
        "config_key": "business.export-source",
        "config_value": "current export value",
        "config_type": "text",
        "config_group": "business",
        "status": "1",
        "value_sensitivity": "private",
    }
    async with plain() as db:
        await db.execute(insert(Config).values(config_id=90001, **values))
        await db.commit()
    return values


@pytest.mark.parametrize("fault", ["disabled", "global", "source", "classification"])
async def test_config_export_refuses_changed_authority_after_encoding(
    parameter_http, monkeypatch, fault
):
    client, plain = parameter_http
    values = await seed(plain)
    original = parameter_export._worker.run
    active, encoded = set(), []
    engine = plain.kw["bind"].sync_engine

    def begin(connection):
        active.add(connection)

    def finish(connection):
        active.discard(connection)

    async def encode(*args, **kwargs):
        assert not active
        result = await original(*args, **kwargs)
        encoded.append(True)
        async with plain() as db:
            if fault == "disabled":
                await db.execute(
                    update(User).where(User.user_id == 101).values(status="2")
                )
            elif fault == "global":
                await db.execute(
                    update(UserFieldPolicyGlobalGuard).values(
                        epoch=UserFieldPolicyGlobalGuard.epoch + 1
                    )
                )
            elif fault == "source":
                await db.execute(
                    delete(Config).where(
                        Config.tenant_id == 0, Config.config_id == 90001
                    )
                )
                await db.execute(insert(Config).values(config_id=90002, **values))
            else:
                await db.execute(
                    update(Config)
                    .where(Config.config_id == 90001)
                    .values(value_sensitivity="secret")
                )
            await db.commit()
        return result

    monkeypatch.setattr(parameter_export._worker, "run", encode)
    for name, fn in (("begin", begin), ("commit", finish), ("rollback", finish)):
        event.listen(engine, name, fn)
    try:
        response = await client.get("/config/export")
    finally:
        for name, fn in (("begin", begin), ("commit", finish), ("rollback", finish)):
            event.remove(engine, name, fn)
    assert encoded == [True]
    assert response.status_code in {401, 403, 409}, response.text
    assert response.headers.get("content-type", "").startswith("application/json")


async def test_config_export_never_releases_bytes_after_unknown_second_rollback(
    parameter_http, monkeypatch
):
    client, plain = parameter_http
    await seed(plain)
    original = parameter_owner.rollback_authorization_session
    calls = []

    async def rollback(db):
        calls.append(True)
        await original(db)
        if len(calls) == 2:
            raise BusinessException(
                503,
                "Injected unknown rollback",
                error_code="AUTHORIZATION_ROLLBACK_UNKNOWN",
            )

    monkeypatch.setattr(parameter_owner, "rollback_authorization_session", rollback)
    response = await client.get("/config/export")
    assert response.status_code == 503, response.status_code
    assert response.json()["errorCode"] == "AUTHORIZATION_ROLLBACK_UNKNOWN"
    assert len(calls) == 2


async def test_config_export_delivers_only_after_both_confirmed_read_scopes(
    parameter_http, monkeypatch
):
    client, plain = parameter_http
    await seed(plain)
    original = parameter_owner.rollback_authorization_session
    calls = []

    async def rollback(db):
        await original(db)
        calls.append(True)

    monkeypatch.setattr(parameter_owner, "rollback_authorization_session", rollback)
    response = await client.get("/config/export")
    assert response.status_code == 200, response.text
    assert calls == [True, True]
    book = load_workbook(BytesIO(response.content), read_only=True)
    try:
        assert "current export value" in [
            cell for row in book.active.values for cell in row
        ]
    finally:
        book.close()
