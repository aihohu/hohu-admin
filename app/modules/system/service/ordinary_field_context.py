"""Current per-role record scopes and immutable ordinary-field policy rules."""

from dataclasses import dataclass

from sqlalchemy import and_, false, or_, select, true

from app.core.field_policy import (
    FieldRef,
    FieldUse,
    Grant,
    RecordContext,
    compile_rules,
    project,
    query_counterexample,
    resolve_fields,
    validate_write,
)
from app.core.field_policy.contracts import denied
from app.core.rbac import is_super_admin
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.field_policy.ordinary_catalog import (
    EXACT,
    READ,
    SURFACES,
    production_catalog,
)
from app.modules.system.models.dict_type import DictType
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.generic_field_policy_service import (
    failure,
    generic_field_policy_service,
)
from app.modules.system.service.user_field_context import load_current_user
from app.utils.data_scope import resolve_data_scope_for_roles


@dataclass
class OrdinaryContext:
    tenant: object
    actor: object
    rules: object
    scopes: dict
    permissions: frozenset
    object_id: str
    action: str

    def record(self, values, key="scope"):
        applicable = frozenset(
            role
            for role, scope in self.scopes.items()
            if scope is None
            or scope.unbounded
            or (scope.include_self and values.get("create_by") == self.actor.user_id)
            or values.get("dept_id") in (scope.accessible_dept_ids or ())
        )
        return RecordContext(
            self.tenant.tenant_id,
            self.object_id,
            str(key),
            applicable,
            self.permissions,
        )

    def scope_sql(self, table):
        return {
            role: true()
            if scope is None or scope.unbounded
            else or_(
                false(),
                table.c.create_by == self.actor.user_id
                if scope.include_self
                else false(),
                table.c.dept_id.in_(scope.accessible_dept_ids or ()),
            )
            for role, scope in self.scopes.items()
        }

    def fields(self, record, access=READ):
        return resolve_fields(
            production_catalog(), self.rules, record, self.action, access
        )

    def output(self, record, values):
        allowed_values = {
            f.field_id: values[f.field_id]
            for f in production_catalog().objects[self.object_id].fields
            if f.field_id in values
        }
        return dict(
            project(
                production_catalog(),
                self.rules,
                record,
                self.action,
                READ,
                allowed_values,
            )
        )

    def write(self, values, footprint, access):
        record = self.record(values)
        if not record.role_ids:
            raise denied()
        validate_write(
            production_catalog(),
            self.rules,
            record,
            self.action,
            access,
            tuple(footprint),
        )

    def capabilities(self, record, operation, access):
        catalog = production_catalog()
        action = self.action.split(".")[0] + "." + operation
        if not set(catalog.actions[action].permission_codes) <= self.permissions:
            return []
        return [
            name
            for name, d in resolve_fields(
                catalog, self.rules, record, action, access
            ).items()
            if d.allowed
        ]


async def load_ordinary_context(
    db, tx, *, tenant, actor_user_version, surface, operation
):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    catalog = production_catalog()
    object_id = SURFACES[surface][0]
    action = f"{surface}.{operation}"
    actor = await load_current_user(
        db, tenant=tenant, actor_user_version=actor_user_version
    )
    required = frozenset(catalog.actions[action].permission_codes)
    permissions = frozenset(collect_user_buttons(actor))
    if is_super_admin(actor):
        permissions |= frozenset(
            p for a in catalog.actions.values() for p in a.permission_codes
        )
    if not required <= permissions:
        raise denied()
    roles = [r for r in actor.roles if r.status == "1"]
    role_ids = tuple(str(r.role_id) for r in roles)
    head = await db.scalar(
        select(FieldPolicyHead.tenant_id).where(
            FieldPolicyHead.tenant_id == tenant.tenant_id
        )
    )
    if head is None:
        state = (
            await db.execute(
                select(
                    UserFieldPolicyState.mode, UserFieldPolicyState.ever_enforced
                ).where(UserFieldPolicyState.tenant_id == tenant.tenant_id)
            )
        ).one_or_none()
        if (
            state is None
            or state.ever_enforced
            or state.mode not in {"legacy", "ready"}
        ):
            raise failure()
        grants = tuple(
            Grant(tenant.tenant_id, rid, ref, a)
            for rid in role_ids
            for ref, f in catalog.fields.items()
            if ref.object_id == object_id
            for a in f.accesses
        )
    else:
        grants = await generic_field_policy_service.load_active(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            catalog=catalog,
            object_id=object_id,
            role_ids=role_ids,
        )
    scopes = {
        str(r.role_id): await resolve_data_scope_for_roles(
            db, user=actor, roles=[r], tenant=tenant
        )
        if surface == "data-scope-demo"
        else None
        for r in roles
    }
    return OrdinaryContext(
        tenant,
        actor,
        compile_rules(catalog, grants),
        scopes,
        permissions,
        object_id,
        action,
    )


def read_coverage(context, role_scopes, field):
    """SQL CASE fetches a cell only where its applicable roles may read it."""
    catalog = production_catalog()
    if catalog.fields[field].structural:
        return true()
    allow, deny = [], []
    for rid, scope in role_scopes.items():
        grant = context.rules.index.get((context.tenant.tenant_id, rid, field, READ))
        if grant:
            (allow if grant.effect == "allow" else deny).append(scope.is_(True))
    return and_(or_(false(), *allow), ~or_(false(), *deny))


async def prove_parent_lookup(db, context):
    table = DictType.__table__
    scopes = context.scope_sql(table)
    counter = query_counterexample(
        production_catalog(),
        context.rules,
        RecordContext(
            context.tenant.tenant_id,
            context.object_id,
            "parent-query",
            frozenset(scopes),
            context.permissions,
        ),
        context.action,
        (FieldUse(FieldRef("system.dict_type", "dict_type"), EXACT),),
        and_(
            table.c.tenant_id == context.tenant.tenant_id,
            or_(false(), *scopes.values()),
        ),
        scopes,
    )
    if await db.scalar(select(select(table.c.dict_type_id).where(counter).exists())):
        raise denied()
