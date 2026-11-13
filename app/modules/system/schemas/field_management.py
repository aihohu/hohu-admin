"""Closed management DTOs. BIGINT versions are canonical decimal strings on wire."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel

from app.core.exceptions import BusinessException

Decimal = Annotated[
    str, StringConstraints(strict=True, pattern=r"^(0|[1-9][0-9]{0,18})$")
]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, alias_generator=to_camel)


class AccessInput(Closed):
    operation: str = Field(max_length=32)
    purpose: str = Field(max_length=96)
    use: str = Field(max_length=32)


class RuleInput(Closed):
    field_id: str = Field(max_length=96)
    access: AccessInput
    effect: Literal["allow", "deny"]
    level: Literal["masked", "plain"]
    mask_id: str | None = Field(max_length=32)


class ManifestDraft(Closed):
    expected_version: Decimal
    catalog_digest: Digest

    @field_validator("expected_version")
    @classmethod
    def bounded_version(cls, value):
        if not 1 <= int(value) < 2**63 - 1:
            raise ValueError("Version out of range")
        return value


class ObjectDraft(ManifestDraft):
    expected_binding_version: Decimal
    rules: list[RuleInput] = Field(max_length=2048)

    @field_validator("expected_binding_version")
    @classmethod
    def bounded_binding(cls, value):
        if int(value) >= 2**63 - 1:
            raise ValueError("Version out of range")
        return value


class Mutation(Closed):
    request_id: str = Field(min_length=36, max_length=36)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("request_id")
    @classmethod
    def canonical_request(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("Invalid request identity")
        return value

    @field_validator("reason")
    @classmethod
    def printable_reason(cls, value):
        if not value.strip() or not value.isprintable():
            raise ValueError("Invalid reason")
        return value


class Publish(ObjectDraft, Mutation):
    preview_digest: str = Field(min_length=1, max_length=512)


class Build(ManifestDraft, Mutation):
    preview_digest: str = Field(min_length=1, max_length=512)


class Freeze(Mutation):
    expected_version: Decimal
    expected_epoch: Decimal
    object_id: str = Field(min_length=1, max_length=96)

    @field_validator("expected_version", "expected_epoch")
    @classmethod
    def bounded_transition(cls, value):
        return ManifestDraft.bounded_version(value)


class Receipt(Closed):
    request_id: str = Field(min_length=36, max_length=36)

    @field_validator("request_id")
    @classmethod
    def valid_request(cls, value):
        return Mutation.canonical_request(value)


class Page(Closed):
    cursor: Decimal = "0"
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("cursor")
    @classmethod
    def bounded_cursor(cls, value):
        if int(value) >= 2**63:
            raise ValueError("Cursor out of range")
        return value


def parse_management(operation, value):
    schema = {
        "catalog": Page,
        "roles": Page,
        "object": Closed,
        "manifest": Closed,
        "preview": ObjectDraft,
        "publish": Publish,
        "manifest-preview": ManifestDraft,
        "build": Build,
        "freeze": Freeze,
        "receipt": Receipt,
    }.get(operation)
    if schema is None:
        raise BusinessException(
            422, "不支持的策略操作", error_code="FIELD_POLICY_INVALID"
        )
    try:
        return schema.model_validate(value).model_dump()
    except (ValidationError, ValueError):
        raise BusinessException(
            422, "字段策略请求无效", error_code="FIELD_POLICY_INVALID"
        ) from None
