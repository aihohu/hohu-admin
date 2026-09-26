"""Persisted translation ownership; an empty mapping means a user override."""

from pydantic.alias_generators import to_camel


def initialize_translations(row, prefix: str, defaults: dict[str, str]) -> None:
    if row.i18n_keys is None:
        row.i18n_keys = {
            to_camel(field): f"{prefix}.{to_camel(field)}"
            for field, value in defaults.items()
            if getattr(row, field) == value
        }


def clear_changed_translations(row, changes: dict) -> None:
    keys = dict(row.i18n_keys or {})
    for field, value in changes.items():
        if to_camel(field) in keys and getattr(row, field) != value:
            keys.pop(to_camel(field))
    row.i18n_keys = keys
