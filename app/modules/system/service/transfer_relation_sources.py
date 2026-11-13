"""Resolve relation copies from the actual protected source cells."""

from dataclasses import replace

from sqlalchemy import and_, select

from app.core.field_policy import FieldRef, FieldUse, query_counterexample
from app.core.field_policy.contracts import denied, limit, primitive
from app.modules.system.field_policy.transfer import transfer_denied
from app.modules.system.field_policy.transfer_catalog import IMPORT_MATCH
from app.modules.system.models.dept import Dept
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.role import Role
from app.modules.system.service.relation_read_context import load_relation_read_context


def strict_dept_ids(dept):
    """A corrupt/missing ancestor never becomes a shorter, misleading path."""
    try:
        pieces = (dept.ancestors or "0").split(",")
        if len(pieces) > 65 or any(not p.isdecimal() for p in pieces):
            return None
        values = tuple(int(p) for p in pieces if p != "0") + (dept.dept_id,)
        if len(values) != len(set(values)) or any(not 0 < v < 2**63 for v in values):
            return None
        return values
    except (TypeError, AttributeError, ValueError):
        return None


def _verified_path(path, records):
    if path is None or any(key not in records for key in path):
        return False
    return all(
        records[key][1].get("parent_id") == (str(path[index - 1]) if index else None)
        for index, key in enumerate(path)
    )


def restrict_relations(projection, allowed):
    """Remove all-hidden relation columns; hide only denied cells in mixed rows."""
    limit(len(allowed) == len(projection.rows))
    indices = tuple(
        index
        for index, name in enumerate(projection.columns)
        if name not in {"roles", "departments"} or any(name in row for row in allowed)
    )
    return replace(
        projection,
        headers=tuple(projection.headers[i] for i in indices),
        columns=tuple(projection.columns[i] for i in indices),
        rows=tuple(
            tuple(
                values[i]
                if projection.columns[i] not in {"roles", "departments"}
                or projection.columns[i] in access
                else "未授权"
                for i in indices
            )
            for values, access in zip(projection.rows, allowed, strict=True)
        ),
        levels=tuple(
            tuple(
                levels[i]
                if projection.columns[i] not in {"roles", "departments"}
                or projection.columns[i] in access
                else "hidden"
                for i in indices
            )
            for levels, access in zip(projection.levels, allowed, strict=True)
        ),
    )


async def context(db, tx, *, tenant, version, action):
    if (
        await db.scalar(
            select(FieldPolicyHead.tenant_id).where(
                FieldPolicyHead.tenant_id == tenant.tenant_id
            )
        )
        is None
    ):
        return None
    result = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=version,
        action=action,
        objects={"system.dept", "system.role"},
    )
    result.source_version = "transfer-relations/v1"
    return result


async def require_import_sources(db, tx, *, tenant, version, records):
    source = await context(
        db, tx, tenant=tenant, version=version, action="transfer.user-import"
    )
    if source is None:
        return
    specs = [("system.dept", Dept, ("dept_name",))]
    if any(record.role_input for record in records):
        specs.append(("system.role", Role, ("role_code", "role_name")))
    for obj, model, names in specs:
        table = model.__table__
        scopes = source.scope_sql(obj, table)
        base = and_(table.c.tenant_id == tenant.tenant_id, table.c.status == "1")
        counter = query_counterexample(
            source.catalog,
            source.rules,
            source.record(obj, "match", scopes),
            source.action,
            tuple(FieldUse(FieldRef(obj, name), IMPORT_MATCH) for name in names),
            base,
            scopes,
        )
        if await db.scalar(
            select(
                select(next(iter(table.primary_key.columns)))
                .select_from(table)
                .where(counter)
                .exists()
            )
        ):
            raise denied()


async def export_sources(db, tx, *, tenant, version, rows, lookup, projection):
    source = await context(
        db, tx, tenant=tenant, version=version, action="transfer.user-export"
    )
    if source is None:
        return projection
    dept_ids = set(lookup)
    role_ids = {
        role.role_id for row in rows for role in row.roles if role.status == "1"
    }
    limit(len(dept_ids) <= 5000 and len(role_ids) <= 1024)
    departments = (
        await source.fetch(
            db,
            Dept,
            "dept_id",
            predicates=(Dept.dept_id.in_(dept_ids),),
            maximum=5001,
            names=("dept_name", "parent_id"),
        )
        if dept_ids
        else ()
    )
    roles = (
        await source.fetch(
            db,
            Role,
            "role_id",
            predicates=(Role.role_id.in_(role_ids),),
            maximum=1025,
            names=("role_code", "status"),
        )
        if role_ids
        else ()
    )
    names = {
        int(record.record_key): (record, values)
        for record, values, _ in departments
        if isinstance(values.get("dept_name"), str) and "parent_id" in values
    }
    codes = {
        int(record.record_key): (record, values)
        for record, values, _ in roles
        if isinstance(values.get("role_code"), str) and "status" in values
    }
    allowed, seen = [], set()
    for index, row in enumerate(rows):
        access = set()
        path = strict_dept_ids(row.depts[0]) if row.depts else ()
        if (
            "departments" in projection.columns
            and projection.levels[index][projection.columns.index("departments")]
            != "hidden"
            and _verified_path(path, names)
        ):
            access.add("departments")
            for key in path:
                if ("dept", key) not in seen:
                    source.source(names[key][0], ("dept_name", "parent_id"))
                    seen.add(("dept", key))
        ids = {role.role_id for role in row.roles if role.status == "1"}
        if (
            "roles" in projection.columns
            and projection.levels[index][projection.columns.index("roles")] != "hidden"
            and ids <= codes.keys()
        ):
            access.add("roles")
            for key in sorted(ids):
                if ("role", key) not in seen:
                    source.source(codes[key][0], ("role_code", "status"))
                    seen.add(("role", key))
        allowed.append(access)
    return replace(
        restrict_relations(projection, tuple(allowed)),
        related_sources=tuple(primitive(ref) for ref in source.source_refs),
    )


async def recheck_sources(db, tx, *, tenant, version, references):
    if not references:
        return
    source = await context(
        db, tx, tenant=tenant, version=version, action="transfer.export-download"
    )
    if source is None:
        raise transfer_denied()
    for obj, model, anchor, names, maximum in (
        ("system.dept", Dept, "dept_id", ("dept_name", "parent_id"), 5000),
        ("system.role", Role, "role_id", ("role_code", "status"), 1024),
    ):
        keys = {
            int(ref["record_key"])
            for ref in references
            if ref["ref"]["object_id"] == obj
        }
        limit(len(keys) <= maximum)
        if not keys:
            continue
        rows = await source.fetch(
            db,
            model,
            anchor,
            predicates=(model.__table__.c[anchor].in_(keys),),
            maximum=maximum + 1,
            names=names,
        )
        found = {int(record.record_key): values for record, values, _ in rows}
        if any(
            int(ref["record_key"]) not in found
            or ref["ref"]["field_id"] not in found[int(ref["record_key"])]
            for ref in references
            if ref["ref"]["object_id"] == obj
        ):
            raise transfer_denied()


async def template_sources(db, tx, *, tenant, version, departments, roles):
    source = await context(
        db, tx, tenant=tenant, version=version, action="transfer.template"
    )
    if source is None:
        return departments, roles
    lookup = {dept.dept_id: dept for dept in departments}
    projected = (
        await source.fetch(
            db,
            Dept,
            "dept_id",
            predicates=(Dept.dept_id.in_(lookup),),
            maximum=5001,
            names=("dept_name", "status", "parent_id"),
        )
        if lookup
        else ()
    )
    safe = {
        int(record.record_key): (record, values)
        for record, values, _ in projected
        if all(key in values for key in ("dept_name", "status", "parent_id"))
    }
    # A template dictionary is a complete selector, so partial names cannot be
    # returned as though they were the complete current assignment universe.
    complete = len(safe) == len(departments) and all(
        _verified_path(strict_dept_ids(dept), safe) for dept in departments
    )
    if complete:
        for record, _values in safe.values():
            source.source(record, ("dept_name", "status", "parent_id"))
    role_ids = {role.role_id for role in roles}
    projected_roles = (
        await source.fetch(
            db,
            Role,
            "role_id",
            predicates=(Role.role_id.in_(role_ids),),
            maximum=1025,
            names=("role_code", "role_name", "status"),
        )
        if role_ids
        else ()
    )
    safe_roles = {
        int(record.record_key): record
        for record, values, _ in projected_roles
        if all(key in values for key in ("role_code", "role_name", "status"))
    }
    role_complete = len(safe_roles) == len(roles)
    if role_complete:
        for record in safe_roles.values():
            source.source(record, ("role_code", "role_name", "status"))
    return (departments if complete else []), (roles if role_complete else [])
