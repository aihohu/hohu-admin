"""Live tenant control fields and SQL ceilings under the caller's G/B."""

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import String, and_, case, func, or_, select, true

from app.core.exceptions import AuthorizationException
from app.core.field_policy import (
    Grant,
    compile_rules,
    project,
    resolve_fields,
    validate_write,
)
from app.core.field_policy.contracts import limit
from app.db.authorization_plan_session import _order_error
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.field_policy.control_catalog import (
    CREATE,
    EXPORT,
    IMPORT_CREATE,
    IMPORT_MATCH,
    NEXT_RUN_SOURCES,
    READ,
    UPDATE,
    control_catalog,
    project_next_run,
)
from app.modules.system.field_policy.control_values import _SECRET_PARTS
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
from app.modules.system.service.relation_read_context import (
    RelationReadContext,
    detached,
)
from app.modules.system.service.user_field_context import load_current_user
from app.modules.system.settings_catalog import BUILTIN_KEYS


def custom_config_scope(table):
    return and_(
        table.c.config_key.not_in(BUILTIN_KEYS),
        *(
            ~table.c.config_key.startswith(prefix)
            for prefix in ("ai:", "security:", "upload:")
        ),
    )


def value_ceiling(table):
    """No unknown, secret, compound or oversized bytes cross the SQL boundary."""
    normalized = func.pg_catalog.regexp_replace(
        func.pg_catalog.lower(table.c.config_key, type_=String()),
        "[^[:alnum:]]",
        "",
        "g",
        type_=String(),
    )
    trimmed = func.pg_catalog.ltrim(
        table.c.config_value, " \t\r\n\ufeff", type_=String()
    )
    return and_(
        table.c.value_sensitivity == "private",
        table.c.config_type.in_(("text", "richtext")),
        func.pg_catalog.octet_length(table.c.config_value) <= 8192,
        ~trimmed.like("{%"),
        ~trimmed.like("[%"),
        *(~normalized.contains(part, autoescape=True) for part in _SECRET_PARTS),
    )


class ParameterContext(RelationReadContext):
    source_version = "parameter-read/v1"

    @property
    def catalog(self):
        return control_catalog()

    def scope_sql(self, obj, _table):
        if obj not in {"system.config", "system.setting", "job.definition"} and not (
            obj == "system.role" and self.action == "settings.registration-options"
        ):
            raise _order_error()
        return {rid: true() for rid in self.scopes}

    def base(self, obj, table):
        inherited = super().base(obj, table)
        return (
            and_(inherited, custom_config_scope(table))
            if obj == "system.config"
            else inherited
        )

    def coverage(self, obj, table, name, access=READ):
        result = super().coverage(obj, table, name, access)
        if obj == "system.config" and name == "config_value":
            result = and_(result, value_ceiling(table))
        if obj == "job.definition" and name == "job_args":
            result = and_(
                result,
                table.c.job_key == "test_task",
                or_(
                    table.c.job_args.is_(None),
                    and_(
                        func.pg_catalog.octet_length(table.c.job_args) <= 8192,
                        table.c.job_args.op("~")(r"^\s*\{\s*\}\s*$"),
                    ),
                ),
            )
        if access == EXPORT:
            result = and_(result, self.coverage(obj, table, name, READ))
        return result

    def allowed(self, record, action, access):
        if not set(self.catalog.actions[action].permission_codes) <= self.permissions:
            return []
        return sorted(
            n
            for n, d in resolve_fields(
                self.catalog, self.rules, record, action, access
            ).items()
            if d.allowed
        )

    def capabilities(self, record, *, hidden=()):
        surface = "config" if record.object_id == "system.config" else "job"
        decisions = self.decisions(record)
        derived = surface == "job" and all(
            decisions[name].allowed for name in NEXT_RUN_SOURCES
        )
        return {
            "read": [
                n
                for n, d in decisions.items()
                if (d.allowed or n == "next_run_time" and derived) and n not in hidden
            ],
            "read_levels": {
                n: (
                    "hidden"
                    if n in hidden
                    else "plain"
                    if n == "next_run_time" and derived
                    else d.level
                )
                for n, d in decisions.items()
            },
            "query": sorted(
                {
                    u.ref.field_id
                    for u in self.catalog.actions[surface + ".list"].uses
                    if u.access.purpose == "management.query"
                    and set(self.catalog.actions[surface + ".list"].permission_codes)
                    <= self.permissions
                    and resolve_fields(
                        self.catalog, self.rules, record, surface + ".list", u.access
                    )[u.ref.field_id].allowed
                }
            ),
            "create": self.allowed(record, surface + ".create", CREATE),
            "update": self.allowed(record, surface + ".update", UPDATE),
            "special_create": [],
            "special_update": sorted(
                {
                    name
                    for action in ("job.schedule", "job.status")
                    for name in self.allowed(record, action, UPDATE)
                }
            )
            if surface == "job"
            else [],
            "can_create": bool(record.role_ids)
            and {
                f.field_id
                for f in self.catalog.objects[record.object_id].fields
                if CREATE in f.accesses
            }
            <= set(self.allowed(record, surface + ".create", CREATE)),
            "complete": {},
            "control_actions": self.control_actions(
                record,
                visible={
                    name
                    for name, decision in decisions.items()
                    if decision.allowed and name not in hidden
                },
            ),
        }

    def control_actions(self, record, *, visible):
        """Pure hints from this protected context; no IO or authority transfer."""
        config = record.object_id == "system.config"
        can_import = config and all(
            bool(
                required := {
                    use.ref.field_id
                    for use in self.catalog.actions["config.import"].uses
                    if use.access == access
                }
            )
            and required <= set(self.allowed(record, "config.import", access))
            for access in (IMPORT_CREATE, IMPORT_MATCH)
        )
        can_export = config and bool(
            visible
            & set(self.allowed(record, "config.export", READ))
            & set(self.allowed(record, "config.export", EXPORT))
        )
        can_run = record.object_id == "job.definition" and {
            "job_id",
            "job_key",
            "job_args",
            "status",
        } <= visible & set(self.allowed(record, "job.run", READ))
        return {"can_import": can_import, "can_export": can_export, "can_run": can_run}

    def write(self, obj, key, action, access, fields):
        validate_write(
            self.catalog,
            self.rules,
            self.record(obj, key, self.scopes),
            action,
            access,
            tuple(fields),
        )

    @read_phase("projection", session_arg=1)
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
        access=READ,
        names=None,
    ):
        obj = "system.config" if anchor == "config_id" else "job.definition"
        table = model.__table__
        fields = tuple(
            f
            for f in self.catalog.objects[obj].fields
            if f.field_id in table.c
            and (names is None or f.field_id in names or f.field_id == anchor)
        )
        columns = [
            table.c[f.field_id]
            if f.field_id == anchor
            else case(
                (self.coverage(obj, table, f.field_id, access), table.c[f.field_id]),
                else_=None,
            ).label(f.field_id)
            for f in fields
        ]
        if obj == "job.definition":
            columns.append(
                case(
                    (self.coverage(obj, table, "job_args", access), True), else_=False
                ).label("_job_args_visible")
            )
        rows = (
            (
                await db.execute(
                    select(*columns)
                    .where(self.base(obj, table), *predicates)
                    .order_by(*(order or (table.c[anchor],)))
                    .offset(offset)
                    .limit(maximum)
                )
            )
            .mappings()
            .all()
        )
        result = []
        for row in rows:
            record = self.record(obj, row[anchor], self.scopes)
            raw = {f.field_id: row[f.field_id] for f in fields}
            if access == EXPORT:
                reads = self.decisions(record, READ)
                raw = {
                    name: value for name, value in raw.items() if reads[name].allowed
                }
            hidden = []
            for name in ("config_value", "job_args"):
                if name in raw and (
                    not row["_job_args_visible"]
                    if name == "job_args"
                    else raw[name] is None
                ):
                    raw.pop(name)
                    hidden.append(name)
            values = detached(
                project(self.catalog, self.rules, record, self.action, access, raw)
            )
            if obj == "job.definition" and self.action == "job.list":
                decisions = self.decisions(record)
                if all(decisions[name].allowed for name in NEXT_RUN_SOURCES) and all(
                    name in values for name in NEXT_RUN_SOURCES
                ):
                    from app.core.scheduler import build_trigger  # noqa: PLC0415

                    sources = {name: values[name] for name in NEXT_RUN_SOURCES}
                    computed = None
                    if sources["status"] == "1":
                        try:
                            computed = build_trigger(
                                SimpleNamespace(**sources)
                            ).get_next_fire_time(None, datetime.now(UTC))
                        except (ValueError, TypeError, KeyError):
                            pass
                    cell = project_next_run(
                        self.catalog, self.rules, record, sources, computed
                    )
                    if cell.visible:
                        values["next_run_time"] = cell.value
                        self.source(record, NEXT_RUN_SOURCES)
            result.append((record, values, self.capabilities(record, hidden=hidden)))
        return result


async def load_parameter_context(db, tx, *, tenant, actor_user_version, action):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    catalog = control_catalog()
    if action not in catalog.actions:
        raise failure()
    objects = set(catalog.actions[action].object_ids)
    allowed = {"system.config", "system.setting", "job.definition"} | (
        {"system.role"} if action == "settings.registration-options" else set()
    )
    if not objects <= allowed:
        raise _order_error()
    with authorization_read_phase(db, "identity"):
        actor = await load_current_user(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
    roles = tuple(r for r in actor.roles if r.status == "1")
    limit(len(roles) <= 1024)
    permissions = frozenset(collect_user_buttons(actor))
    if any(r.role_code == "R_SUPER" for r in roles):
        permissions |= frozenset(
            p
            for a in catalog.actions.values()
            for p in a.permission_codes
            if p != "system:config:publish"
        )
    if not set(catalog.actions[action].permission_codes) <= permissions:
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
            state = await db.scalar(
                select(UserFieldPolicyState.mode).where(
                    UserFieldPolicyState.tenant_id == tenant.tenant_id
                )
            )
            if state not in {"legacy", "ready", "enforcing", "frozen"}:
                raise failure()
            grants = tuple(
                Grant(tenant.tenant_id, rid, ref, a)
                for rid in ids
                for ref, f in catalog.fields.items()
                if ref.object_id in objects
                for a in f.accesses
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
    return ParameterContext(
        tenant,
        actor,
        compile_rules(catalog, grants),
        dict.fromkeys(ids),
        permissions,
        action,
    )
