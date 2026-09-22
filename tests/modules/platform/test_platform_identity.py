from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.security import create_access_token, create_platform_access_token
from app.db.session import get_db
from app.main import app
from app.modules.platform.audit import decode_platform_reason


@pytest.mark.parametrize("mode", ["valid", "missing", "tenant", "disabled", "revoked"])
async def test_identity_uses_live_platform_principal(client, mode):
    principal = SimpleNamespace(
        principal_id=9007199254740993,
        principal_name="platform-reader",
        status="0" if mode == "disabled" else "1",
        row_version=2 if mode == "revoked" else 1,
        permissions=["platform:ai:read"],
        hashed_password="never-return-this",
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    token = create_platform_access_token(
        subject=str(principal.principal_id), principal_version=1
    )
    if mode == "tenant":
        token = create_access_token("1", tenant_id=0, tenant_version=1, user_version=1)
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = await client.get(
            "/platform/auth/me",
            headers={} if mode == "missing" else {"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    if mode == "valid":
        assert response.status_code == 200
        assert response.json()["data"] == {
            "principalId": "9007199254740993",
            "principalName": "platform-reader",
            "permissions": ["platform:ai:read"],
        }
    else:
        assert response.status_code in {401, 403}
        assert (
            "permissions" not in response.json().get("data", {})
            if response.json().get("data")
            else True
        )
    assert "never-return-this" not in response.text


def test_browser_audit_reason_decodes_utf8_without_changing_cli():
    assert decode_platform_reason("%E8%B0%83%E6%95%B4", "uri-component") == "调整"
    assert decode_platform_reason("CLI review 50%", None) == "CLI review 50%"
    assert decode_platform_reason("%FF", "uri-component") is None
    assert decode_platform_reason("anything", "unknown") is None
