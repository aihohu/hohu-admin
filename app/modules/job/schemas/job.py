from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.config import settings


class JobCreate(BaseModel):
    """定时任务创建请求"""

    job_name: str = Field(..., min_length=1, max_length=64, description="任务名称")
    job_key: str = Field(..., min_length=1, max_length=64, description="任务标识")
    cron_expression: str | None = Field(
        None, min_length=1, max_length=64, description="cron表达式"
    )
    trigger_type: str = Field("cron", description="调度类型：cron/interval")
    interval_value: int | None = Field(None, ge=1, description="间隔值")
    interval_unit: str | None = Field(
        None, description="间隔单位：seconds/minutes/hours/days"
    )
    job_args: str | None = Field(None, description="任务参数JSON")
    status: str = Field(STATUS_DISABLED, description="状态：1-启用，2-停用")
    concurrent: str = Field("2", description="并发策略：1-允许，2-不允许")
    timeout_seconds: int | None = Field(
        None, ge=1, description="单次执行超时秒数（空表示不限）"
    )
    max_retries: int = Field(0, ge=0, description="失败重试次数（0 表示不重试）")
    run_on_enable: bool = Field(
        False, description="启用时是否立即执行一次（不影响后续按计划触发）"
    )
    remark: str | None = Field(None, max_length=256, description="备注")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(停用)")
        return v

    @field_validator("concurrent")
    @classmethod
    def validate_concurrent(cls, v: str) -> str:
        if v not in ["1", "2"]:
            raise ValueError("并发策略必须是 1(允许) 或 2(不允许)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class JobUpdate(BaseModel):
    """定时任务更新请求"""

    job_id: int = Field(..., description="任务ID")
    job_name: str | None = Field(
        None, min_length=1, max_length=64, description="任务名称"
    )
    cron_expression: str | None = Field(
        None, min_length=1, max_length=64, description="cron表达式"
    )
    trigger_type: str | None = Field(None, description="调度类型：cron/interval")
    interval_value: int | None = Field(None, ge=1, description="间隔值")
    interval_unit: str | None = Field(None, description="间隔单位")
    job_args: str | None = Field(None, description="任务参数JSON")
    status: str | None = Field(None, description="状态")
    concurrent: str | None = Field(None, description="并发策略")
    timeout_seconds: int | None = Field(
        None, ge=1, description="单次执行超时秒数（空表示不限）"
    )
    max_retries: int | None = Field(None, ge=0, description="失败重试次数")
    run_on_enable: bool | None = Field(None, description="启用时是否立即执行一次")
    remark: str | None = Field(None, max_length=256, description="备注")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(停用)")
        return v

    @field_validator("concurrent")
    @classmethod
    def validate_concurrent(cls, v: str | None) -> str | None:
        if v is not None and v not in ["1", "2"]:
            raise ValueError("并发策略必须是 1(允许) 或 2(不允许)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class JobQuery(BaseModel):
    """定时任务查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    job_name: str | None = Field(None, description="任务名称（模糊查询）")
    job_key: str | None = Field(None, description="任务标识（模糊查询）")
    status: str | None = Field(None, description="状态")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(停用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class JobOut(BaseModel):
    """定时任务输出"""

    job_id: int
    job_name: str
    job_key: str
    cron_expression: str | None
    trigger_type: str
    interval_value: int | None
    interval_unit: str | None
    job_args: str | None
    status: str
    concurrent: str
    timeout_seconds: int | None
    max_retries: int
    run_on_enable: bool
    remark: str | None
    create_by: str | None
    create_time: datetime
    update_by: str | None
    update_time: datetime
    # 运行时计算字段，不落库；停用任务为 None
    next_run_time: datetime | None = None

    @field_serializer("job_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("update_time")
    def serialize_update_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("next_run_time")
    def serialize_next_run_time(self, dt: datetime | None) -> str | None:
        return dt.strftime(settings.DATETIME_FORMAT) if dt else None

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class JobLogQuery(BaseModel):
    """任务日志查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    job_id: int | None = Field(None, description="任务ID")
    job_key: str | None = Field(None, description="任务标识（模糊查询）")
    status: str | None = Field(None, description="状态：1-成功，2-失败，3-执行中")
    start_time: datetime | None = Field(None, description="开始时间（起）")
    end_time: datetime | None = Field(None, description="开始时间（止）")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class JobLogOut(BaseModel):
    """任务日志输出"""

    job_log_id: int
    job_id: int
    job_name: str
    job_key: str
    status: str
    error_msg: str | None
    start_time: datetime
    end_time: datetime | None
    duration: int | None
    attempt_count: int

    @field_serializer("job_log_id")
    def serialize_log_id(self, v: int, _info):
        return str(v)

    @field_serializer("job_id")
    def serialize_job_id(self, v: int, _info):
        return str(v)

    @field_serializer("start_time")
    def serialize_start_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("end_time")
    def serialize_end_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )
