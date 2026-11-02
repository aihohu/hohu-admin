"""Field evidence is closed, value-free and bound to one authority snapshot."""

from copy import deepcopy

import pytest

from app.core.exceptions import AuthorizationException
from app.modules.ai.service.field_authority import (
    freeze_field_authority,
    require_current_field_authority,
    validate_field_authority,
)


def evidence(**overrides):
    args = {
        "tenant_id": 7,
        "actor_id": 11,
        "global_epoch": 2,
        "tenant_epoch": 3,
        "mode": "legacy",
        "active_digest": None,
        "build": "test-build",
        "purpose": "ai.run",
        "fields": ("nickname", "user_name"),
    }
    args.update(overrides)
    return freeze_field_authority(**args)


def test_evidence_is_canonical_and_value_free():
    value = evidence()
    assert value == evidence(fields=("user_name", "nickname", "user_name"))
    assert value["tenant_id"] == "7" and value["actor_id"] == "11"
    assert validate_field_authority(value) == value
    assert require_current_field_authority(value, value) is None


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "actor_id",
        "global_epoch",
        "tenant_epoch",
        "build",
        "purpose",
        "fields",
        "catalog_digest",
        "digest",
    ],
)
def test_mutation_never_passes_validation(field):
    value = deepcopy(evidence())
    value[field] = "tampered"
    assert validate_field_authority(value) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": 8},
        {"actor_id": 12},
        {"global_epoch": 4},
        {"tenant_epoch": 5},
        {"build": "other"},
        {"purpose": "ai.confirmation"},
        {"fields": ("user_name",)},
    ],
)
def test_changed_authority_requires_new_preview(overrides):
    with pytest.raises(AuthorizationException) as caught:
        require_current_field_authority(evidence(), evidence(**overrides))
    assert caught.value.error_code == "FIELD_AUTHORIZATION_CHANGED"


def test_missing_old_confirmation_is_not_backfilled():
    with pytest.raises(AuthorizationException) as caught:
        require_current_field_authority(None, evidence(), confirmation=True)
    assert caught.value.error_code == "FIELD_CONFIRMATION_LINEAGE_REQUIRED"


@pytest.mark.parametrize(
    "value", [None, {}, [], True, {"schema": 1, "password": "secret"}]
)
def test_unproven_body_has_no_authority(value):
    assert validate_field_authority(value) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": True},
        {"actor_id": 0},
        {"global_epoch": -1},
        {"tenant_epoch": True},
        {"mode": "invalid"},
        {"fields": ("password",)},
        {"fields": ("unknown",)},
        {"purpose": "user supplied"},
        {"build": "x" * 1025},
    ],
)
def test_invalid_or_secret_dependency_is_rejected(overrides):
    with pytest.raises(AuthorizationException):
        evidence(**overrides)


def test_history_dependency_budget_rejects_before_sql():
    from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
        result_projection_service,
    )

    with pytest.raises(ValueError):
        result_projection_service.normalize_projection_dependency_message_ids(
            tuple(range(1, 4098))
        )
