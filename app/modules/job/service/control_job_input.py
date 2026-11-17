"""Bounded preparation, never task execution authority."""

from pydantic import BaseModel, ValidationError

from app.core.exceptions import BusinessRuleException
from app.core.scheduler import validate_trigger_config
from app.modules.job.execution_contract import prepare_job_execution
from app.modules.job.schemas.control_job import (
    ControlJobCreate,
    JobMetadataUpdate,
    JobScheduleWrite,
    JobStatusWrite,
)


def normalize_definition(values):
    _, _, values = prepare_job_execution(values)
    try:
        validate_trigger_config(
            values["trigger_type"],
            values["cron_expression"],
            values["interval_value"],
            values["interval_unit"],
        )
    except (KeyError, TypeError, ValueError):
        raise BusinessRuleException(
            "Job schedule rejected", error_code="JOB_INPUT_INVALID"
        ) from None
    return values


def prepare_control_job(operation, payload):
    schema = {
        "create": ControlJobCreate,
        "update": JobMetadataUpdate,
        "schedule": JobScheduleWrite,
        "status": JobStatusWrite,
    }[operation]
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(exclude_unset=operation != "create")
    try:
        model = schema.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise BusinessRuleException(
            "Use the specific job command and registered fields",
            error_code="JOB_INPUT_INVALID",
        ) from None
    values = model.model_dump(exclude_unset=operation != "create")
    return normalize_definition(values) if operation == "create" else values
