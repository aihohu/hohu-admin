"""New owner sessions release G/B and acknowledge rollback before returning."""

from sqlalchemy import func, select

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.field_policy.contracts import limit
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.job.builtin_task_specs import builtin_task_contracts
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobQuery
from app.modules.system.field_policy.control_catalog import (
    CONTAINS,
    EQ,
    EXPORT,
    READ,
    UPDATE,
)
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.schemas.config import ConfigQuery
from app.modules.system.schemas.parameter_form import ParameterFormQuery
from app.modules.system.schemas.parameter_projection import (
    ParameterFieldPolicy,
    serialize_parameter,
)
from app.modules.system.service.authorization_read_budget import (
    RELATION_READ_PHASE_LIMITS,
    AuthorizationReadBudget,
    authorization_read_phase,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.parameter_context import load_parameter_context
from app.modules.system.service.parameter_facts import parameter_rows
from app.modules.system.service.parameter_public_read import public_parameters
from app.modules.system.service.parameter_receipt import (
    issue_receipt,
    observed_form_facts,
    publication_revision,
)
from app.modules.system.service.parameter_settings import (
    describe_mail,
    fixed_actor,
    read_fixed_settings,
    read_group,
)
from app.utils.closed_json import closed_object


class ParameterReadOwner:
    async def execute(
        self, db, *, tenant, actor_user_version, operation, query=None, target_id=None
    ):
        require_candidate_session(db)
        surface = operation.split(".")[0] if type(operation) is str else ""
        if (
            operation
            not in {
                "config.list",
                "job.list",
                "job.registered",
                "config.form",
                "job.form",
                "config.public",
                "config.export",
                "settings.group",
                "mail.describe",
                "settings.public",
                "settings.runtime",
                "settings.groups",
            }
            or target_id is not None
        ):
            raise _order_error()
        schema, model, anchor, obj = (
            (ConfigQuery, Config, "config_id", "system.config")
            if surface == "config"
            else (JobQuery, SysJob, "job_id", "job.definition")
        )
        if operation.endswith(".form"):
            schema = ParameterFormQuery
        if (
            operation
            not in {
                "config.public",
                "job.registered",
                "settings.group",
                "mail.describe",
                "settings.public",
                "settings.runtime",
                "settings.groups",
            }
            and type(query) is not schema
        ):
            raise _order_error()
        if operation.endswith(".list"):
            limit(
                type(query.current) is int
                and query.current >= 1
                and type(query.size) is int
                and 1 <= query.size <= 100
            )
        if operation in {
            "config.list",
            "config.export",
            "job.list",
            "config.form",
            "job.form",
        }:
            closed_object(query.model_dump(), max_bytes=8192)
        try:
            with bounded_authorization_reads(
                db,
                AuthorizationReadBudget(
                    max_statements=4096, phase_limits=RELATION_READ_PHASE_LIMITS
                ),
            ):
                async with authorization_transactions.scope(
                    db,
                    tenants=(TenantLock(tenant.tenant_id, LockMode.SHARED),),
                    budget=TransactionBudget(timeout_ms=8000),
                ) as tx:
                    if operation == "job.registered":
                        await load_parameter_context(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            action="job.registered",
                        )
                        if query is not None:
                            raise _order_error()
                        return builtin_task_contracts().tenant_descriptions()
                    if operation in {
                        "settings.public",
                        "settings.runtime",
                        "settings.groups",
                    }:
                        if query is not None:
                            raise _order_error()
                        return await read_fixed_settings(
                            db, tx, tenant, actor_user_version, operation
                        )
                    if operation == "settings.group":
                        if type(query) is not str:
                            raise _order_error()
                        return await read_group(
                            db, tx, tenant, actor_user_version, query
                        )
                    if operation == "mail.describe":
                        if query is not None:
                            raise _order_error()
                        await fixed_actor(
                            db,
                            tenant,
                            actor_user_version,
                            "system:setting:list",
                            system=True,
                        )
                        return await describe_mail(db)
                    if operation == "config.public":
                        if query is not None or actor_user_version is not None:
                            raise _order_error()
                        return await public_parameters(db, tx, tenant)
                    if operation.endswith(".form"):
                        return await self.form(
                            db,
                            tx,
                            tenant,
                            actor_user_version,
                            surface,
                            query,
                            model,
                            anchor,
                        )
                    context = await load_parameter_context(
                        db,
                        tx,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        action=operation,
                    )
                    table = model.__table__
                    dependencies = []
                    predicates = []
                    for name in (
                        ("config_name", "config_key", "config_group", "status")
                        if surface == "config"
                        else ("job_name", "job_key", "status")
                    ):
                        value = getattr(query, name, None)
                        if value is None or value == "":
                            continue
                        access = EQ if name == "status" else CONTAINS
                        dependencies.append((name, access))
                        predicates.append(
                            table.c[name] == value
                            if access == EQ
                            else table.c[name].contains(value, autoescape=True)
                        )
                    await context.prove(db, obj, table, dependencies)
                    if operation == "config.export":
                        return await self.export(db, context, predicates)
                    with authorization_read_phase(db, "projection"):
                        total = await db.scalar(
                            select(func.pg_catalog.count(1))
                            .select_from(table)
                            .where(context.base(obj, table), *predicates)
                        )
                    entries = await context.fetch(
                        db,
                        model,
                        anchor,
                        predicates=predicates,
                        offset=(query.current - 1) * query.size,
                        maximum=query.size,
                    )
                    return {
                        "fieldPolicy": ParameterFieldPolicy.model_validate(
                            context.capabilities(
                                context.record(obj, "page", context.scopes)
                            )
                        ).model_dump(by_alias=True),
                        "records": [
                            serialize_parameter(surface, values, policy)
                            for _, values, policy in entries
                        ],
                        "total": total,
                        "current": query.current,
                        "size": query.size,
                    }
        finally:
            await rollback_authorization_session(db)

    async def export(self, db, context, predicates):
        probe = context.record("system.config", "export", context.scopes)
        allowed = set(context.allowed(probe, "config.export", EXPORT)) & set(
            context.allowed(probe, "config.export", READ)
        )
        headers = tuple(
            f.field_id
            for f in context.catalog.objects["system.config"].fields
            if f.field_id in allowed
        )
        last = 0
        records = []
        while True:
            entries = await context.fetch(
                db,
                Config,
                "config_id",
                predicates=(*predicates, Config.config_id > last),
                maximum=256,
                access=EXPORT,
            )
            limit(len(records) + len(entries) <= 10000)
            for record, values, _ in entries:
                records.append(
                    {key: value for key, value in values.items() if key in headers}
                )
                context.source(record, tuple(values), EXPORT)
            if len(entries) < 256:
                return {
                    "headers": headers,
                    "records": records,
                    "sources": tuple(context.source_refs),
                }
            last = int(entries[-1][0].record_key)

    async def form(self, db, tx, tenant, version, surface, query, model, anchor):
        action = surface + (".create" if query.mode == "create" else ".list")
        context = await load_parameter_context(
            db, tx, tenant=tenant, actor_user_version=version, action=action
        )
        permission = (
            "system:" + surface + (":add" if query.mode == "create" else ":edit")
        )
        if permission not in context.permissions:
            raise AuthorizationException(
                "缺少操作权限", error_code="MISSING_PERMISSION"
            )
        target = int(query.record_id) if query.record_id else None
        record = None
        if target is not None:
            entries = await context.fetch(
                db,
                model,
                anchor,
                predicates=(model.__table__.c[anchor] == target,),
                maximum=1,
            )
            if not entries:
                raise NotFoundException(surface)
            source, values, policy = entries[0]
            record = serialize_parameter(surface, values, policy)
        else:
            source = context.record(
                "system.config" if surface == "config" else "job.definition",
                "new",
                context.scopes,
            )
            policy = context.capabilities(source)
        facts = await observed_form_facts(
            db, tx, context, mode=query.mode, target_id=target
        )
        result = {
            "record": record,
            "fieldPolicy": ParameterFieldPolicy.model_validate(policy).model_dump(
                by_alias=True
            ),
            "formReceipt": issue_receipt(facts),
        }
        if (
            surface == "config"
            and target is not None
            and "system:config:publish" in context.permissions
        ):
            with authorization_read_phase(db, "projection"):
                metadata = (
                    await parameter_rows(
                        db,
                        Config.__table__,
                        tenant.tenant_id,
                        predicates=(Config.config_id == target,),
                        names=("config_id", "is_public"),
                    )
                )[0]
                prior = (
                    (
                        await db.execute(
                            select(ConfigPublication.__table__).where(
                                ConfigPublication.tenant_id == tenant.tenant_id,
                                ConfigPublication.config_id == target,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            result["revocationRevision"] = publication_revision(
                facts,
                dict(metadata) | {"tenant_id": tenant.tenant_id},
                dict(prior) if prior else None,
                kind="revoke",
            )
        if (
            surface == "config"
            and target is not None
            and "system:config:publish" in context.permissions
            and "config_value" in values
            and values.get("config_type") == "text"
            and values.get("status") == "1"
            and all(
                {
                    use.ref.field_id
                    for use in context.catalog.actions["config.publish"].uses
                    if use.access == access
                }
                <= set(context.allowed(source, "config.publish", access))
                for access in (READ, UPDATE)
            )
        ):
            with authorization_read_phase(db, "projection"):
                rows = await parameter_rows(
                    db,
                    Config.__table__,
                    tenant.tenant_id,
                    predicates=(Config.config_id == target,),
                )
                publication = (
                    (
                        await db.execute(
                            select(ConfigPublication.__table__).where(
                                ConfigPublication.tenant_id == tenant.tenant_id,
                                ConfigPublication.config_id == target,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            current = next(row for row in rows if row["config_id"] == target)
            result["publicationRevision"] = publication_revision(
                facts, current, dict(publication) if publication else None
            )
        return result


parameter_read_owner = ParameterReadOwner()
