"""Closed transfer declarations; historical catalogs remain immutable."""

from collections import defaultdict
from dataclasses import replace
from functools import lru_cache

from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
    compile_rules,
)
from app.core.field_policy.contracts import require
from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.generic_compat import legacy_accesses
from app.modules.system.field_policy.user_fields import USER_FIELDS

READ = Access("read", "management.read")
CREATE = Access("create", "management.create")
EQ = Access("eq", "management.query", "filter")
CONTAINS = Access("contains", "management.query", "filter")
RANGE = Access("range", "management.query", "filter")
IMPORT_MATCH = Access("exact", "import", "match")
TRANSFER_CATALOG_REVISION = "system-fields/5-transfers"


def _field(name, kind="string", *, query=None, create=False, sources=()):
    return FieldSpec(
        name,
        kind,
        (READ, *((CREATE,) if create else ()), *((query,) if query else ())),
        nullable=True,
        sources=sources,
    )


def _object(name, anchor, fields):
    return ObjectSpec(
        name,
        "tenant",
        "file.owner/v1" if name == "system.file" else "transfer.owner/v1",
        (
            FieldSpec(
                anchor,
                "opaque_id" if anchor in {"batch_id", "export_id"} else "id",
                (READ,),
                structural=True,
            ),
            *fields,
        ),
    )


@lru_cache(maxsize=1)
def transfer_catalog():
    """Candidate only; runtime promotion requires explicit catalog migration."""
    base = control_catalog()
    source_fields = {
        "system.dept": {"dept_name"},
        "system.role": {"role_name", "role_code"},
    }
    existing = {
        name: replace(
            obj,
            fields=tuple(
                replace(field, accesses=field.accesses + (IMPORT_MATCH,))
                if field.field_id in source_fields.get(name, set())
                else field
                for field in obj.fields
            ),
        )
        if name in source_fields
        else obj
        for name, obj in base.objects.items()
    }
    file = _object(
        "system.file",
        "file_id",
        (
            _field("original_name", query=CONTAINS, create=True),
            _field("file_size", "integer"),
            _field("file_ext", query=EQ),
            _field("mime_type"),
            _field("business_type", query=EQ, create=True),
            _field("create_time", "datetime"),
            _field("owner_name", sources=(FieldRef("system.user", "user_name"),)),
            _field("content", "boolean", create=True),
        ),
    )
    imported = _object(
        "transfer.import",
        "batch_id",
        (
            _field("filename"),
            _field("status", query=EQ),
            *(
                _field(name, "integer")
                for name in (
                    "total_rows",
                    "summary_new",
                    "summary_exists",
                    "summary_conflict",
                    "summary_out_of_scope",
                    "success_count",
                    "skipped_count",
                    "overwritten_count",
                    "failed_count",
                )
            ),
            *(
                _field(name, "datetime", query=RANGE if name == "created_at" else None)
                for name in ("created_at", "started_at", "finished_at")
            ),
            _field("operator_name", sources=(FieldRef("system.user", "user_name"),)),
        ),
    )
    exported = _object(
        "transfer.export",
        "export_id",
        (
            _field("status", query=EQ),
            _field("row_count", "integer"),
            _field("file_size_bytes", "integer"),
            _field("created_at", "datetime", query=RANGE),
            _field("finished_at", "datetime"),
            _field("duration_ms", "integer"),
            _field("operator_name", sources=(FieldRef("system.user", "user_name"),)),
        ),
    )
    audit = _object(
        "transfer.audit",
        "log_id",
        (
            _field("event", query=EQ),
            _field("from_status"),
            _field("to_status"),
            _field("created_at", "datetime", query=RANGE),
            _field("operator_name", sources=(FieldRef("system.user", "user_name"),)),
            _field("counts", "json"),
        ),
    )
    added = (file, imported, exported, audit)
    actions = list(base.actions.values())
    for obj, action, permission in (
        (file, "file.list", "system:file:list"),
        (file, "file.detail", "system:file:list"),
        (file, "file.download", "system:file:list"),
        (file, "file.upload", "system:file:upload"),
        (file, "file.chat-read", "ai:chat:use"),
        (file, "file.parse", "ai:file:parse"),
        (imported, "transfer.import-list", "system:user:list"),
        (exported, "transfer.export-list", "system:user:list"),
        (audit, "transfer.audit-list", "system:user:list"),
    ):
        accesses = {READ, EQ, CONTAINS, RANGE}
        if action == "file.upload":
            accesses = {READ, CREATE}
        actions.append(
            ActionSpec(
                action,
                (obj.object_id, "system.user"),
                tuple(
                    FieldUse(FieldRef(obj.object_id, f.field_id), access)
                    for f in obj.fields
                    for access in f.accesses
                    if access in accesses
                )
                + (FieldUse(FieldRef("system.user", "user_name"), READ),),
                (permission,),
            )
        )
    for action, permission, purposes in (
        (
            "transfer.user-export",
            "system:user:export",
            {"export", "management.read", "management.query"},
        ),
        ("transfer.user-import", "system:user:import", {"import", "management.read"}),
        ("transfer.template", "system:user:import", {"import", "management.read"}),
        (
            "transfer.error-download",
            "system:user:import",
            {"import", "management.read"},
        ),
        (
            "transfer.export-download",
            "system:user:export",
            {"export", "management.read"},
        ),
    ):
        objects = ("system.user", "system.dept", "system.role")
        actions.append(
            ActionSpec(
                action,
                objects,
                tuple(
                    FieldUse(FieldRef(obj, f.field_id), access)
                    for obj in objects
                    for f in existing[obj].fields
                    for access in f.accesses
                    if access.purpose in purposes
                ),
                (permission,),
            )
        )
    return Catalog(tuple(existing.values()) + added, tuple(actions))


def transfer_user_grants(tenant_id, grants):
    """Conservative bridge to the frozen user evaluator, without merging uses.

    Legacy eq covers both filter and join. It can only receive an allow when
    every corresponding generic use allows it; a deny on either use is retained.
    The caller obtains grants from the verified snapshot under its G/B barrier.
    """
    catalog = transfer_catalog()
    compile_rules(catalog, grants)
    require(all(g.tenant_id == tenant_id for g in grants))
    groups = defaultdict(dict)
    for grant in grants:
        if (
            grant.ref.object_id != "system.user"
            or grant.ref.field_id not in USER_FIELDS
        ):
            continue
        access = grant.access
        expected = legacy_accesses(access.operation, access.purpose)
        if access not in expected:
            continue
        groups[grant.role_id, grant.ref.field_id, access.operation, access.purpose][
            access
        ] = grant
    result = []
    for (rid, field, operation, purpose), variants in sorted(groups.items()):
        expected = legacy_accesses(operation, purpose)
        denied = any(g.effect == "deny" for g in variants.values())
        if not denied and set(variants) != set(expected):
            continue
        masked = any(g.level == "masked" for g in variants.values())
        result.append(
            FieldGrant(
                rid,
                field,
                operation,
                purpose,
                "deny" if denied else "allow",
                "masked" if masked else "plain",
                USER_FIELDS[field].mask_id if masked else None,
            )
        )
    return tuple(result)
