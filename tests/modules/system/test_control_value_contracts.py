"""Classification and content fingerprints can only narrow delivery ceilings."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.control_values import (
    MASKED_VALUE,
    PUBLIC_TEXT_SCHEMA,
    ConfigContent,
    PublicationFact,
    prepare_value,
    project_value,
    publication_digest,
    publication_matches,
)


def content(**changes):
    return ConfigContent(
        tenant_id=7,
        config_id=301,
        config_key="notice",
        config_value="Hello",
        config_type="text",
        status="1",
        is_public=True,
        value_sensitivity="private",
        **changes,
    )


@pytest.mark.parametrize(
    "key",
    [
        "db.password",
        "api-key",
        "Api_Key",
        "ACCESS-TOKEN",
        "private.key",
        "smtpCredentials",
        "Authorization",
        "cookie",
        "headers",
    ],
)
def test_sensitive_key_variants_are_never_plaintext_after_explicit_input(key):
    result = prepare_value(key=key, config_type="text", replacement="sentinel-secret")
    assert result.sensitivity == "secret" and result.write_value
    assert not project_value(
        key=key, value=result.value, config_type="text", sensitivity=result.sensitivity
    ).visible
    assert "sentinel-secret" not in repr(result)


def test_unknown_requires_explicit_complete_input_and_secret_is_sticky():
    kept = prepare_value(key="ordinary", config_type="text", previous="unknown")
    assert not kept.write_value and kept.sensitivity == "unknown"
    new = prepare_value(key="ordinary", config_type="text", replacement="value")
    assert new.sensitivity == "private" and new.value == "value"
    for kind in ("text", "richtext"):
        changed = prepare_value(
            key="ordinary", config_type=kind, previous="secret", replacement="new value"
        )
        assert changed.sensitivity == "secret"
        assert (
            prepare_value(
                key="ordinary", config_type=kind, previous="secret"
            ).sensitivity
            == "secret"
        )
    with pytest.raises(FrozenInstanceError):
        new.sensitivity = "private"


@pytest.mark.parametrize("previous", ["unknown", "private", "secret"])
def test_legacy_mask_is_only_an_unchanged_marker(previous):
    result = prepare_value(
        key="ordinary", config_type="text", previous=previous, replacement=MASKED_VALUE
    )
    assert (
        result.sensitivity == previous
        and not result.write_value
        and result.value is None
    )


@pytest.mark.parametrize(
    "value",
    [
        '{"options":{"credential":"sentinel"}}',
        '[{"innocent":"sentinel"}]',
        ' \ufeff {"x":1}',
        "[broken",
        "{broken",
    ],
)
def test_unregistered_structured_values_are_rejected_as_a_whole(value):
    with pytest.raises(BusinessException) as error:
        prepare_value(key="ordinary", config_type="text", replacement=value)
    assert error.value.error_code == "CONFIG_VALUE_SCHEMA_UNREGISTERED"
    assert "sentinel" not in str(error.value)
    assert not project_value(
        key="ordinary", value=value, config_type="text", sensitivity="private"
    ).visible


@pytest.mark.parametrize("sensitivity", ["unknown", "secret"])
def test_history_cannot_be_reclassified_by_a_read(sensitivity):
    projection = project_value(
        key="ordinary",
        value="old original",
        config_type="text",
        sensitivity=sensitivity,
    )
    assert not projection.visible and projection.value is None


def test_plain_value_contract_is_not_a_grant_or_a_richtext_sanitizer():
    raw = '<script>alert("private input")</script>'
    decision = prepare_value(key="ordinary", config_type="richtext", replacement=raw)
    assert decision.value == raw
    assert (
        project_value(
            key="ordinary", value=raw, config_type="richtext", sensitivity="private"
        ).value
        == raw
    )
    assert not project_value(
        key="looks.secret", value="x", config_type="text", sensitivity="private"
    ).visible
    assert not project_value(
        key="file", value="/private/1", config_type="file", sensitivity="private"
    ).visible
    with pytest.raises(BusinessException):
        prepare_value(key="file", config_type="file", replacement="/private/1")


def test_actual_utf8_limit_and_explicit_null():
    assert prepare_value(
        key="ordinary", config_type="text", replacement="x" * 8192
    ).write_value
    for value in ("x" * 8193, "汉" * 2731, None, {"secret": "not a string"}, "\ud800"):
        with pytest.raises(BusinessException):
            prepare_value(key="ordinary", config_type="text", replacement=value)


def test_publication_matches_exact_current_content_without_storing_a_second_value():
    row = content()
    digest = publication_digest(row)
    fact = PublicationFact(
        tenant_id=7,
        config_id=301,
        schema_revision=PUBLIC_TEXT_SCHEMA,
        content_digest=digest,
    )
    assert publication_matches(row, fact)
    assert len(digest) == 64 and "Hello" not in repr(fact)
    for field, value in (
        ("tenant_id", 8),
        ("config_id", 302),
        ("config_key", "renamed"),
        ("config_value", "changed"),
        ("status", "2"),
        ("is_public", False),
        ("value_sensitivity", "secret"),
        ("config_type", "richtext"),
    ):
        assert not publication_matches(row.model_copy(update={field: value}), fact)
    assert not publication_matches(row, None)
    assert not publication_matches(
        row, fact.model_copy(update={"schema_revision": "unknown/1"})
    )
    assert not publication_matches(
        row, fact.model_copy(update={"content_digest": "0" * 64})
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"value_sensitivity": "secret"},
        {"value_sensitivity": "unknown"},
        {"config_key": "myPassword"},
        {"config_type": "richtext"},
        {"config_type": "file"},
        {"config_value": '{"secret":"x"}'},
        {"status": "2"},
    ],
)
def test_unsafe_content_cannot_get_a_publishable_fingerprint(changes):
    with pytest.raises(BusinessException):
        publication_digest(content().model_copy(update=changes))


def test_publication_and_value_schemas_reject_internal_or_unknown_input():
    for changes in (
        {"published_by_user_id": 1},
        {"schema_revision": "client-owned"},
        {"extra": "x"},
    ):
        with pytest.raises(ValidationError):
            content(**changes)


@pytest.mark.parametrize("config_type", [[], {}, 1, None])
def test_invalid_type_shapes_never_escape_as_python_errors(config_type):
    with pytest.raises(BusinessException):
        prepare_value(key="ordinary", config_type=config_type, replacement="value")
    assert not project_value(
        key="ordinary", value="value", config_type=config_type, sensitivity="private"
    ).visible


@pytest.mark.parametrize("key", ["\ud800", "a\x00b"])
def test_invalid_key_encoding_is_not_a_private_value(key):
    with pytest.raises(BusinessException):
        prepare_value(key=key, config_type="text", replacement="value")
