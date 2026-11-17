"""Fixed job commands discover every parent and child before taking row locks."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.core.scheduler import validate_trigger_config
from app.db.authorization_plan_session import _order_error
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.service.control_job_guard import (
    authorize_job_command,
    issue_job_dispatch,
)
from app.modules.job.service.control_job_input import (
    normalize_definition,
    prepare_control_job,
)
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.parameter_facts import parameter_rows

PERMISSIONS = {
    "create": "system:job:add",
    "update": "system:job:edit",
    "status": "system:job:edit",
    "schedule": "system:job:edit",
    "delete": "system:job:delete",
    "batch-delete": "system:job:batch-delete",
    "run": "system:job:run",
    "log-delete": "system:job-log:batch-delete",
    "log-clean": "system:job-log:clean",
}


@dataclass(frozen=True)
class JobCommand:
    operation: str
    identifiers: tuple
    values: dict
    new_id: int | None = None
    cutoff: datetime | None = None


def prepare(operation, *, payload=None, ids=(), days=None):
    if operation not in PERMISSIONS or type(ids) not in {list, tuple}:
        raise _order_error()
    if len(ids) > 256 or any(
        type(item) is not int or not 0 < item < 2**63 for item in ids
    ):
        raise _order_error()
    identifiers = tuple(sorted(set(ids)))
    values, new_id, cutoff = {}, None, None
    if operation == "create":
        values = prepare_control_job(operation, payload)
        new_id = next_id()
    elif operation in {"update", "status", "schedule"}:
        values = prepare_control_job(operation, payload)
        identifiers = (values.pop("job_id"),)
        if operation == "status" and set(values) != {"status"}:
            raise _order_error()
    elif payload is not None:
        raise _order_error()
    if operation in {"delete", "run"} and len(identifiers) != 1:
        raise _order_error()
    if operation == "log-clean":
        if type(days) is not int or not 1 <= days <= 36500:
            raise _order_error()
        cutoff = datetime.now() - timedelta(days=days)
    args = values.get("job_args")
    if args is not None and len(args.encode("utf-8")) > 8192:
        raise BusinessRuleException(
            "任务参数超出大小限制", error_code="JOB_INPUT_TOO_LARGE"
        )
    return JobCommand(operation, identifiers, values, new_id, cutoff)


class JobHttpCandidate:
    async def discover(self, db, tenant, command):
        jobs, logs = [], []
        jt, lt = SysJob.__table__, SysJobLog.__table__
        operation, identifiers = command.operation, command.identifiers
        if operation.startswith("log-"):
            predicate = (
                and_(
                    lt.c.start_time < command.cutoff,
                    lt.c.status.in_(("1", "2")),
                    or_(
                        lt.c.dispatch_expires_at.is_(None),
                        lt.c.dispatch_expires_at <= datetime.now(UTC),
                    ),
                )
                if operation == "log-clean"
                else lt.c.job_log_id.in_(identifiers)
            )
            logs = await parameter_rows(
                db, lt, tenant.tenant_id, predicates=(predicate,)
            )
            if operation == "log-delete" and {row["job_log_id"] for row in logs} != set(
                identifiers
            ):
                raise NotFoundException("任务日志")
            identifiers = tuple({row["job_id"] for row in logs})
        predicate = (
            jt.c.job_key == command.values["job_key"]
            if operation == "create"
            else jt.c.job_id.in_(identifiers)
        )
        jobs = await parameter_rows(db, jt, tenant.tenant_id, predicates=(predicate,))
        if operation == "create" and jobs:
            raise DuplicateException("任务标识", command.values["job_key"])
        if operation != "create" and {row["job_id"] for row in jobs} != set(
            identifiers
        ):
            raise NotFoundException("定时任务")
        if operation in {"delete", "batch-delete"}:
            if any(row["status"] == "1" for row in jobs):
                raise BusinessRuleException("请先停用任务再删除")
            logs = await parameter_rows(
                db, lt, tenant.tenant_id, predicates=(lt.c.job_id.in_(identifiers),)
            )
        if len(logs) > 10000 or len(jobs) > 10000:
            raise BusinessRuleException(
                "任务操作超过事务预算", error_code="JOB_BATCH_TOO_LARGE"
            )
        return sorted(jobs, key=lambda row: row["job_id"]), sorted(
            logs, key=lambda row: row["job_log_id"]
        )

    async def execute(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        command,
        http_protocol=False,
        protocol_receipt=None,
    ):
        if type(command) is not JobCommand:
            raise _order_error()
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=(PERMISSIONS[command.operation],),
        )
        jobs, logs = await self.discover(db, tenant, command)
        context = await authorize_job_command(
            db,
            tx,
            tenant,
            actor_user_version,
            command,
            jobs,
            http_protocol=http_protocol,
            protocol_receipt=protocol_receipt,
        )
        new = (
            ResourceKey(K.JOB, tenant.tenant_id, command.new_id)
            if command.new_id
            else None
        )
        await tx.lock(
            LockPlan(
                existing=tuple(
                    set(identity.keys)
                    | {
                        ResourceKey(K.JOB, tenant.tenant_id, row["job_id"])
                        for row in jobs
                    }
                    | {
                        ResourceKey(K.JOB_LOG, tenant.tenant_id, row["job_log_id"])
                        for row in logs
                    }
                ),
                new=(NewResource(new),) if new else (),
            )
        )
        await assert_identity(db, identity)
        if (jobs, logs) != await self.discover(db, tenant, command):
            raise _order_error()
        operation = command.operation
        if operation == "create":
            values = command.values | {
                "create_by": identity.user_name,
                "update_by": identity.user_name,
            }
            values = normalize_definition(values)
            await tx.insert_reserved(new, values)
            result = {"job_id": str(command.new_id)}
            if values["status"] == "1" and values["run_on_enable"]:
                result["dispatch"] = await issue_job_dispatch(
                    db,
                    tx,
                    context,
                    values | {"job_id": command.new_id, "tenant_id": tenant.tenant_id},
                )
            return result
        if operation in {"update", "status", "schedule"}:
            job = jobs[0]
            changes = command.values | (
                {"update_by": identity.user_name} if operation == "update" else {}
            )
            if (
                operation == "schedule"
                or operation == "status"
                and changes["status"] == "1"
            ):
                effective = normalize_definition(job | changes)
                changes = {
                    name: effective[name]
                    if name in {"job_args", "timeout_seconds", "max_retries"}
                    else value
                    for name, value in changes.items()
                }
            if changes:
                await tx.update_factual(
                    ResourceKey(K.JOB, tenant.tenant_id, job["job_id"]),
                    changes,
                    expected=job,
                )
            result = {"job_id": str(job["job_id"])}
            if (
                operation == "status"
                and job["status"] != "1"
                and changes["status"] == "1"
                and job["run_on_enable"]
            ):
                result["dispatch"] = await issue_job_dispatch(
                    db, tx, context, job | changes
                )
            return result
        if operation == "run":
            return {
                "job_id": str(jobs[0]["job_id"]),
                "dispatch": await issue_job_dispatch(db, tx, context, jobs[0]),
            }
        for row in logs:
            await tx.delete_job_fact(
                ResourceKey(K.JOB_LOG, tenant.tenant_id, row["job_log_id"]),
                expected=row,
            )
        if operation in {"delete", "batch-delete"}:
            for row in jobs:
                await tx.delete_job_fact(
                    ResourceKey(K.JOB, tenant.tenant_id, row["job_id"]), expected=row
                )
            return len(jobs)
        return len(logs)

    @staticmethod
    def _validate(values):
        try:
            validate_trigger_config(
                values["trigger_type"],
                values["cron_expression"],
                values["interval_value"],
                values["interval_unit"],
            )
        except ValueError as error:
            raise BusinessRuleException(str(error)) from error


job_http_candidate = JobHttpCandidate()
