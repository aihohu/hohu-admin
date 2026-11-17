"""Current field authority never comes from creator roles or a signed notice."""

from app.core.exceptions import AuthorizationException
from app.core.field_policy import resolve_fields
from app.core.field_policy.contracts import denied
from app.modules.job.dispatch import issue_dispatch, job_config_digest
from app.modules.job.execution_contract import prepare_job_execution
from app.modules.system.field_policy.control_catalog import CREATE, READ, UPDATE
from app.modules.system.service.parameter_context import load_parameter_context
from app.modules.system.service.parameter_receipt import (
    observed_form_facts,
    require_receipt,
)


def require_job_run(context, job):
    if "system:job:run" not in context.permissions:
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    record = context.record("job.definition", job["job_id"], context.scopes)
    decisions = resolve_fields(context.catalog, context.rules, record, "job.run", READ)
    if any(
        not decisions[name].allowed
        for name in ("job_id", "job_key", "job_args", "status")
    ):
        raise denied()
    prepare_job_execution(job)


async def authorize_job_command(
    db,
    tx,
    tenant,
    version,
    command,
    jobs,
    *,
    http_protocol=False,
    protocol_receipt=None,
):
    operation = command.operation
    if operation.startswith("log-"):
        return None
    context = await load_parameter_context(
        db, tx, tenant=tenant, actor_user_version=version, action="job." + operation
    )
    identifier = (
        command.new_id
        if operation == "create"
        else command.identifiers[0]
        if len(command.identifiers) == 1
        else "batch"
    )
    if http_protocol and operation in {"create", "update"}:
        facts = await observed_form_facts(
            db,
            tx,
            context,
            mode=operation,
            target_id=None if operation == "create" else identifier,
        )
        require_receipt(protocol_receipt, facts)
    if operation in {"create", "update", "status", "schedule"}:
        context.write(
            "job.definition",
            identifier,
            "job." + operation,
            CREATE if operation == "create" else UPDATE,
            tuple(command.values),
        )
        current = (
            command.values | {"job_id": identifier}
            if operation == "create"
            else jobs[0] | command.values
        )
        immediate = (
            current["status"] == "1"
            and current["run_on_enable"]
            and (
                operation == "create"
                or operation == "status"
                and jobs[0]["status"] != "1"
            )
        )
        if immediate:
            require_job_run(context, current)
    if operation == "run":
        require_job_run(context, jobs[0])
    return context


async def issue_job_dispatch(db, tx, context, job):
    require_job_run(context, job)
    facts = await observed_form_facts(
        db, tx, context, mode="update", target_id=job["job_id"]
    )
    return issue_dispatch(
        tenant_id=facts.tenant_id,
        job_id=job["job_id"],
        actor_id=facts.actor_id,
        actor_version=facts.actor_version,
        g_epoch=facts.g_epoch,
        b_epoch=facts.b_epoch,
        job_digest=job_config_digest(job),
        catalog_digest=facts.catalog_digest,
    )
