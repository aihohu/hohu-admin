"""Bounded task preparation only; a successful claim is separate authority."""

from collections.abc import Mapping

from app.core.exceptions import BusinessRuleException
from app.modules.job.builtin_task_specs import builtin_task_contracts
from app.modules.job.task_contracts import prepare_task_args
from app.modules.job.task_registry import TaskScope
from app.utils.closed_json import closed_object


def prepare_job_execution(job):
    if not isinstance(job, Mapping):
        raise BusinessRuleException("Invalid job input", error_code="JOB_INPUT_INVALID")
    spec = builtin_task_contracts().resolve(job.get("job_key"), scope=TaskScope.TENANT)
    prepared = prepare_task_args(spec, job.get("job_args"))
    _, canonical = closed_object(spec.call_arguments(prepared)[0], max_bytes=8192)
    timeout = job.get("timeout_seconds")
    timeout = spec.timeout_seconds if timeout is None else timeout
    retries = job.get("max_retries", 0)
    if (
        type(timeout) is not int
        or not 1 <= timeout <= spec.timeout_seconds
        or type(retries) is not int
        or not 0 <= retries <= spec.max_retries
    ):
        raise BusinessRuleException(
            "Job exceeds its TaskSpec execution budget", error_code="JOB_INPUT_INVALID"
        )
    return (
        spec,
        prepared,
        dict(job)
        | {
            "job_args": canonical.decode("utf-8"),
            "timeout_seconds": timeout,
            "max_retries": retries,
        },
    )
