"""Dormant value ceilings and fingerprints, never field or publication grants.

The future owner must supply observed DB classification/publication facts under
its live barrier and independently authorize the operation and every field.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import BusinessRuleException
from app.utils.closed_json import closed_object

MASKED_VALUE = "******"
PUBLIC_TEXT_SCHEMA = "custom-parameter/text/1"
_ABSENT = object()
_SECRET_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "credential",
    "authorization",
    "cookie",
    "headers",
)


def _invalid(code="CONFIG_VALUE_SCHEMA_UNREGISTERED"):
    return BusinessRuleException(
        "Configuration value contract rejected", error_code=code
    )


class ValueSensitivity(StrEnum):
    UNKNOWN = "unknown"
    PRIVATE = "private"
    SECRET = "secret"


def _sensitivity(value):
    if type(value) not in (str, ValueSensitivity):
        raise _invalid()
    try:
        return ValueSensitivity(value)
    except ValueError:
        raise _invalid() from None


def _key(key):
    if type(key) is not str or not key.strip() or len(key) > 100 or "\x00" in key:
        raise _invalid()
    try:
        key.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None
    normalized = "".join(char for char in key.lower() if char.isalnum())
    return any(part in normalized for part in _SECRET_PARTS)


def _atomic(value, config_type):
    if (
        type(config_type) is not str
        or config_type not in {"text", "richtext"}
        or type(value) is not str
    ):
        raise _invalid()
    if len(value) > 8192:
        raise _invalid()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise _invalid() from None
    if (
        size > 8192
        or "\x00" in value
        or value.lstrip(" \t\r\n\ufeff").startswith(("{", "["))
    ):
        raise _invalid()
    return value


@dataclass(frozen=True, slots=True)
class ValueChange:
    sensitivity: ValueSensitivity
    write_value: bool = False
    value: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ValueProjection:
    visible: bool = False
    value: str | None = field(default=None, repr=False)


def prepare_value(*, key, config_type, previous="unknown", replacement=_ABSENT):
    """Only an explicit validated replacement may classify an unknown old value."""
    old, secret_key = _sensitivity(previous), _key(key)
    if replacement is _ABSENT or (
        type(replacement) is str and replacement == MASKED_VALUE
    ):
        return ValueChange(ValueSensitivity.SECRET if secret_key else old)
    value = _atomic(replacement, config_type)
    sensitivity = (
        ValueSensitivity.SECRET
        if secret_key or old == ValueSensitivity.SECRET
        else ValueSensitivity.PRIVATE
    )
    return ValueChange(sensitivity, True, value)


def project_value(*, key, value, config_type, sensitivity):
    """A visible result is a ceiling only; callers still need current read grants."""
    try:
        if _sensitivity(sensitivity) != ValueSensitivity.PRIVATE or _key(key):
            return ValueProjection()
        return ValueProjection(True, _atomic(value, config_type))
    except BusinessRuleException:
        return ValueProjection()


class ConfigContent(BaseModel):
    """Internal observed content, not an HTTP request or response schema."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tenant_id: int = Field(ge=0, lt=2**63)
    config_id: int = Field(gt=0, lt=2**63)
    config_key: str = Field(min_length=1, max_length=100)
    config_value: str = Field(repr=False)
    config_type: str
    status: str
    is_public: bool
    value_sensitivity: str


class PublicationFact(BaseModel):
    """Only a trusted owner can establish the provenance of this persisted fact."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tenant_id: int = Field(ge=0, lt=2**63)
    config_id: int = Field(gt=0, lt=2**63)
    schema_revision: str = Field(max_length=64)
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def publication_digest(content):
    if type(content) is not ConfigContent:
        raise _invalid("CONFIG_PUBLICATION_UNSAFE")
    try:
        content = ConfigContent.model_validate(content.model_dump())
        ceiling = project_value(
            key=content.config_key,
            value=content.config_value,
            config_type=content.config_type,
            sensitivity=content.value_sensitivity,
        )
        if (
            not ceiling.visible
            or content.config_type != "text"
            or content.status != "1"
        ):
            raise _invalid("CONFIG_PUBLICATION_UNSAFE")
        _, encoded = closed_object(
            {
                "tenant_id": content.tenant_id,
                "config_id": content.config_id,
                "config_key": content.config_key,
                "config_value": content.config_value,
                "config_type": content.config_type,
                "status": content.status,
                "value_sensitivity": content.value_sensitivity,
                "schema_revision": PUBLIC_TEXT_SCHEMA,
            },
            max_bytes=16384,
        )
    except ValidationError:
        raise _invalid("CONFIG_PUBLICATION_UNSAFE") from None
    return hashlib.sha256(encoded).hexdigest()


def publication_matches(content, fact):
    """Match exact observed facts; a matching SHA256 alone authorizes nothing."""
    if type(content) is not ConfigContent or type(fact) is not PublicationFact:
        return False
    try:
        content = ConfigContent.model_validate(content.model_dump())
        fact = PublicationFact.model_validate(fact.model_dump())
        return (
            content.is_public
            and (content.tenant_id, content.config_id)
            == (fact.tenant_id, fact.config_id)
            and fact.schema_revision == PUBLIC_TEXT_SCHEMA
            and hmac.compare_digest(publication_digest(content), fact.content_digest)
        )
    except (ValidationError, BusinessRuleException):
        return False
