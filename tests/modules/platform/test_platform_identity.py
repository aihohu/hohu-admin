import pytest
from sqlalchemy import update

from app.core.security import create_access_token, create_platform_access_token
from app.modules.platform.audit import decode_platform_reason
from app.modules.platform.models import PlatformPrincipal
from tests.modules.platform.test_authorization_principal_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_principal_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_principal_http import (
    principal_http as principal_http,
)
from tests.modules.platform.test_authorization_principal_http import (
    principal_sessions as principal_sessions,
)
from tests.modules.platform.test_authorization_principal_http import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize("mode", ["valid", "missing", "tenant", "disabled", "revoked"])
async def test_identity_uses_live_platform_principal(principal_http, mode):
    client, plain = principal_http
    version = 1
    if mode in {"disabled", "revoked"}:
        async with plain() as db:
            await db.execute(update(PlatformPrincipal).values(status="2"))
            await db.commit()
        if mode == "disabled":
            version = 2
    token = create_platform_access_token(subject="700", principal_version=version)
    if mode == "tenant":
        token = create_access_token("1", tenant_id=0, tenant_version=1, user_version=1)
    client.headers.pop("Authorization", None)
    response = await client.get(
        "/platform/auth/me",
        headers={} if mode == "missing" else {"Authorization": f"Bearer {token}"},
    )
    if mode == "valid":
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "principalId": "700",
            "principalName": "control-admin",
            "permissions": ["platform:ai:read"],
        }
    else:
        assert response.status_code in {401, 403}, response.text
        assert not response.json().get("data")
    assert (
        "hashedPassword" not in response.text and "Principal123!" not in response.text
    )


def test_browser_audit_reason_decodes_utf8_without_changing_cli():
    assert decode_platform_reason("%E8%B0%83%E6%95%B4", "uri-component") == "调整"
    assert decode_platform_reason("CLI review 50%", None) == "CLI review 50%"
    assert decode_platform_reason("%FF", "uri-component") is None
    assert decode_platform_reason("anything", "unknown") is None
