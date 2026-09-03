from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.core.base_response import PageResult
from app.core.tenant import normalize_tenant_code
from app.modules.platform.constants import PLATFORM_PRINCIPAL_NAME_RE
from app.schemas.types import LocalNaiveDatetime


class PlatformLoginCredentials(BaseModel):
    principal_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    @field_validator("principal_name")
    @classmethod
    def normalize_principal_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if PLATFORM_PRINCIPAL_NAME_RE.fullmatch(normalized) is None:
            raise ValueError("平台账号只能使用小写字母、数字、下划线和连字符")
        return normalized


class PlatformTokenResponse(BaseModel):
    token: str


class PlatformTenantCreate(BaseModel):
    tenant_code: str = Field(min_length=2, max_length=32)
    tenant_name: str = Field(min_length=1, max_length=100)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    @field_validator("tenant_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = normalize_tenant_code(value)
        if normalized is None or len(normalized) < 2 or normalized == "default":
            raise ValueError("租户代码必须是 2-32 位小写字母、数字或连字符")
        return normalized

    @field_validator("tenant_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            not character.isprintable() for character in normalized
        ):
            raise ValueError("租户名称不能为空或包含控制字符")
        return normalized


class PlatformTenantQuery(BaseModel):
    current: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class PlatformTenantOut(BaseModel):
    tenant_id: int
    tenant_code: str
    tenant_name: str
    enabled: bool
    lifecycle_state: str
    row_version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @classmethod
    def from_record(cls, tenant) -> "PlatformTenantOut":
        return cls(
            tenant_id=tenant.tenant_id,
            tenant_code=tenant.tenant_code,
            tenant_name=tenant.tenant_name,
            enabled=tenant.status == "1",
            lifecycle_state=tenant.lifecycle_state,
            row_version=tenant.row_version,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
        )

    @field_serializer("tenant_id")
    def serialize_tenant_id(self, value: int, _info) -> str:
        return str(value)

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime, _info) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PlatformTenantPage(PageResult[PlatformTenantOut]):
    pass


class PlatformSupportAuditQuery(BaseModel):
    current: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class PlatformSupportAuditOut(BaseModel):
    event_id: int
    category: str
    event_type: str
    outcome: str
    duration_ms: int | None
    occurred_at: datetime

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    @field_serializer("event_id")
    def serialize_event_id(self, value: int, _info) -> str:
        return str(value)


class PlatformSupportAuditPage(PageResult[PlatformSupportAuditOut]):
    pass


class PlatformRetentionPreviewRequest(BaseModel):
    cutoff: LocalNaiveDatetime

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class PlatformRetentionPurgeRequest(PlatformRetentionPreviewRequest):
    expected_operation_count: int = Field(ge=0)
    expected_login_count: int = Field(ge=0)


class PlatformRetentionOut(BaseModel):
    cutoff: datetime
    operation_count: int
    login_count: int
    affected_count: int

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @classmethod
    def from_result(cls, result) -> "PlatformRetentionOut":
        return cls(
            cutoff=result.cutoff,
            operation_count=result.operation_count,
            login_count=result.login_count,
            affected_count=result.affected_count,
        )
