"""Live relation scopes, pre-filter proofs and SQL-masked scalar fetching."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import and_, case, false, or_, select, true

from app.core.exceptions import AuthorizationException
from app.core.field_policy import (
    FieldRef,
    FieldUse,
    Grant,
    RecordContext,
    compile_rules,
    project,
    query_counterexample,
    resolve_fields,
)
from app.core.field_policy.contracts import (
    canonical,
    denied,
    identity,
    limit,
    primitive,
    require,
)
from app.core.field_policy.sources import SourceRef
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.field_policy.generic_compat import convert_legacy_grants
from app.modules.system.field_policy.ordinary_catalog import relation_catalog
from app.modules.system.field_policy.relation_catalog import READ
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_read_budget import (
    authorization_read_phase,
    read_phase,
)
from app.modules.system.service.generic_field_policy_service import (
    failure,
    generic_field_policy_service,
)
from app.modules.system.service.relation_capabilities import relation_write_permissions
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_field_context import load_current_user
from app.utils.data_scope import (
    get_user_filters_from_resolution,
    resolve_data_scope_for_roles,
)


def detached(value):
    if isinstance(value, Mapping):
        return {key: detached(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [detached(item) for item in value]
    return value


@dataclass
class RelationReadContext:
    tenant: object
    actor: object
    rules: object
    scopes: dict
    permissions: frozenset
    action: str
    source_count: int = 0
    source_bytes: int = 2
    source_refs: list = field(default_factory=list)

    @property
    def catalog(self):
        return relation_catalog()

    def scope_sql(self, obj, table):
        if obj in {"system.role", "system.menu"}:
            return {rid: true() for rid in self.scopes}
        if obj == "system.dept":
            return {
                rid: true()
                if scope.unbounded
                else table.c.dept_id.in_(scope.accessible_dept_ids or ())
                for rid, scope in self.scopes.items()
            }
        return {
            rid: and_(*get_user_filters_from_resolution(scope))
            for rid, scope in self.scopes.items()
        }

    def base(self, obj, table):
        scopes = self.scope_sql(obj, table)
        return and_(
            table.c.tenant_id == self.tenant.tenant_id, or_(false(), *scopes.values())
        )

    def record(self, obj, key, roles):
        return RecordContext(
            self.tenant.tenant_id, obj, str(key), frozenset(roles), self.permissions
        )

    def coverage(self, obj, table, name, access=READ):
        ref = FieldRef(obj, name)
        if self.catalog.fields[ref].structural and access.operation == "read":
            return true()
        if FieldUse(ref, access) not in self.catalog.actions[self.action].uses:
            return false()
        allow, deny = [], []
        for rid, scope in self.scope_sql(obj, table).items():
            grant = self.rules.index.get((self.tenant.tenant_id, rid, ref, access))
            if grant:
                (allow if grant.effect == "allow" else deny).append(scope.is_(True))
        result = and_(or_(false(), *allow), ~or_(false(), *deny))
        if access == READ:
            result = and_(
                result,
                *(
                    self.coverage(obj, table, source.field_id, READ)
                    for source in self.catalog.fields[ref].sources
                    if source.object_id == obj
                ),
            )
        return result

    def decisions(self, record, access=READ):
        return resolve_fields(self.catalog, self.rules, record, self.action, access)

    @read_phase("proof", session_arg=1)
    async def prove(self, db, obj, table, dependencies=(), *, readable=()):
        scopes = self.scope_sql(obj, table)
        base = self.base(obj, table)
        if dependencies:
            counter = query_counterexample(
                self.catalog,
                self.rules,
                self.record(obj, "query", scopes),
                self.action,
                tuple(
                    FieldUse(FieldRef(obj, name), access)
                    for name, access in dependencies
                ),
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
        if readable:
            if await db.scalar(
                select(
                    select(next(iter(table.primary_key.columns)))
                    .select_from(table)
                    .where(
                        base, ~and_(*(self.coverage(obj, table, f) for f in readable))
                    )
                    .exists()
                )
            ):
                raise denied()

    @read_phase("projection", session_arg=1, preserve=("source",))
    async def fetch(
        self,
        db,
        model,
        anchor,
        *,
        predicates=(),
        order=(),
        offset=0,
        maximum=100,
        names=None,
        access=READ,
        internals=(),
    ):
        obj, table = "system." + model.__name__.lower(), model.__table__
        fields = tuple(
            f
            for f in self.catalog.objects[obj].fields
            if f.field_id in table.c
            and (names is None or f.field_id in names or f.field_id == anchor)
        )
        scopes = self.scope_sql(obj, table)
        columns = [
            case(
                (
                    self.coverage(obj, table, f.field_id, access),
                    case((table.c.leader.is_(None), None), else_="")
                    if obj == "system.dept" and f.field_id == "leader"
                    else table.c[f.field_id],
                ),
                else_=None,
            ).label(f.field_id)
            for f in fields
        ]
        columns += [
            case((scope, True), else_=False).label(f"_role_{i}")
            for i, scope in enumerate(scopes.values())
        ]
        columns += [
            (
                case(
                    (
                        and_(
                            self.coverage(obj, table, "leader"),
                            table.c.leader.is_not(None),
                            table.c.leader != "",
                        ),
                        True,
                    ),
                    else_=False,
                )
                if n == "leader_has_text" and obj == "system.dept"
                else table.c[n]
            ).label("_" + n)
            for n in internals
        ]
        rows = (
            (
                await db.execute(
                    select(*columns)
                    .where(self.base(obj, table), *predicates)
                    .order_by(*order)
                    .offset(offset)
                    .limit(maximum)
                )
            )
            .mappings()
            .all()
        )
        result = []
        for row in rows:
            record = self.record(
                obj,
                row[anchor],
                (rid for i, rid in enumerate(scopes) if row[f"_role_{i}"]),
            )
            values = {f.field_id: row[f.field_id] for f in fields}
            # Source-dependent i18n metadata cannot restore a hidden label.
            decisions = self.decisions(record, access)
            for meta, label in (("i18n_keys", "role_name"), ("i18n_key", "menu_name")):
                if meta in values and not decisions[label].allowed:
                    values.pop(meta)
            output = detached(
                project(self.catalog, self.rules, record, self.action, access, values)
            )
            if "i18n_keys" in output and output["i18n_keys"] is not None:
                require(type(output["i18n_keys"]) is dict)
                require(
                    all(
                        type(value) is str
                        for key, value in output["i18n_keys"].items()
                        if key in {"roleName", "roleDesc"}
                    )
                )
                output["i18n_keys"] = {
                    key: value
                    for key, value in output["i18n_keys"].items()
                    if key == "roleName"
                    or (key == "roleDesc" and decisions["role_desc"].allowed)
                }
            result.append((record, output, {n: row["_" + n] for n in internals}))
        return result

    def source(self, record, names, access=READ):
        require(
            type(record) is RecordContext
            and record.tenant_id == self.tenant.tenant_id
            and record.role_ids <= self.scopes.keys()
            and record.permission_codes == self.permissions
        )
        identity(record.record_key)
        decisions = self.decisions(record, access)
        fixed_own = (
            self.action == "user.own-read"
            and record.object_id == "system.user"
            and record.record_key == str(self.actor.user_id)
            and access.purpose == "own-account"
        )
        require(
            all(
                name in decisions and (decisions[name].allowed or fixed_own)
                for name in names
            )
        )
        for name in names:
            ref = SourceRef(
                "tenant",
                self.tenant.tenant_id,
                record.record_key,
                FieldRef(record.object_id, name),
                access,
                self.catalog.digest,
                "relation-read/v1",
            )
            encoded = canonical(primitive(ref), 4 * 1024 * 1024)
            self.source_count += 1
            self.source_bytes += len(encoded) + 1
            limit(self.source_count <= 16384 and self.source_bytes <= 4 * 1024 * 1024)
            self.source_refs.append(ref)


async def load_relation_read_context(
    db, tx, *, tenant, actor_user_version, action, objects, write_form=False
):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    with authorization_read_phase(db, "identity"):
        actor = await load_current_user(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
    roles = tuple(r for r in actor.roles if r.status == "1")
    limit(len(roles) <= 1024)
    catalog = relation_catalog()
    permissions = frozenset(collect_user_buttons(actor))
    if action == "user.own-read":
        # Fixed authenticated self-service protocol; never a delegated business
        # permission and never used for management actions.
        permissions |= frozenset({"auth:own-account:read"})
    if any(r.role_code == "R_SUPER" for r in roles):
        permissions |= frozenset(
            p for a in catalog.actions.values() for p in a.permission_codes
        )
    functional = (
        relation_write_permissions(actor, catalog, action.split(".")[0])
        if write_form
        else permissions
    )
    if not set(catalog.actions[action].permission_codes) <= functional:
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    ids = tuple(str(r.role_id) for r in roles)
    with authorization_read_phase(db, "manifest"):
        head = await db.scalar(
            select(FieldPolicyHead.tenant_id).where(
                FieldPolicyHead.tenant_id == tenant.tenant_id
            )
        )
        grants = ()
        if head is None:
            state = (
                await db.execute(
                    select(UserFieldPolicyState.mode).where(
                        UserFieldPolicyState.tenant_id == tenant.tenant_id
                    )
                )
            ).scalar_one_or_none()
            if state not in {"legacy", "ready", "enforcing", "frozen"}:
                raise failure()
            grants = tuple(
                Grant(tenant.tenant_id, rid, ref, a)
                for rid in ids
                for ref, f in catalog.fields.items()
                if ref.object_id in objects - {"system.user"}
                for a in f.accesses
            )
            if "system.user" in objects:
                legacy = await active_policy_grants(
                    db, tx, tenant_id=tenant.tenant_id, role_ids=ids
                )
                grants += (
                    convert_legacy_grants(tenant.tenant_id, legacy)
                    if legacy is not None
                    else tuple(
                        Grant(tenant.tenant_id, rid, ref, a)
                        for rid in ids
                        for ref, f in catalog.fields.items()
                        if ref.object_id == "system.user"
                        for a in f.accesses
                    )
                )
        else:
            for obj in sorted(objects):
                grants += await generic_field_policy_service.load_active(
                    db,
                    tx,
                    tenant_id=tenant.tenant_id,
                    catalog=catalog,
                    object_id=obj,
                    role_ids=ids,
                )
    with authorization_read_phase(db, "scope"):
        scopes = {
            str(r.role_id): await resolve_data_scope_for_roles(
                db, user=actor, roles=[r], tenant=tenant
            )
            for r in roles
        }
    return RelationReadContext(
        tenant, actor, compile_rules(catalog, grants), scopes, permissions, action
    )
