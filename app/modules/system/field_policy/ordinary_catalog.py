"""Server-owned ordinary object declarations shared by preparation and runtime."""

from functools import lru_cache

from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
)
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.field_policy.relation_catalog import build_relation_catalog

READ = Access("read", "management.read")
CREATE = Access("create", "management.create")
UPDATE = Access("update", "management.update")
SORT = Access("sort", "management.query", "sort")
EQ = Access("eq", "management.query", "filter")
CONTAINS = Access("contains", "management.query", "filter")
EXACT = Access("exact", "management.query", "filter")
CATALOG_REVISION = "system-fields/3-relations"

SURFACES = {
    "dict-type": ("system.dict_type", "dict_type_id"),
    "dict-data": ("system.dict_data", "dict_code"),
    "data-scope-demo": ("system.data_scope_demo", "demo_id"),
}


def _object(surface, fields):
    object_id, anchor = SURFACES[surface]
    return ObjectSpec(
        object_id,
        "tenant",
        "ordinary.scope/v1",
        (
            FieldSpec(anchor, "id", (READ,), structural=True),
            *fields,
        ),
    )


def _field(name, kind="string", *, writable=True, nullable=False, queries=()):
    return FieldSpec(
        name,
        kind,
        (READ, *((CREATE, UPDATE) if writable else ()), *queries),
        nullable=nullable,
    )


@lru_cache(maxsize=1)
def ordinary_catalog():
    """Immutable four-object upgrade source; never append current objects here."""
    objects = (
        user_compat_object(),
        _object(
            "dict-type",
            (
                _field("dict_name", queries=(CONTAINS,)),
                _field("dict_type", queries=(CONTAINS, EXACT)),
                _field("status", queries=(EQ,)),
                _field("remark", nullable=True),
                _field("create_time", "datetime", writable=False),
            ),
        ),
        _object(
            "dict-data",
            (
                _field("dict_label", queries=(CONTAINS,)),
                _field("dict_value", queries=(CONTAINS,)),
                _field("dict_type", queries=(CONTAINS, EXACT)),
                _field("dict_sort", "integer", queries=(SORT,)),
                _field("css_class", nullable=True),
                _field("list_class", nullable=True),
                _field("is_default"),
                _field("status", queries=(EQ,)),
                _field("create_time", "datetime", writable=False),
            ),
        ),
        _object(
            "data-scope-demo",
            (
                _field("title", queries=(CONTAINS,)),
                _field("content", nullable=True),
                _field("status", queries=(EQ,)),
                _field("dept_id", "id", writable=False),
                _field("create_by", "id", writable=False),
                _field("create_time", "datetime", writable=False, nullable=True),
                _field("update_time", "datetime", writable=False, nullable=True),
            ),
        ),
    )
    actions = []
    for surface, (object_id, _) in SURFACES.items():
        obj = next(o for o in objects if o.object_id == object_id)
        for operation, permission in (
            ("list", "list"),
            ("options", "list"),
            ("create", "add"),
            ("update", "edit"),
            ("delete", "delete"),
            ("batch-delete", "batch-delete"),
        ):
            if surface == "data-scope-demo" and operation in {
                "options",
                "batch-delete",
            }:
                continue
            accesses = {READ}
            if operation in {"list", "options"}:
                accesses |= {EQ, CONTAINS, EXACT, SORT}
            if operation == "create":
                accesses.add(CREATE)
            if operation == "update":
                accesses.add(UPDATE)
            dependencies = ()
            object_ids = (object_id,)
            if surface == "dict-data" and operation in {"create", "update"}:
                object_ids += ("system.dict_type",)
                dependencies = (
                    FieldUse(FieldRef("system.dict_type", "dict_type"), EXACT),
                )
            elif surface == "dict-type" and operation == "update":
                object_ids += ("system.dict_data",)
                dependencies = (
                    FieldUse(FieldRef("system.dict_data", "dict_type"), UPDATE),
                )
            actions.append(
                ActionSpec(
                    f"{surface}.{operation}",
                    object_ids,
                    tuple(
                        FieldUse(FieldRef(object_id, f.field_id), a)
                        for f in obj.fields
                        for a in f.accesses
                        if a in accesses
                    )
                    + dependencies,
                    (f"system:{surface}:{permission}",),
                )
            )
    return Catalog(objects, tuple(actions))


@lru_cache(maxsize=1)
def relation_catalog():
    """Complete relation declarations, built from the fixed ordinary source."""
    return build_relation_catalog(ordinary_catalog())


@lru_cache(maxsize=1)
def production_catalog():
    """Current preparation catalog; activation still requires qualification."""
    return relation_catalog()
