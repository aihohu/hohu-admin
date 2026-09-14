import math

import pytest

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.lowcode.identifiers import (
    quote_identifier,
    render_sql_literal,
    validate_manifest_identifier,
)
from app.modules.marketplace.lowcode.type_mapping import make_table_name


@pytest.mark.parametrize(
    "value",
    [
        "name;drop_table",
        'bad"quote',
        "white space",
        "混淆",
        "UpperCase",
        "tenant_id",
        "_private",
    ],
)
def test_manifest_identifiers_fail_closed(value: str):
    with pytest.raises(AppInvalidManifestException):
        validate_manifest_identifier(value, label="field", reject_system=True)


def test_valid_identifier_is_always_quoted():
    assert quote_identifier("customer_name", label="field") == '"customer_name"'
    assert quote_identifier("select", label="field") == '"select"'


def test_physical_table_name_budget_prevents_postgres_truncation():
    assert len(make_table_name("a" * 39)) == 48
    with pytest.raises(AppInvalidManifestException):
        make_table_name("a" * 40)
    with pytest.raises(AppInvalidManifestException):
        make_table_name("short-app", "m" * 40)


def test_slug_normalization_rejects_ambiguous_legacy_characters():
    with pytest.raises(AppInvalidManifestException):
        make_table_name("legacy_slug")
    with pytest.raises(AppInvalidManifestException):
        make_table_name("legacy.slug")


def test_sql_literal_renderer_escapes_strings_and_rejects_non_finite_numbers():
    assert (
        render_sql_literal("x'; DROP TABLE audit; --") == "E'x''; DROP TABLE audit; --'"
    )
    assert render_sql_literal(True) == "TRUE"
    assert render_sql_literal(12.5) == "12.5"
    with pytest.raises(AppInvalidManifestException):
        render_sql_literal(math.nan)
