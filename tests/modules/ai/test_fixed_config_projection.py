"""Closed control-plane projection candidates leave legacy runtime untouched."""

from copy import deepcopy

import pytest

from app.core.exceptions import BusinessException
from app.modules.ai.schemas.config_projection import redact_config, redact_url
from app.modules.ai.schemas.control_config import (
    project_control_config,
    validate_control_config,
)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("provider", {"options": {"value": "sentinel-secret"}}),
        ("provider", {"generation": {"max_tokens": 10}}),
        ("model", {"generation": {"max_tokens": 10, "innocent": "sentinel-secret"}}),
        ("model", {"headers": {"x": "sentinel-secret"}}),
        ("model", {"generation": {"max_tokens": True}}),
        ("model", {"generation": {"temperature": float("nan")}}),
        ("model", {"generation": {"temperature": 3}}),
        ("model", {"generation": {"max_tokens": 0}}),
    ],
)
def test_unknown_or_invalid_config_is_not_a_partially_redacted_object(kind, value):
    before = deepcopy(value)
    with pytest.raises(BusinessException) as error:
        validate_control_config(value, kind=kind)
    assert "sentinel-secret" not in str(error.value)
    assert project_control_config(value, kind=kind) == {}
    assert value.keys() == before.keys()
    if "headers" in value:
        assert value["headers"] == before["headers"]


def test_generation_is_exact_typed_and_detached():
    original = {"generation": {"max_tokens": 1_000_000, "temperature": 0}}
    clean = validate_control_config(original, kind="model")
    assert clean == original
    clean["generation"]["max_tokens"] = 1
    assert original["generation"]["max_tokens"] == 1_000_000
    assert project_control_config(None, kind="provider") is None
    assert validate_control_config({}, kind="provider") == {}
    assert validate_control_config({"generation": None}, kind="model") == {
        "generation": None
    }
    with pytest.raises(BusinessException):
        validate_control_config({}, kind="unknown")


def test_legacy_helpers_are_not_a_closed_contract_and_are_not_silently_rewired():
    raw = {"ordinary": {"innocent": "legacy-unknown"}}
    assert redact_config(raw) == raw
    assert project_control_config(raw, kind="provider") == {}


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://user:sentinel@EXAMPLE.com:443/api?token=sentinel#fragment",
            "https://example.com:443/api",
        ),
        ("https://example.com:bad/api", None),
        ("file:///private/secret", None),
        ("https://[::1]:8443/path?secret=x", "https://[::1]:8443/path"),
    ],
)
def test_fixed_url_projection_drops_credentials_and_raw_errors(url, expected):
    assert redact_url(url) == expected
