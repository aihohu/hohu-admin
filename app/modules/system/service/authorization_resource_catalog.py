"""Fixed cross-domain tables for the opt-in lock executor, never business reads.

This is the explicitly scoped infrastructure exception to cross-module model
access. Pure plans never import this module; model loading is lazy and exact.
"""

from dataclasses import dataclass
from importlib import import_module

from sqlalchemy import Table

from app.core.authorization_plan import (
    ResourceKey,
    ResourceKind,
    _key_values,
    _resource_definition,
    _resource_order,
)
from app.db.base import Base


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    table: Table
    key_columns: tuple[str, ...]
    tenant_column: str | None
    order: int


def resource_spec(kind: ResourceKind) -> ResourceSpec:
    definition = _resource_definition(kind)
    table = Base.metadata.tables.get(definition.table_name)
    if table is None:
        import_module(definition.module)
        table = Base.metadata.tables[definition.table_name]
    if not all(name in table.c for name in definition.key_columns) or (
        definition.tenant_column is not None and definition.tenant_column not in table.c
    ):
        raise RuntimeError(
            "Authorization resource catalog does not match model metadata"
        )
    return ResourceSpec(
        table, definition.key_columns, definition.tenant_column, definition.order
    )


def key_values(key: ResourceKey) -> tuple[int | str, ...]:
    """Return identity values aligned to key_columns, excluding tenant scope."""
    return _key_values(key)


def resource_order(key: ResourceKey) -> tuple[int, int, tuple[int | str, ...]]:
    return _resource_order(key)
