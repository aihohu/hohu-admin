"""JWT, strict owner and FastAPI serialization preserve sparse ordinary DTOs."""

from sqlalchemy import select

from app.modules.system.api import dict_data, dict_type
from app.modules.system.models.dict_type import DictType
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_ordinary_field_active import activate


async def test_real_http_never_reinserts_hidden_keys_and_patch_is_closed(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    app = client._transport.app
    app.include_router(dict_type.router, prefix="/dict-type")
    app.include_router(dict_data.router, prefix="/dict-data")
    created = await client.post(
        "/dict-type/add",
        json={"dictName": "HTTP", "dictType": "http_type", "status": "1"},
    )
    assert created.status_code == 200, created.text
    async with plain() as db:
        key = await db.scalar(select(DictType.dict_type_id))
    await activate(
        system_sessions,
        monkeypatch,
        missing=(("dict_name", "read"), ("remark", "update")),
    )
    listed = await client.get("/dict-type/list")
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    row = data["records"][0]
    assert row["dictTypeId"] == str(key)
    assert "dictName" not in row and "tenantId" not in row
    assert "dict_name" not in data["fieldPolicy"]["read"]
    assert "remark" not in row["fieldPolicy"]["update"]
    denied = await client.put(f"/dict-type/{key}", json={"remark": "denied"})
    assert denied.status_code == 403, denied.text
    malformed = await client.put(f"/dict-type/{key}", json={"tenantId": "1"})
    assert malformed.status_code == 422, malformed.text
    options = await client.get("/dict-type/all")
    assert options.status_code == 403, options.text
    async with plain() as db:
        assert (await db.get(DictType, key)).remark is None
