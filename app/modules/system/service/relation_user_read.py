"""User management/own-account projections with independently scoped sources."""

from pydantic.alias_generators import to_camel
from sqlalchemy import and_, func, or_, select

from app.core.config import settings
from app.core.exceptions import NotFoundException
from app.core.field_policy import Access, resolve_fields
from app.core.field_policy.contracts import denied, limit
from app.db.base import user_depts, user_roles
from app.modules.auth.permission_collect import collect_user_identity
from app.modules.system.field_policy.dto import (
    USER_OUTPUT_SOURCES,
    project_user_dto,
    resolve_own_account_fields,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.relation_catalog import (
    CONTAINS,
    EQ,
    EXACT,
    JOIN,
    READ,
)
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.user_projection import ProjectedUserOut
from app.modules.system.service.mail_service import mail_service, ready
from app.modules.system.service.relation_read_context import load_relation_read_context
from app.modules.system.service.settings_service import settings_service

OWN = Access("read", "own-account")


async def relation_sources(db, context, entries, *, own=False):
    """One relationship batch then bounded source batches, never per-cell SQL."""
    ids = [int(record.record_key) for record, _, _ in entries]
    roots = {int(record.record_key): record for record, _, _ in entries}
    role_links, dept_links = {}, {}
    for uid, rid in await db.execute(
        select(user_roles.c.user_id, user_roles.c.role_id)
        .where(
            user_roles.c.tenant_id == context.tenant.tenant_id,
            user_roles.c.user_id.in_(ids),
        )
        .order_by(user_roles.c.user_id, user_roles.c.role_id)
    ):
        role_links.setdefault(uid, []).append(rid)
    if not own:
        for uid, did, primary in await db.execute(
            select(user_depts.c.user_id, user_depts.c.dept_id, user_depts.c.is_primary)
            .where(
                user_depts.c.tenant_id == context.tenant.tenant_id,
                user_depts.c.user_id.in_(ids),
            )
            .order_by(user_depts.c.user_id, user_depts.c.dept_id)
        ):
            dept_links.setdefault(uid, []).append((did, primary == "Y"))
    limit(
        sum(map(len, role_links.values())) + sum(map(len, dept_links.values())) <= 16384
    )
    sources = {}
    for model, anchor, keys, names in (
        (
            Role,
            "role_id",
            {rid for ids in role_links.values() for rid in ids},
            {"role_code", "role_name", "i18n_keys"},
        ),
        (
            Dept,
            "dept_id",
            {did for links in dept_links.values() for did, _ in links},
            {"dept_name"},
        ),
    ):
        ordered = sorted(keys)
        for start in range(0, len(ordered), 256):
            batch = await context.fetch(
                db,
                model,
                anchor,
                names=names,
                predicates=(
                    model.__table__.c[anchor].in_(ordered[start : start + 256]),
                ),
                maximum=256,
            )
            sources.update(
                {
                    (model.__name__, int(record.record_key)): (record, values)
                    for record, values, _ in batch
                }
            )
    for _, output, _ in entries:
        uid = int(output["user_id"])
        record = roots[uid]
        root_access = OWN if own else READ
        decisions = context.decisions(record, root_access)
        if decisions["roles"].allowed or own:
            context.source(record, ("roles",), root_access)
            codes, names, keys = [], [], []
            for rid in role_links.get(uid, ()):
                source = sources.get(("Role", rid))
                values = source[1] if source else {}
                codes.append(values.get("role_code"))
                names.append(values.get("role_name"))
                keys.append(
                    (values.get("i18n_keys") or {}).get("roleName")
                    if "role_name" in values
                    else None
                )
                if source:
                    context.source(
                        source[0],
                        tuple(
                            n
                            for n in ("role_code", "role_name", "i18n_keys")
                            if n in values
                        ),
                    )
            if own:
                output["roles"] = names
            else:
                output.update(roles=codes, role_names=names, role_name_keys=keys)
        if not own and decisions["departments"].allowed:
            context.source(record, ("departments",))
            links = dept_links.get(uid, ())
            output.update(
                dept_ids=[str(did) for did, _ in links],
                primary_dept=next(
                    (str(did) for did, primary in links if primary), None
                ),
                user_depts=[
                    {"dept_id": str(did), "is_primary": primary}
                    for did, primary in links
                ],
            )
            labels = []
            complete = True
            for did, _ in links:
                source = sources.get(("Dept", did))
                if not source or "dept_name" not in source[1]:
                    complete = False
                else:
                    labels.append(source[1]["dept_name"])
                    context.source(source[0], ("dept_name",))
            if complete:
                output["dept_names"] = ", ".join(labels)


def field_access(context, record):
    result = {}
    reads = context.decisions(record)
    for name in (
        "user_id",
        "user_name",
        "nickname",
        "user_phone",
        "user_email",
        "user_gender",
        "status",
        "create_time",
        "roles",
        "departments",
    ):
        decision = reads[name]
        caps = {
            "read": decision.level,
            "maskId": decision.mask_id,
            "create": False,
            "update": False,
            "replace": False,
            "queryOps": [],
        }
        for op, action, access in (
            ("create", "user.create", Access("create", "management.create")),
            ("update", "user.update", Access("update", "management.update")),
            (
                "replace",
                "user.roles" if name == "roles" else "user.departments",
                Access("replace", "management.relations"),
            ),
        ):
            if name not in {"roles", "departments"} and op == "replace":
                continue
            if (
                set(context.catalog.actions[action].permission_codes)
                <= context.permissions
            ):
                caps[op] = resolve_fields(
                    context.catalog, context.rules, record, action, access
                )[name].allowed
        result[name] = caps
    return result


async def query_coverage(db, context, field, access):
    table = User.__table__
    return not await db.scalar(
        select(
            select(1)
            .select_from(table)
            .where(
                context.base("system.user", table),
                ~context.coverage("system.user", table, field, access),
            )
            .exists()
        )
    )


async def role_query_coverage(db, context):
    table, source = User.__table__, Role.__table__
    links = user_roles.join(
        source,
        and_(
            source.c.tenant_id == user_roles.c.tenant_id,
            source.c.role_id == user_roles.c.role_id,
        ),
    )
    invalid = (
        select(1)
        .select_from(links)
        .where(
            user_roles.c.tenant_id == context.tenant.tenant_id,
            user_roles.c.user_id == table.c.user_id,
            ~and_(
                context.base("system.role", source),
                context.coverage("system.role", source, "role_code", EXACT),
            ),
        )
        .correlate(table)
        .exists()
    )
    return not await db.scalar(
        select(
            select(1)
            .select_from(table)
            .where(
                context.base("system.user", table),
                or_(~context.coverage("system.user", table, "roles", JOIN), invalid),
            )
            .exists()
        )
    )


async def read_users(db, tx, *, tenant, actor_user_version, operation, query=None):
    own = operation not in {"list", "members"}
    action = (
        "user.own-read"
        if own
        else "user.members-read"
        if operation == "members"
        else "user.list-read"
    )
    context = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        action=action,
        objects={"system.user", "system.role"} | (set() if own else {"system.dept"}),
    )
    if own:
        return await read_own(db, context, operation)
    table = User.__table__
    dependencies, predicates = [], []
    member_map = {}
    if operation == "list":
        for name, value in query.model_dump(exclude_none=True).items():
            if name in {"current", "size"}:
                continue
            if name == "role_code":
                if not await role_query_coverage(db, context):
                    raise denied()
                predicates.append(
                    User.roles.any(
                        and_(
                            Role.tenant_id == tenant.tenant_id, Role.role_code == value
                        )
                    )
                )
            else:
                access = EQ if name in {"user_gender", "status"} else CONTAINS
                dependencies.append((name, access))
                predicates.append(
                    table.c[name] == value
                    if access == EQ
                    else table.c[name].contains(value, autoescape=True)
                )
        # Existing fixed create_time ordering has a declared read dependency.
        if not await query_coverage(db, context, "create_time", READ):
            raise denied()
        order = (User.create_time.desc(), User.user_id.desc())
    else:
        if not await db.scalar(
            select(Dept.dept_id).where(
                context.base("system.dept", Dept.__table__),
                Dept.dept_id == query.dept_id,
            )
        ):
            raise NotFoundException("部门")
        dependencies = [("departments", JOIN), ("status", EQ)]
        member_map = {
            uid: primary == "Y"
            for uid, primary in await db.execute(
                select(user_depts.c.user_id, user_depts.c.is_primary).where(
                    user_depts.c.tenant_id == tenant.tenant_id,
                    user_depts.c.dept_id == query.dept_id,
                )
            )
        }
        member_ids = sorted(member_map)
        for start in range(0, len(member_ids), 256):
            batch = member_ids[start : start + 256]
            visible = set(
                (
                    await db.execute(
                        select(User.user_id).where(
                            context.base("system.user", table), User.user_id.in_(batch)
                        )
                    )
                ).scalars()
            )
            if visible != set(batch):
                raise denied()
        predicates.append(or_(User.status == "1", User.user_id.in_(member_ids)))
        if (query.query or "").strip():
            dependencies += [("user_name", CONTAINS), ("nickname", CONTAINS)]
            predicates.append(
                or_(
                    User.user_name.icontains(query.query.strip(), autoescape=True),
                    User.nickname.icontains(query.query.strip(), autoescape=True),
                )
            )
        order = (User.user_id,)
    await context.prove(db, "system.user", table, tuple(dependencies))
    total = await db.scalar(
        select(func.count())
        .select_from(table)
        .where(context.base("system.user", table), *predicates)
    )
    names = (
        {"user_id", "user_name", "nickname", "status"}
        if operation == "members"
        else {s.field_id for s in USER_OUTPUT_SOURCES["list"].values()}
    )
    entries = await context.fetch(
        db,
        User,
        "user_id",
        names=names,
        predicates=tuple(predicates),
        order=order,
        offset=(query.current - 1) * query.size,
        maximum=query.size,
    )
    if operation == "list":
        await relation_sources(db, context, entries)
    else:
        for record, values, _ in entries:
            if context.decisions(record)["departments"].allowed:
                uid = int(record.record_key)
                values.update(
                    is_member=uid in member_map, is_primary=member_map.get(uid, False)
                )
                context.source(record, ("departments",))
    query_caps = {}
    for name in (
        "user_name",
        "nickname",
        "user_phone",
        "user_email",
        "user_gender",
        "status",
    ):
        access = EQ if name in {"user_gender", "status"} else CONTAINS
        query_caps[name] = (
            [access.operation]
            if await query_coverage(db, context, name, access)
            else []
        )
    query_caps["roles"] = ["eq"] if await role_query_coverage(db, context) else []
    for record, values, _ in entries:
        if values.get("create_time") is not None:
            values["create_time"] = values["create_time"].strftime(
                settings.DATETIME_FORMAT
            )
        values["field_access"] = field_access(context, record)
        for name, caps in values["field_access"].items():
            caps["queryOps"] = query_caps.get(name, [])
        values["field_access"] = {
            to_camel(name): cap for name, cap in values["field_access"].items()
        }
    return {
        "records": [
            ProjectedUserOut.model_validate(values).model_dump(
                mode="json", by_alias=True
            )
            for _, values, _ in entries
        ],
        "total": total,
        "current": query.current,
        "size": query.size,
    }


async def read_own(db, context, operation):
    # Exact own-account conversion cannot collapse distinct generic query uses.
    grants = tuple(
        FieldGrant(
            g.role_id,
            g.ref.field_id,
            g.access.operation,
            "own-account",
            g.effect,
            g.level,
            g.mask_id,
        )
        for g in context.rules.index.values()
        if g.ref.object_id == "system.user" and g.access.purpose == "own-account"
    )
    roles = frozenset(context.scopes)
    args = {
        "actor": (context.tenant.tenant_id, context.actor.user_id),
        "target": (context.tenant.tenant_id, context.actor.user_id),
        "grants": grants,
        "applicable_role_ids": roles,
    }
    decisions = resolve_own_account_fields(**args, operation="read")
    writes = resolve_own_account_fields(**args, operation="update")
    view = {
        "profile": "profile",
        "capabilities": "profile",
        "auth": "avatar",
        "email": "email",
    }[operation]
    values = {
        s.field_id: getattr(context.actor, s.field_id)
        for s in USER_OUTPUT_SOURCES[view].values()
        if s.field_id not in {"roles", "email_verified"}
    }
    if operation == "capabilities":
        values = {}
    if values.get("create_time") is not None:
        values["create_time"] = values["create_time"].strftime(settings.DATETIME_FORMAT)
    if operation == "profile":
        record = context.record("system.user", context.actor.user_id, roles)
        # Fixed own-account readability remains; source Role labels still need
        # management.read on the real source records.
        entries = [(record, {"user_id": str(context.actor.user_id)}, {})]
        if decisions.fields["roles"].allowed:
            await relation_sources(db, context, entries, own=True)
            values["roles"] = entries[0][1].get("roles", [])
    if operation == "email":
        values["email_verified"] = context.actor.email_verified_at is not None
    result = project_user_dto(
        view=view, values=values, decisions=decisions, capability_decisions=(writes,)
    )
    if operation == "auth":
        result = {**collect_user_identity(context.actor), **result}
    if operation == "email":
        mail_settings = await settings_service.values(
            db, tenant_id=context.tenant.tenant_id
        )
        result.update(
            enabled=ready(await mail_service.config(db)),
            codeMinutes=mail_settings["email_code_minutes"],
        )
    return result
