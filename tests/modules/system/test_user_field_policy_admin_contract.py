"""Public control-plane input and output must remain closed and value-free."""

import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.system.schemas.user_field_policy import PolicyDraft
from app.modules.system.service.user_field_policy_admin import (
    policy_catalog,
    policy_rule_diff,
)


def test_policy_catalog_contains_enumerated_capabilities_without_secret_reads():
    result = policy_catalog()
    assert len(result["catalogDigest"]) == 64
    fields = {item["fieldId"]: item for item in result["fields"]}
    assert fields["user_phone"]["maskId"] == "phone/v1"
    assert "hashed_password" not in fields
    assert fields["password"]["readable"] is False


def test_control_plane_routes_publish_typed_response_contracts():

    schema = app.openapi()
    route = schema["paths"]["/system/user-field-policy/{role_id}"]["get"]
    assert "PolicyReadResult" in str(route["responses"]["200"])
    fields = schema["components"]["schemas"]["PolicyStateOut"]["properties"]
    assert "canActivate" in fields and "activationBlockers" in fields


async def test_public_policy_routes_require_authentication(client):
    result = await client.get("/system/user-field-policy/201")
    assert result.status_code == 401


def test_draft_rejects_unknown_tenant_and_arbitrary_expressions():
    with pytest.raises(ValidationError):
        PolicyDraft(expectedVersion=0, catalogDigest="a" * 64, rules=[], tenantId="1")
    with pytest.raises(ValidationError):
        PolicyDraft(expectedVersion=0, catalogDigest="a" * 64, rules=[{"fieldId": "*"}])


def test_rules_preserve_static_field_ids_and_diff_never_contains_business_values():
    draft = PolicyDraft(
        expectedVersion=0,
        catalogDigest="a" * 64,
        rules=[
            {
                "fieldId": "user_email",
                "operation": "read",
                "purpose": "management.read",
                "effect": "allow",
                "read": "masked",
                "maskId": "email/v1",
            }
        ],
    )
    rules = draft.grants(201)
    assert rules[0].field_id == "user_email"
    changes = policy_rule_diff((), rules)
    assert changes == [
        {
            "fieldId": "user_email",
            "operation": "read",
            "purpose": "management.read",
            "before": None,
            "after": {"effect": "allow", "read": "masked", "maskId": "email/v1"},
        }
    ]
