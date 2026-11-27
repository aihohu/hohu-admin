"""Generic write authority from a live barrier, never from caller capabilities."""

from dataclasses import dataclass, field

from pydantic.alias_generators import to_camel
from sqlalchemy import and_, false, or_, select, true

from app.core.authorization_plan import ResourceKind as K
from app.core.field_policy import (
    Access,
    FieldRef,
    FieldUse,
    Grant,
    RecordContext,
    compile_rules,
    query_counterexample,
    resolve_fields,
    validate_write,
)
from app.core.field_policy.contracts import denied
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.field_policy.generic_compat import convert_legacy_grants
from app.modules.system.field_policy.list_admission import user_write_roles
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as relation_catalog,
)
from app.modules.system.field_policy.relation_catalog import CREATE, EXACT, READ, UPDATE
from app.modules.system.field_policy.user_list_policy import USES
from app.modules.system.field_policy.write_plan import user_write_footprint
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.generic_field_policy_service import (
    failure,
    generic_field_policy_service,
)
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_field_context import load_current_user
from app.utils.data_scope import (
    get_user_filters_from_resolution,
    resolve_data_scope_for_roles,
    resolve_list_role_scopes,
)


@dataclass(repr=False)
class RelationWriteContext:
    tenant: object
    actor: object
    rules: object
    scopes: dict
    permissions: frozenset
    facts: dict
    memberships: dict = field(init=False)

    def __post_init__(self):
        self.memberships = {}
        for row in self.facts[K.USER_DEPTS]:
            self.memberships.setdefault(row["user_id"], set()).add(row["dept_id"])

    def record(self, object_id, record_id, *, proposed_depts=None):
        if object_id in {"system.role", "system.menu"}:
            applicable = frozenset(self.scopes)
        else:
            depts = self.memberships.get(record_id, set())
            applicable = frozenset(
                rid
                for rid, scope in self.scopes.items()
                if scope.unbounded
                or (
                    bool(proposed_depts)
                    and set(proposed_depts) <= set(scope.accessible_dept_ids or ())
                    if proposed_depts is not None
                    else (
                        record_id in (scope.accessible_dept_ids or ())
                        if object_id == "system.dept"
                        else (scope.include_self and record_id == self.actor.user_id)
                        or bool(depts & set(scope.accessible_dept_ids or ()))
                    )
                )
            )
        return RecordContext(
            self.tenant.tenant_id,
            object_id,
            str(record_id),
            applicable,
            self.permissions,
        )

    def write(
        self, object_id, record_id, action, access, fields, *, proposed_depts=None
    ):
        fields = tuple(fields)
        if not fields:
            return
        record = self.record(object_id, record_id, proposed_depts=proposed_depts)
        if not record.role_ids:
            raise denied()
        validate_write(relation_catalog(), self.rules, record, action, access, fields)


async def load_relation_write_context(
    db, tx, *, tenant, actor_user_version, facts, objects, user_action=None
):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    head = await db.scalar(
        select(FieldPolicyHead.tenant_id).where(
            FieldPolicyHead.tenant_id == tenant.tenant_id
        )
    )
    if head is None and user_action is None:
        if objects == {"system.user"}:
            return None  # The existing user guard owns old active/frozen admission.
        state = (
            await db.execute(
                select(
                    UserFieldPolicyState.mode, UserFieldPolicyState.ever_enforced
                ).where(UserFieldPolicyState.tenant_id == tenant.tenant_id)
            )
        ).one_or_none()
        if state is None or state.mode not in {
            "legacy",
            "ready",
            "enforcing",
            "frozen",
        }:
            raise failure()
        # The old state governs users, not relation objects. Preserve their
        # existing function/delegation protocols until a generic head exists.
        # A leader lookup still has to authorize its user sources below.
        if "system.user" not in objects or not state.ever_enforced:
            return None
    actor = await load_current_user(
        db, tenant=tenant, actor_user_version=actor_user_version
    )
    roles = tuple(r for r in actor.roles if r.status == "1")
    if user_action:
        roles = user_write_roles(roles, tenant.tenant_id, user_action)
        if not roles:
            raise denied()
    catalog = relation_catalog()
    grants = ()
    if head is None:
        legacy = await active_policy_grants(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            role_ids=tuple(str(r.role_id) for r in roles),
        )
        if legacy is None and not user_action:
            return None
        grants = (
            convert_legacy_grants(tenant.tenant_id, legacy)
            if legacy is not None
            else ()
        ) + tuple(
            Grant(tenant.tenant_id, str(r.role_id), ref, a)
            for r in roles
            for ref, field in catalog.fields.items()
            if ref.object_id
            in (objects if legacy is None else objects - {"system.user"})
            for a in field.accesses
        )
    else:
        for obj in sorted(objects):
            grants += await generic_field_policy_service.load_active(
                db,
                tx,
                tenant_id=tenant.tenant_id,
                catalog=catalog,
                object_id=obj,
                role_ids=tuple(str(r.role_id) for r in roles),
                list_defaults=user_action is not None,
            )
    permissions = frozenset(collect_user_buttons(actor))
    if any(r.role_code == "R_SUPER" for r in roles):
        permissions |= frozenset(
            p for a in catalog.actions.values() for p in a.permission_codes
        )
    scopes = (
        await resolve_list_role_scopes(db, actor, roles, tenant)
        if user_action
        else {
            str(r.role_id): await resolve_data_scope_for_roles(
                db, user=actor, roles=[r], tenant=tenant
            )
            for r in roles
        }
    )
    return RelationWriteContext(
        tenant, actor, compile_rules(catalog, grants), scopes, permissions, facts
    )


async def user_write_form_fields(db, tx, *, tenant, actor_user_version, query):
    editing = query.user_id is not None
    purpose = "update" if editing else "create"
    action = "user." + purpose
    ctx = await load_relation_write_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        facts={K.USER_DEPTS: ()},
        objects={"system.user"},
        user_action=action,
    )
    if editing:
        scopes = {
            rid: and_(true(), *get_user_filters_from_resolution(scope))
            for rid, scope in ctx.scopes.items()
        }
        row = (
            (
                await db.execute(
                    select(*(scope.label(rid) for rid, scope in scopes.items())).where(
                        User.tenant_id == tenant.tenant_id,
                        User.user_id == int(query.user_id),
                        or_(false(), *scopes.values()),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise denied()
        record = RecordContext(
            tenant.tenant_id,
            "system.user",
            query.user_id,
            frozenset(rid for rid in scopes if row[rid]),
            ctx.permissions,
        )
    else:
        record = ctx.record(
            "system.user", "new", proposed_depts=set(map(int, query.dept_ids))
        )
    catalog = relation_catalog()
    decisions = resolve_fields(
        catalog, ctx.rules, record, action, UPDATE if editing else CREATE
    )
    fields = [to_camel(name) for name in USES[purpose] if decisions[name].allowed]
    if editing:
        return {"fields": fields, "canSubmit": bool(fields)}
    replace = Access("replace", "management.relations")
    relations = resolve_fields(catalog, ctx.rules, record, action, replace)
    # A scoped creator must be able to choose the first department before a
    # proposed target exists. Only expose the selector; submit rechecks the set.
    choose_dept = relations["departments"].allowed or (
        not query.dept_ids
        and any(
            scope.accessible_dept_ids
            and resolve_fields(
                catalog,
                ctx.rules,
                RecordContext(
                    tenant.tenant_id,
                    "system.user",
                    "new",
                    frozenset({rid}),
                    ctx.permissions,
                ),
                action,
                replace,
            )["departments"].allowed
            for rid, scope in ctx.scopes.items()
        )
    )
    return {
        "fields": fields,
        "canSubmit": {"userName", "status", "password"} <= set(fields)
        and relations["roles"].allowed
        and (not query.dept_ids or relations["departments"].allowed),
        "assignRoles": relations["roles"].allowed
        and "system:user:role-auth" in ctx.permissions,
        "assignDepartments": bool(choose_dept)
        and "system:dept:list" in ctx.permissions,
    }


async def validate_relation_system_fields(
    db, tx, *, tenant, actor_user_version, operation, payload, target_id, new_id, facts
):
    if operation in {"user.change_password", "user.revoke"}:
        return
    obj = (
        "system.user"
        if operation == "dept.members"
        else "system." + operation.split(".")[0]
    )
    objects = {obj}
    leader_written = operation in {"dept.create", "dept.update"} and (
        operation.endswith("create") or "leader" in payload.model_fields_set
    )
    if leader_written and payload.leader and payload.leader.strip():
        objects.add("system.user")
    context = await load_relation_write_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        facts=facts,
        objects=objects,
        user_action=operation if operation in {"user.create", "user.update"} else None,
    )
    if context is None:
        return
    if operation.endswith("delete"):
        return context
    if operation.startswith("user.") or operation == "dept.members":
        validate_generic_user_fields(context, operation, payload, target_id, new_id)
    elif operation in {"role.create", "role.update", "dept.create", "dept.update"}:
        access = CREATE if operation.endswith("create") else UPDATE
        values = payload.model_dump(exclude_unset=access == UPDATE)
        ordinary = {
            f.field_id
            for f in relation_catalog().objects[obj].fields
            if access in f.accesses
        }
        context.write(
            obj,
            new_id or target_id,
            operation,
            access,
            ordinary & values.keys(),
            proposed_depts={int(payload.parent_id)}
            if operation == "dept.create" and payload.parent_id
            else None,
        )
        if leader_written and payload.leader and payload.leader.strip():
            await prove_leader_lookup(db, context, operation)
    return context


async def validate_menu_fields(db, tx, *, tenant, actor_user_version, facts, changes):
    context = await load_relation_write_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        facts=facts,
        objects={"system.menu"},
    )
    if context is None:
        return
    catalog = relation_catalog()
    for change in changes:
        if change.key.kind != K.MENU or change.action == "delete":
            continue
        access = CREATE if change.action == "insert" else UPDATE
        action = "menu.create" if access == CREATE else "menu.update"
        ordinary = {
            f.field_id
            for f in catalog.objects["system.menu"].fields
            if access in f.accesses
        }
        context.write(
            "system.menu",
            change.key.identity[0],
            action,
            access,
            ordinary & change.values.keys(),
        )


def validate_generic_user_fields(context, operation, payload, target_id, new_id):
    if operation == "user.profile":
        # Preserve the existing fixed minimal self grant, with explicit role denies.
        if target_id != context.tenant.actor_user_id:
            raise denied()
        fields = payload.model_fields_set
        if not set(fields) <= {
            "nickname",
            "user_gender",
            "user_phone",
            "user_email",
            "user_avatar",
        }:
            raise denied()
        record = context.record("system.user", target_id)
        access = Access("update", "own-account")
        if any(
            g.effect == "deny"
            and g.role_id in record.role_ids
            and g.access == access
            and g.ref.field_id in fields
            for g in context.rules.index.values()
        ):
            raise denied()
        return
    if operation in {"user.create", "user.update"}:
        groups = dict(user_write_footprint(payload))
        if operation == "user.create":
            # The server always assigns a role, even when role_ids was omitted.
            groups["replace", "management.relations"] = {"roles"} | (
                {"departments"}
                if payload.dept_ids or "dept_ids" in payload.model_fields_set
                else set()
            )
    elif operation in {"user.roles", "user.departments", "dept.members"}:
        groups = {
            ("replace", "management.relations"): (
                "roles" if operation == "user.roles" else "departments",
            )
        }
    else:
        groups = {("update", "management.update"): ("password",)}
    if operation == "dept.members":
        ids = {
            r["user_id"]
            for r in context.facts[K.USER_DEPTS]
            if r["dept_id"] == target_id
        } | {int(uid) for uid in payload}
    else:
        ids = {new_id or target_id}
    for uid in ids:
        for (op, purpose), fields in groups.items():
            context.write(
                "system.user",
                uid,
                "dept.members-write" if operation == "dept.members" else operation,
                Access(op, purpose),
                fields,
                proposed_depts={int(d.dept_id) for d in payload.dept_ids}
                if operation == "user.create"
                else None,
            )


async def prove_leader_lookup(db, context, action):
    catalog = relation_catalog()
    scopes = {
        rid: and_(true(), *get_user_filters_from_resolution(scope))
        for rid, scope in context.scopes.items()
    }
    base = and_(
        User.tenant_id == context.tenant.tenant_id, or_(false(), *scopes.values())
    )
    record = RecordContext(
        context.tenant.tenant_id,
        "system.user",
        "leader-query",
        frozenset(scopes),
        context.permissions,
    )
    dependencies = tuple(
        FieldUse(FieldRef("system.user", f), EXACT) for f in ("user_name", "nickname")
    )
    counter = query_counterexample(
        catalog, context.rules, record, action, dependencies, base, scopes
    )
    if await db.scalar(select(select(User.user_id).where(counter).exists())):
        raise denied()


def validate_leader_source(context, operation, prepared):
    source = prepared.get("leader")
    if (
        context is None
        or operation not in {"dept.create", "dept.update"}
        or source is None
    ):
        return
    record = context.record("system.user", int(source["userId"]))
    decisions = resolve_fields(
        relation_catalog(), context.rules, record, operation, READ
    )
    if not record.role_ids or any(
        not decisions[f].allowed for f in ("user_name", "nickname")
    ):
        raise denied()
