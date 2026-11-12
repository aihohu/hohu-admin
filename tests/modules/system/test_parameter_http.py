"""Real request adapters hand identity to the fresh control owner."""

import pytest

from app.modules.system.api import config, settings
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
async def parameter_http(http_system):
    client, plain = http_system
    client._transport.app.include_router(config.router, prefix="/config")
    client._transport.app.include_router(settings.router, prefix="/settings")
    return client, plain


async def test_old_web_configuration_write_requires_current_form_receipt(
    parameter_http,
):
    client, _ = parameter_http
    payload = {
        "configName": "Example",
        "configKey": "business.example",
        "configValue": "value",
        "configGroup": "business",
        "status": "1",
    }
    denied = await client.post("/config/add", json=payload)
    assert denied.status_code == 409, denied.text
    assert denied.json()["errorCode"] == "FIELD_POLICY_PROTOCOL_REQUIRED"
    form = await client.get("/config/form", params={"mode": "create"})
    assert form.status_code == 200, form.text
    created = await client.post(
        "/config/add",
        json={**payload, "formReceipt": form.json()["data"]["formReceipt"]},
    )
    assert created.status_code == 200, created.text
    listed = await client.get("/config/list")
    assert listed.status_code == 200, listed.text
    row = listed.json()["data"]["records"][0]
    assert row["configValue"] == "value" and row["configId"].isdigit()
    identifier = row["configId"]
    denied = await client.put("/config/" + identifier, json={"remark": "old full form"})
    assert denied.status_code == 409, denied.text
    form = await client.get(
        "/config/form", params={"mode": "update", "recordId": identifier}
    )
    assert form.status_code == 200, form.text
    saved = await client.put(
        "/config/" + identifier,
        json={
            "remark": "new sparse form",
            "formReceipt": form.json()["data"]["formReceipt"],
        },
    )
    assert saved.status_code == 200, saved.text
    stale = await client.put(
        "/config/" + identifier,
        json={"remark": "replay", "formReceipt": form.json()["data"]["formReceipt"]},
    )
    assert stale.status_code == 409, stale.text


async def test_settings_group_uses_current_owner_and_dirty_receipt(parameter_http):
    client, _ = parameter_http
    current = await client.get("/settings/brand")
    assert current.status_code == 200, current.text
    body = current.json()["data"]
    assert (
        body.get("formReceipt")
        and body["fieldPolicy"]["readLevels"]["site_name"] == "plain"
    )
    old = await client.put(
        "/settings/brand",
        json={"revision": body["revision"], "values": {"site_name": "old web"}},
    )
    assert old.status_code == 409, old.text
    saved = await client.put(
        "/settings/brand",
        json={
            "revision": body["revision"],
            "values": {"site_name": "new owner"},
            "formReceipt": body["formReceipt"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["values"]["site_name"] == "new owner"
    mail = await client.get("/settings/mail")
    assert mail.status_code == 200, mail.text
    assert mail.json()["data"]["values"]["password"] == ""
    runtime = await client.get("/settings/runtime")
    assert runtime.status_code == 200, runtime.text
    assert set(runtime.json()["data"]) == {
        "defaultLocale",
        "requirePrimaryDept",
        "brand",
        "defaultAvatar",
        "uploads",
    }
    groups = await client.get("/settings")
    assert groups.status_code == 200, groups.text
    assert "mail" in groups.json()["data"]
