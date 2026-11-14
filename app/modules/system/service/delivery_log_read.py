"""Bounded scalar log projections. Historic raw text remains stored, undisclosed."""

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from sqlalchemy import and_, func, select

from app.core.field_policy.contracts import denied
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.schemas.job import JobLogQuery
from app.modules.system.field_policy.delivery_catalog import (
    CONTAINS,
    EQ,
    RANGE,
    SORT,
)
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User
from app.modules.system.schemas.delivery_projection import (
    ProjectedJobLog,
    ProjectedLoginLog,
    ProjectedOperationLog,
)
from app.modules.system.schemas.login_log import LoginLogQuery
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.modules.system.service.delivery_read_context import load_delivery_context


def masked_ip(value):
    if value is None:
        return None
    try:
        address = ip_address(value)
        return str(
            ip_network(f"{address}/{24 if address.version == 4 else 48}", strict=False)
        )
    except (ValueError, TypeError):
        return None


def safe_path(value):
    from app.main import app  # noqa: PLC0415

    if type(value) is not str or len(value) > 200:
        return None
    try:
        path = urlsplit(value).path
    except ValueError:
        return None
    for route in app.routes:
        pattern = getattr(route, "path_regex", None)
        if pattern is not None and pattern.fullmatch(path):
            return route.path
    return None


async def log_page(db, tx, *, tenant, version, kind, query):
    definitions = {
        "operation": (
            SysOperationLog,
            "system.operation_log",
            "operation_log_id",
            "operation-list",
            OperationLogQuery,
            ProjectedOperationLog,
            "create_time",
        ),
        "login": (
            SysLoginLog,
            "system.login_log",
            "login_log_id",
            "login-list",
            LoginLogQuery,
            ProjectedLoginLog,
            "login_time",
        ),
        "job": (
            SysJobLog,
            "job.log",
            "job_log_id",
            "job-list",
            JobLogQuery,
            ProjectedJobLog,
            "start_time",
        ),
    }
    if kind not in definitions:
        raise denied()
    model, obj, anchor, action, query_type, schema, time_name = definitions[kind]
    tx.assert_planning(tenant.tenant_id, exclusive=True)
    if type(query) is not query_type:
        raise denied()
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="delivery." + action
    )
    table = model.__table__
    predicates, dependencies = [], [(time_name, SORT)]
    for key in (
        "module",
        "action",
        "username",
        "ip",
        "job_key",
        "job_id",
        "status_code",
        "status",
    ):
        value = getattr(query, key, None)
        if value is None or value == "":
            continue
        access = (
            CONTAINS if key in {"module", "action", "username", "ip", "job_key"} else EQ
        )
        dependencies.append((key, access))
        if key == "username":
            users = User.__table__
            visible = and_(
                context.base("system.user", users),
                users.c.user_id == table.c.user_id,
                context.coverage("system.user", users, "user_name", CONTAINS),
            )
            missing = await db.scalar(
                select(
                    select(table.c[anchor])
                    .where(
                        context.base(obj, table),
                        ~select(users.c.user_id)
                        .where(visible)
                        .correlate(table)
                        .exists(),
                    )
                    .exists()
                )
            )
            if missing:
                raise denied()
            predicates.append(
                select(users.c.user_id)
                .where(visible, users.c.user_name.contains(value, autoescape=True))
                .correlate(table)
                .exists()
            )
        elif key == "job_key" and kind == "job":
            jobs = SysJob.__table__
            predicates.append(
                select(jobs.c.job_id)
                .where(
                    jobs.c.tenant_id == tenant.tenant_id,
                    jobs.c.job_id == table.c.job_id,
                    jobs.c.job_key.contains(value, autoescape=True),
                )
                .correlate(table)
                .exists()
            )
        else:
            column = table.c[key]
            predicates.append(
                column.contains(value, autoescape=True)
                if access == CONTAINS
                else column == value
            )
    for key, operation in (("start_time", "ge"), ("end_time", "le")):
        value = getattr(query, key, None)
        if value is not None:
            dependencies.append((time_name, RANGE))
            predicates.append(
                table.c[time_name] >= value
                if operation == "ge"
                else table.c[time_name] <= value
            )
    await context.prove(db, obj, table, tuple(dict.fromkeys(dependencies)))
    if kind == "job" and getattr(query, "job_key", None):
        jobs = SysJob.__table__
        visible = and_(
            jobs.c.tenant_id == tenant.tenant_id,
            jobs.c.job_id == table.c.job_id,
            context.coverage("job.definition", jobs, "job_key"),
            context.coverage("job.definition", jobs, "job_key", CONTAINS),
        )
        if await db.scalar(
            select(
                select(table.c[anchor])
                .where(
                    context.base(obj, table),
                    ~select(jobs.c.job_id).where(visible).correlate(table).exists(),
                )
                .exists()
            )
        ):
            raise denied()
    count = await db.scalar(
        select(func.pg_catalog.count(1))
        .select_from(table)
        .where(context.base(obj, table), *predicates)
    )
    rows = await context.rows(
        db,
        obj,
        model,
        anchor,
        predicates=tuple(predicates),
        order=(table.c[time_name].desc(), table.c[anchor].desc()),
        offset=(query.current - 1) * query.size,
        maximum=query.size,
        internals=("user_id",)
        if kind in {"operation", "login"}
        else ("job_id",)
        if kind == "job"
        else (),
    )
    names = (
        await context.user_names(db, (extra["user_id"] for _, _, extra in rows))
        if kind in {"operation", "login"}
        else {}
    )
    output = []
    for record, values, extra in rows:
        values[anchor] = str(values[anchor])
        if "ip" in values:
            values["ip"] = masked_ip(values["ip"])
        if "path" in values:
            path = safe_path(values["path"])
            if path is None:
                values.pop("path")
            else:
                values["path"] = path
        if kind in {"operation", "login"}:
            if (
                context.decisions(record)["username"].allowed
                and extra["user_id"] in names
            ):
                values["username"] = names[extra["user_id"]]
            else:
                values.pop("username", None)
        if kind == "job":
            identifier = extra["job_id"]
            jobs = (
                await context.rows(
                    db,
                    "job.definition",
                    SysJob,
                    "job_id",
                    names={"job_name", "job_key"},
                    predicates=(SysJob.job_id == identifier,),
                    maximum=1,
                )
                if identifier is not None
                else []
            )
            for name in ("job_name", "job_key"):
                if name in values and jobs and name in jobs[0][1]:
                    values[name] = jobs[0][1][name]
                else:
                    values.pop(name, None)
            if "job_id" in values and values["job_id"] is not None:
                values["job_id"] = str(values["job_id"])
        values["field_policy"] = context.policy(record)
        output.append(
            schema.model_validate(values).model_dump(mode="json", by_alias=True)
        )
    policy = context.policy(context.record(obj, "query", context.scopes))
    return {
        "records": output,
        "total": count or 0,
        "current": query.current,
        "size": query.size,
        "fieldPolicy": policy,
    }
