"""One manifest and per-role scopes for tenant delivery copies."""

from dataclasses import dataclass, field

from sqlalchemy import and_, case, false, literal, or_, select, true

from app.core.field_policy import Access, project
from app.core.field_policy.contracts import denied, limit
from app.core.tenant import bind_tenant_context
from app.modules.system.field_policy.delivery_catalog import READ
from app.modules.system.service.relation_read_context import (
    RelationReadContext,
    detached,
    load_relation_read_context,
)


@dataclass
class DeliveryReadContext(RelationReadContext):
    visible_agent_codes: frozenset = field(default_factory=frozenset)
    baselines: dict = field(default_factory=dict)

    def scope_sql(self, obj, table):
        if obj in {"system.user", "system.dept", "system.role", "system.menu"}:
            return super().scope_sql(obj, table)
        if obj == "ai.agent":
            return dict.fromkeys(
                self.scopes, table.c.code.in_(self.visible_agent_codes)
            )
        if obj == "system.file":
            return dict.fromkeys(
                self.scopes,
                and_(
                    table.c.owner_user_id == self.actor.user_id, table.c.del_flag == "0"
                ),
            )
        if obj == "job.definition":
            from app.modules.job.task_registry import tenant_task_keys  # noqa: PLC0415

            return dict.fromkeys(self.scopes, table.c.job_key.in_(tenant_task_keys()))
        if obj in {"ai.conversation", "ai.message"}:
            if obj == "ai.conversation":
                own = and_(
                    table.c.user_id == self.actor.user_id, table.c.deleted_at.is_(None)
                )
            else:
                from app.modules.ai.models.conversation import (  # noqa: PLC0415
                    AiConversation,
                )

                parent = AiConversation.__table__
                own = (
                    select(parent.c.conversation_id)
                    .where(
                        parent.c.tenant_id == self.tenant.tenant_id,
                        parent.c.conversation_id == table.c.conversation_id,
                        parent.c.user_id == self.actor.user_id,
                        parent.c.deleted_at.is_(None),
                    )
                    .correlate(table)
                    .exists()
                )
                own = and_(own, table.c.is_active.is_(True))
            return dict.fromkeys(self.scopes, own)
        return dict.fromkeys(self.scopes, true())

    def base(self, obj, table):
        scopes = self.scope_sql(obj, table)
        predicate = or_(false(), *scopes.values())
        if obj != "ai.agent":
            predicate = and_(table.c.tenant_id == self.tenant.tenant_id, predicate)
        if obj in {"system.login_log", "system.operation_log"}:
            predicate = and_(predicate, table.c.audit_scope == "tenant")
        return and_(predicate, *self.baselines.get(obj, ()))

    async def rows(
        self,
        db,
        obj,
        model,
        anchor,
        *,
        names=None,
        predicates=(),
        order=(),
        offset=0,
        maximum=100,
        internals=(),
    ):
        table = model.__table__
        fields = tuple(
            f
            for f in self.catalog.objects[obj].fields
            if f.field_id in table.c
            and (names is None or f.field_id in names or f.field_id == anchor)
        )
        scopes = self.scope_sql(obj, table)
        columns = []
        for f in fields:
            value = table.c[f.field_id]
            if any(source.object_id != obj for source in f.sources):
                value = literal(None)
            if obj == "ai.conversation" and f.field_id == "title":
                from app.modules.ai.service.conversation_delivery import (  # noqa: PLC0415
                    known_title,  # noqa: PLC0415
                )

                value = case((known_title(table), value), else_=None)
            columns.append(
                case((self.coverage(obj, table, f.field_id), value), else_=None).label(
                    f.field_id
                )
            )
        columns += [
            case((scope, True), else_=False).label(f"_role_{i}")
            for i, scope in enumerate(scopes.values())
        ]
        columns += [table.c[n].label("_" + n) for n in internals]
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
        output = []
        for row in rows:
            record = self.record(
                obj,
                row[anchor],
                (rid for i, rid in enumerate(scopes) if row[f"_role_{i}"]),
            )
            values = detached(
                project(
                    self.catalog,
                    self.rules,
                    record,
                    self.action,
                    READ,
                    {f.field_id: row[f.field_id] for f in fields},
                )
            )
            if obj == "ai.conversation" and values.get("title") is None:
                values.pop("title", None)
            output.append((record, values, {n: row["_" + n] for n in internals}))
        return output

    async def user_names(self, db, ids):
        from app.modules.system.models.user import User  # noqa: PLC0415

        identifiers = tuple(sorted(set(ids) - {None, 0}))
        limit(len(identifiers) <= 5000)
        result = {}
        for offset in range(0, len(identifiers), 256):
            for record, values, _ in await self.rows(
                db,
                "system.user",
                User,
                "user_id",
                names={"user_name"},
                predicates=(User.user_id.in_(identifiers[offset : offset + 256]),),
                maximum=256,
            ):
                if "user_name" in values:
                    result[int(record.record_key)] = values["user_name"]
        return result

    def policy(self, record):
        writes = {}
        for operation in ("create", "update"):
            access = Access(operation, "management." + operation)
            writes[operation] = (
                sorted(
                    name
                    for name, decision in self.decisions(record, access).items()
                    if decision.allowed
                )
                if any(
                    use.ref.object_id == record.object_id and use.access == access
                    for use in self.catalog.actions[self.action].uses
                )
                else []
            )
        return {
            **writes,
            "read": sorted(
                name
                for name, decision in self.decisions(record).items()
                if decision.allowed
            ),
            "query": sorted(
                {
                    use.ref.field_id
                    for use in self.catalog.actions[self.action].uses
                    if use.ref.object_id == record.object_id
                    and use.access.purpose == "management.query"
                    and self.decisions(record, use.access)[use.ref.field_id].allowed
                }
            ),
        }


async def load_delivery_context(
    db, tx, *, tenant, version, action, objects=None, agents=False
):
    from app.modules.system.field_policy.ordinary_catalog import (  # noqa: PLC0415
        production_catalog,
    )

    catalog = production_catalog()
    if action not in catalog.actions:
        raise denied()
    original = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=version,
        action=action,
        objects=set(objects or catalog.actions[action].object_ids),
    )
    context = DeliveryReadContext(
        original.tenant,
        original.actor,
        original.rules,
        original.scopes,
        original.permissions,
        original.action,
    )
    if tenant.source == "access_token":
        bind_tenant_context(context.actor, tenant)
    if action.startswith(
        ("ai.", "delivery.message", "delivery.agent", "delivery.conversation")
    ):
        from app.core.auth import has_explicit_permission  # noqa: PLC0415

        if not has_explicit_permission(context.actor, "ai:chat:use"):
            raise denied()
    if agents or "ai.agent" in catalog.actions[action].object_ids:
        from app.modules.ai.service.agent_authorization_service import (  # noqa: PLC0415
            agent_authorization_service,
        )

        if action in {"ai.tool.role.agent_lookup", "ai.tool.role.update_agents"}:
            from app.modules.ai.models.agent import AiAgent  # noqa: PLC0415
            from app.modules.system.service.grant_authority import (  # noqa: PLC0415
                grant_authority_service,  # noqa: PLC0415
            )

            authority = await grant_authority_service.build(
                db, context.actor.user_id, tenant=tenant
            )
            statement = select(AiAgent.code)
            if not authority.super_admin:
                statement = statement.where(
                    AiAgent.agent_id.in_(authority.grantable_agent_ids)
                )
            codes = tuple(
                await db.scalars(statement.order_by(AiAgent.agent_id).limit(1025))
            )
            limit(len(codes) <= 1024)
            context.visible_agent_codes = frozenset(codes)
        else:
            visible = await agent_authorization_service.list_agents(
                db, context.actor, force_refresh=True
            )
            limit(len(visible) <= 1024)
            context.visible_agent_codes = frozenset(a.code for a in visible)
    return context
