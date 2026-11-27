"""Scheduler log facts are separate from task execution and never retry its IO."""

from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.db.authorization_plan_session import _order_error
from app.modules.job.dispatch import job_config_digest, verify_dispatch
from app.modules.job.execution_contract import prepare_job_execution
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.service.control_job_guard import require_job_run
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.parameter_context import load_parameter_context
from app.modules.system.service.parameter_facts import parameter_rows
from app.modules.system.service.parameter_receipt import observed_form_facts

_SEAL = object()


@dataclass(frozen=True, slots=True)
class JobRun:
    job: MappingProxyType
    log: MappingProxyType
    _seal: object

    def require(self):
        if self._seal is not _SEAL:
            raise _order_error()


class JobLifecycleCandidate:
    async def _orphans(self, db, tenant_id):
        logs = await parameter_rows(
            db, SysJobLog.__table__, tenant_id, predicates=(SysJobLog.status == "3",)
        )
        jobs = {
            row["job_id"]: dict(row)
            for row in await parameter_rows(
                db,
                SysJob.__table__,
                tenant_id,
                predicates=(SysJob.job_id.in_({row["job_id"] for row in logs}),),
                names=("job_id", "timeout_seconds"),
            )
        }
        return sorted(logs, key=lambda row: row["job_log_id"]), jobs

    async def reclaim(self, db, tx, *, tenant_id, runner_id):
        if type(runner_id) is not str or not 1 <= len(runner_id) <= 64:
            raise _order_error()
        logs, jobs = await self._orphans(db, tenant_id)
        await tx.lock(
            LockPlan(
                existing=(
                    ResourceKey(K.TENANT, None, tenant_id),
                    *(ResourceKey(K.JOB, tenant_id, identifier) for identifier in jobs),
                    *(
                        ResourceKey(K.JOB_LOG, tenant_id, row["job_log_id"])
                        for row in logs
                    ),
                )
            )
        )
        tenant = await db.get(Tenant, tenant_id, populate_existing=True)
        if tenant.status != "1":
            return 0
        if (logs, jobs) != await self._orphans(db, tenant_id):
            raise _order_error()
        now, count = datetime.now(), 0
        for row in logs:
            job = jobs.get(row["job_id"])
            if job is None:
                raise _order_error()
            grace = min(max(job["timeout_seconds"] or 1800, 1), 1800) * 2
            if (
                row["runner_id"] == runner_id
                or (now - row["start_time"]).total_seconds() < grace
            ):
                continue
            description = (
                "历史数据（迁移前遗留）"
                if row["runner_id"] is None
                else f"上一进程 runner_id={row['runner_id']}"
            )
            await tx.update_factual(
                ResourceKey(K.JOB_LOG, tenant_id, row["job_log_id"]),
                {
                    "status": "2",
                    "error_msg": f"执行进程未确认结果（{description}），疑似进程崩溃或重启，被守护协程标记失败",
                    "end_time": now,
                    "duration": 0,
                },
                expected=row,
            )
            count += 1
        return count

    async def _rows(self, db, tenant_id, job_id):
        job = (
            (
                await db.execute(
                    select(SysJob.__table__).where(
                        SysJob.tenant_id == tenant_id, SysJob.job_id == job_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        logs = await parameter_rows(
            db,
            SysJobLog.__table__,
            tenant_id,
            predicates=(SysJobLog.job_id == job_id, SysJobLog.status == "3"),
        )
        return dict(job) if job else None, sorted(
            (dict(row) for row in logs), key=lambda row: row["job_log_id"]
        )

    async def claim(self, db, tx, *, tenant_id, job_id, runner_id, dispatch=None):
        if type(runner_id) is not str or not 1 <= len(runner_id) <= 64:
            raise _order_error()
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            return None
        job, logs = await self._rows(db, tenant_id, job_id)
        if job is None:
            return None
        claims = verify_dispatch(dispatch) if dispatch is not None else None
        identity = None
        if claims is not None:
            if (claims.tenant_id, claims.job_id) != (tenant_id, job_id):
                raise _order_error()
            subject = TenantContext(
                tenant_id,
                tenant.tenant_code,
                claims.actor_id,
                tenant.row_version,
                "worker_envelope",
            )
            identity = await read_identity(
                db,
                tenant=subject,
                actor_user_version=claims.actor_version,
                permissions=("system:job:run",),
            )
            context = await load_parameter_context(
                db,
                tx,
                tenant=subject,
                actor_user_version=claims.actor_version,
                action="job.run",
            )
            facts = await observed_form_facts(
                db, tx, context, mode="update", target_id=job_id
            )
            if (
                claims.g_epoch,
                claims.b_epoch,
                claims.catalog_digest,
                claims.job_digest,
            ) != (
                facts.g_epoch,
                facts.b_epoch,
                facts.catalog_digest,
                job_config_digest(job),
            ):
                raise BusinessException(
                    409, "Dispatch facts have changed", error_code="JOB_DISPATCH_STALE"
                )
            require_job_run(context, job)
            if (
                await db.scalar(
                    select(SysJobLog.job_log_id)
                    .where(
                        SysJobLog.tenant_id == tenant_id,
                        SysJobLog.dispatch_id == claims.dispatch_id,
                    )
                    .limit(1)
                )
                is not None
            ):
                return None
        else:
            if (
                tenant.status != "1"
                or tenant.lifecycle_state != "active"
                or job["status"] != "1"
            ):
                return None
            try:
                prepare_job_execution(job)
            except BusinessException:
                return None
        new = ResourceKey(K.JOB_LOG, tenant_id, next_id())
        await tx.lock(
            LockPlan(
                existing=tuple(
                    {
                        *(identity.keys if identity is not None else ()),
                        ResourceKey(K.TENANT, None, tenant_id),
                        ResourceKey(K.JOB, tenant_id, job_id),
                        *(
                            ResourceKey(K.JOB_LOG, tenant_id, row["job_log_id"])
                            for row in logs
                        ),
                    }
                ),
                new=(NewResource(new),),
            )
        )
        tenant = await db.get(Tenant, tenant_id, populate_existing=True)
        if identity is not None:
            await assert_identity(db, identity)
            verify_dispatch(dispatch)
        if (job, logs) != await self._rows(db, tenant_id, job_id):
            raise _order_error()
        if (
            tenant.status != "1"
            or tenant.lifecycle_state != "active"
            or (claims is None and job["status"] != "1")
            or (job["concurrent"] == "2" and logs)
        ):
            return None
        values = {
            "job_id": job_id,
            "job_name": job["job_name"],
            "job_key": job["job_key"],
            "runner_id": runner_id,
            "start_time": datetime.now(),
            "status": "3",
            "attempt_count": 1,
        }
        if claims is not None:
            values.update(
                dispatch_id=claims.dispatch_id,
                dispatch_expires_at=datetime.fromtimestamp(claims.expires_at, UTC),
            )
        await tx.insert_reserved(new, values)
        return JobRun(
            MappingProxyType(job),
            MappingProxyType(
                values | {"tenant_id": tenant_id, "job_log_id": new.identity[0]}
            ),
            _SEAL,
        )

    async def finish(self, db, tx, *, tenant_id, claim, status, error_msg, attempts):
        if (
            type(claim) is not JobRun
            or status not in {"1", "2"}
            or type(attempts) is not int
            or attempts < 1
            or (
                error_msg is not None
                and (type(error_msg) is not str or len(error_msg) > 65536)
            )
        ):
            raise _order_error()
        claim.require()
        if claim.log["tenant_id"] != tenant_id:
            raise _order_error()
        job_id, log_id = claim.job["job_id"], claim.log["job_log_id"]
        row = (
            (
                await db.execute(
                    select(SysJobLog.__table__).where(
                        SysJobLog.tenant_id == tenant_id, SysJobLog.job_log_id == log_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        await tx.lock(
            LockPlan(
                existing=(
                    ResourceKey(K.TENANT, None, tenant_id),
                    ResourceKey(K.JOB, tenant_id, job_id),
                    ResourceKey(K.JOB_LOG, tenant_id, log_id),
                )
            )
        )
        current = (
            (
                await db.execute(
                    select(SysJobLog.__table__).where(
                        SysJobLog.tenant_id == tenant_id, SysJobLog.job_log_id == log_id
                    )
                )
            )
            .mappings()
            .one()
        )
        if any(
            current[name] != claim.log[name]
            for name in ("tenant_id", "job_id", "job_key", "runner_id", "start_time")
        ):
            raise _order_error()
        if current["status"] != "3":
            return False
        now = datetime.now()
        await tx.update_factual(
            ResourceKey(K.JOB_LOG, tenant_id, log_id),
            {
                "status": status,
                "error_msg": error_msg,
                "attempt_count": attempts,
                "end_time": now,
                "duration": max(
                    0, int((now - current["start_time"]).total_seconds() * 1000)
                ),
            },
            expected=dict(current),
        )
        return True


job_lifecycle_candidate = JobLifecycleCandidate()
