"""Closed ordinary metadata and explicit schedule/status commands."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.modules.job.schemas.job import JobCreate


class ControlJobCreate(JobCreate):
    model_config = ConfigDict(extra="forbid", strict=True)


class JobCreateForm(ControlJobCreate):
    form_receipt: str | None = Field(None, max_length=2048, repr=False)


class JobTarget(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, alias_generator=to_camel, populate_by_name=True
    )
    job_id: int = Field(gt=0, lt=2**63)

    @field_validator("job_id", mode="before", json_schema_input_type=int | str)
    @classmethod
    def canonical_identifier(cls, value):
        if type(value) is str:
            if (
                not value.isascii()
                or not value.isdecimal()
                or not 1 <= len(value) <= 19
                or value.startswith("0")
            ):
                raise ValueError("Invalid job identifier")
            return int(value)
        return value


class JobMetadataUpdate(JobTarget):
    job_name: str | None = Field(None, min_length=1, max_length=64)
    remark: str | None = Field(None, max_length=256)

    @model_validator(mode="after")
    def sparse(self):
        fields = self.model_fields_set - {"job_id", "form_receipt"}
        if not fields or "job_name" in fields and self.job_name is None:
            raise ValueError("Invalid metadata update")
        return self


class JobStatusWrite(JobTarget):
    status: str = Field(pattern="^[12]$")


class JobMetadataForm(JobMetadataUpdate):
    form_receipt: str | None = Field(None, max_length=2048, repr=False)


class JobScheduleWrite(JobTarget):
    trigger_type: str | None = Field(None, pattern="^(cron|interval)$")
    cron_expression: str | None = Field(None, min_length=1, max_length=64)
    interval_value: int | None = Field(None, ge=1)
    interval_unit: str | None = Field(None, pattern="^(seconds|minutes|hours|days)$")
    job_args: str | None = None
    concurrent: str | None = Field(None, pattern="^[12]$")
    timeout_seconds: int | None = Field(None, ge=1, le=60)
    max_retries: int | None = Field(None, ge=0, le=3)
    run_on_enable: bool | None = None

    @model_validator(mode="after")
    def sparse(self):
        values = self.model_dump(exclude_unset=True, exclude={"job_id"})
        if not values or any(
            value is None
            and name in {"trigger_type", "concurrent", "max_retries", "run_on_enable"}
            for name, value in values.items()
        ):
            raise ValueError("Invalid schedule update")
        return self
