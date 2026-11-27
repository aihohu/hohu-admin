"""User management/own-account projections with independently scoped sources."""

from pydantic.alias_generators import to_camel
from sqlalchemy import and_, func, or_, select

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    NotFoundException,
)
from app.core.field_policy import Access, resolve_fields
from app.core.field_policy.contracts import limit
from app.db.base import user_depts, user_roles
from app.modules.auth.permission_collect import collect_user_identity
from app.modules.system.field_policy.dto import (
    USER_OUTPUT_SOURCES,
    project_user_dto,
    resolve_own_account_fields,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.query_plan import query_denied
from app.modules.system.field_policy.relation_catalog import (
    CONTAINS,
    CREATE,
    EQ,
    EXACT,
    JOIN,
    READ,
    SORT,
    UPDATE,
)
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.schemas.user_projection import ProjectedUserOut
from app.modules.system.service.authorization_read_budget import read_phase
from app.modules.system.service.mail_service import mail_service, ready
from app.modules.system.service.relation_read_context import (
    load_list_context,
    load_relation_read_context,
)
from app.modules.system.service.settings_service import settings_service

OWN = Access("read", "own-account")
REPLACE = Access("replace", "management.relations")


@read_phase("source")
async def relation_sources(db, context, entries, *, own=False):
    """One relationship batch then bounded source batches, never per-cell SQL."""
    root_access = OWN if own else READ
    role_users, dept_users = set(), set()
    for record, _, _ in entries:
        uid = int(record.record_key)
        if own:
            role_users.add(uid)
            continue
        decisions = context.decisions(record, root_access)
        if decisions["roles"].allowed:
            role_users.add(uid)
        if decisions["departments"].allowed:
            dept_users.add(uid)
    role_links, dept_links = {}, {}
    if role_users:
        for uid, rid in await db.execute(
            select(user_roles.c.user_id, user_roles.c.role_id)
            .where(
                user_roles.c.tenant_id == context.tenant.tenant_id,
                user_roles.c.user_id.in_(sorted(role_users)),
            )
            .order_by(user_roles.c.user_id, user_roles.c.role_id)
        ):
            role_links.setdefault(uid, []).append(rid)
    if dept_users:
        for uid, did, primary in await db.execute(
            select(user_depts.c.user_id, user_depts.c.dept_id, user_depts.c.is_primary)
            .where(
                user_depts.c.tenant_id == context.tenant.tenant_id,
                user_depts.c.user_id.in_(sorted(dept_users)),
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
    for record, output, _ in entries:
        uid = int(record.record_key)
        if uid in role_users:
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
        if uid in dept_users:
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
        result[name] = {
            "read": decision.level,
            "maskId": decision.mask_id,
            "create": False,
            "update": False,
            "replace": False,
            "queryOps": [],
        }
    for op, action, access, names in (
        ("create", "user.create", CREATE, result),
        ("update", "user.update", UPDATE, result),
        ("replace", "user.roles", REPLACE, ("roles",)),
        ("replace", "user.departments", REPLACE, ("departments",)),
    ):
        if set(context.catalog.actions[action].permission_codes) <= context.permissions:
            decisions = resolve_fields(
                context.catalog, context.rules, record, action, access
            )
            for name in names:
                result[name][op] = decisions[name].allowed
    return result


def field_coverage(context, field, access, request_scope=()):
    table = User.__table__
    return ~(
        select(table.c.user_id)
        .select_from(table)
        .where(
            context.base("system.user", table),
            *request_scope,
            ~context.coverage("system.user", table, field, access),
        )
        .exists()
    )


def role_coverage(context, request_scope=()):
    table, source = User.__table__, Role.__table__
    links = user_roles.outerjoin(
        source,
        and_(
            source.c.tenant_id == user_roles.c.tenant_id,
            source.c.role_id == user_roles.c.role_id,
        ),
    )
    invalid = (
        select(source.c.role_id)
        .select_from(links)
        .where(
            user_roles.c.tenant_id == context.tenant.tenant_id,
            user_roles.c.user_id == table.c.user_id,
            or_(
                source.c.role_id.is_(None),
                ~and_(
                    context.base("system.role", source),
                    context.coverage("system.role", source, "role_code", EXACT),
                ),
            ),
        )
        .correlate(table)
        .exists()
    )
    return ~(
        select(table.c.user_id)
        .select_from(table)
        .where(
            context.base("system.user", table),
            *request_scope,
            or_(~context.coverage("system.user", table, "roles", JOIN), invalid),
        )
        .exists()
    )


FILTERS = {
    name: EQ if name in {"user_gender", "status"} else CONTAINS
    for name in (
        "user_name",
        "nickname",
        "user_phone",
        "user_email",
        "user_gender",
        "status",
    )
}


@read_phase("proof")
async def query_capabilities(db, context, request_scope):
    expressions = {
        name: field_coverage(context, name, access, request_scope)
        for name, access in FILTERS.items()
    }
    expressions["roles"] = role_coverage(context, request_scope)
    expressions["create_time"] = field_coverage(
        context, "create_time", READ, request_scope
    )
    return (
        (
            await db.execute(
                select(*(expr.label(name) for name, expr in expressions.items()))
            )
        )
        .mappings()
        .one()
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
    return await _read_page(db, context, operation, query)


async def read_list(db, tx, claims, query, expected_tenant_code):
    context = await load_list_context(
        db,
        tx,
        claims,
        expected_tenant_code,
        {"system.user", "system.role", "system.dept"},
    )
    return await _read_page(db, context, "list", query)


async def _read_page(db, context, operation, query):
    tenant = context.tenant
    table = User.__table__
    dependencies, predicates = [], []
    member_map = {}
    request_scope = []
    if operation == "list":
        if query.dept_id is not None:
            if not await db.scalar(
                select(field_coverage(context, "departments", JOIN))
            ):
                raise query_denied()
            request_scope.append(
                select(user_depts.c.user_id)
                .where(
                    user_depts.c.tenant_id == tenant.tenant_id,
                    user_depts.c.user_id == User.user_id,
                    user_depts.c.dept_id == query.dept_id,
                )
                .correlate(table)
                .exists()
            )
            predicates.extend(request_scope)
        capabilities = await query_capabilities(db, context, request_scope)
        for name, value in query.model_dump(exclude_none=True).items():
            if name in {"current", "size", "dept_id", "sort_by", "sort_order"}:
                continue
            if name == "role_code":
                if not capabilities["roles"]:
                    raise query_denied()
                predicates.append(
                    select(Role.role_id)
                    .select_from(
                        user_roles.join(
                            Role.__table__,
                            and_(
                                Role.tenant_id == user_roles.c.tenant_id,
                                Role.role_id == user_roles.c.role_id,
                            ),
                        )
                    )
                    .where(
                        user_roles.c.tenant_id == tenant.tenant_id,
                        user_roles.c.user_id == User.user_id,
                        Role.tenant_id == tenant.tenant_id,
                        Role.role_code == value,
                    )
                    .correlate(User.__table__)
                    .exists()
                )
            else:
                access = FILTERS[name]
                if not capabilities[name]:
                    raise query_denied()
                predicates.append(
                    table.c[name] == value
                    if access == EQ
                    else table.c[name].contains(value, autoescape=True)
                )
        if query.sort_by == "userPhone":
            dependencies.append(("user_phone", SORT))
            direction = (
                User.user_phone.desc
                if query.sort_order == "desc"
                else User.user_phone.asc
            )
            order = (direction().nulls_last(), User.user_id.asc())
        elif query.sort_order is not None:
            raise query_denied()
        elif capabilities["create_time"]:
            order = (User.create_time.desc(), User.user_id.desc())
        else:
            order = (User.user_id.desc(),)
    else:
        capabilities = await query_capabilities(db, context, request_scope)
        if not await db.scalar(
            select(Dept.dept_id).where(
                context.base("system.dept", Dept.__table__),
                Dept.dept_id == query.dept_id,
            )
        ):
            raise NotFoundException("部门")
        dependencies = [("departments", JOIN), ("status", EQ)]
        if (query.query or "").strip():
            dependencies += [("user_name", CONTAINS), ("nickname", CONTAINS)]
            predicates.append(
                or_(
                    User.user_name.icontains(query.query.strip(), autoescape=True),
                    User.nickname.icontains(query.query.strip(), autoescape=True),
                )
            )
        order = (User.user_id,)
    try:
        await context.prove(
            db, "system.user", table, tuple(dependencies), request_scope=request_scope
        )
    except BusinessException as exc:
        if exc.error_code == "FIELD_POLICY_DENIED":
            raise query_denied() from None
        raise
    if operation == "members":
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
                raise AuthorizationException(
                    "部门成员集合包含不可管理用户",
                    error_code="DEPT_MEMBERSHIP_GLOBAL_IMPACT_OUT_OF_SCOPE",
                )
        predicates.append(or_(User.status == "1", User.user_id.in_(member_ids)))
    total = await db.scalar(
        select(func.pg_catalog.count(1))
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
    query_caps = {
        name: [access.operation] if capabilities[name] else []
        for name, access in FILTERS.items()
    }
    query_caps["roles"] = ["eq"] if capabilities["roles"] else []
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
        **(
            {
                "authorization_epoch": str(
                    await db.scalar(
                        select(UserFieldPolicyState.epoch).where(
                            UserFieldPolicyState.tenant_id == tenant.tenant_id
                        )
                    )
                )
            }
            if operation == "members"
            else {}
        ),
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
