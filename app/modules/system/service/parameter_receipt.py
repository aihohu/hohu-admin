"""Protocol freshness only. Owners must still authorize every actual field."""

import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_read_budget import read_phase
from app.utils.closed_json import closed_object

_FORM_DOMAIN = b"hohu.parameter.form/v1\x00"
_REVISION_DOMAIN = b"hohu.parameter.publication-revision/v1\x00"


def _error():
    return BusinessException(
        409,
        "Reload the current form before saving",
        error_code="FIELD_POLICY_PROTOCOL_REQUIRED",
    )


class FormFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tenant_id: int = Field(ge=0, lt=2**63)
    actor_id: int = Field(gt=0, lt=2**63)
    actor_version: int = Field(gt=0, lt=2**63)
    tenant_version: int = Field(gt=0, lt=2**63)
    object_id: Literal["system.config", "system.setting", "job.definition"]
    mode: Literal["create", "update", "group"]
    group: (
        Literal["brand", "account", "locale", "files", "agreements", "security"] | None
    ) = None
    target_id: int | None = Field(default=None, gt=0, lt=2**63)
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    g_epoch: int = Field(gt=0, lt=2**63)
    b_epoch: int = Field(gt=0, lt=2**63)

    @model_validator(mode="after")
    def target(self):
        if (
            (self.mode == "group") != (self.group is not None)
            or self.group is not None
            and self.object_id != "system.setting"
        ):
            raise ValueError("Invalid group receipt")
        if (self.mode == "update") != (self.target_id is not None):
            raise ValueError("Invalid form target")
        return self


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _mac(domain, body):
    key = hashlib.sha256(domain + settings.SECRET_KEY.encode()).digest()
    return _b64(hmac.new(key, body, hashlib.sha256).digest())


def _now(value):
    result = int(time.time()) if value is None else value
    if type(result) is not int or not 0 <= result < 2**63 - 300:
        raise _error()
    return result


def issue_receipt(facts, *, now=None):
    if type(facts) is not FormFacts:
        raise _error()
    try:
        facts = FormFacts.model_validate(facts.model_dump())
    except ValidationError:
        raise _error() from None
    issued = _now(now)
    _, encoded = closed_object(
        {
            "v": 1,
            "facts": facts.model_dump(),
            "issuedAt": issued,
            "expiresAt": issued + 300,
        },
        max_bytes=1536,
    )
    body = _b64(encoded)
    return body + "." + _mac(_FORM_DOMAIN, body.encode("ascii"))


def require_receipt(token, facts, *, now=None):
    if (
        type(token) is not str
        or not token.isascii()
        or not 1 <= len(token) <= 2048
        or token.count(".") != 1
        or type(facts) is not FormFacts
    ):
        raise _error()
    body, signature = token.split(".")
    if not hmac.compare_digest(signature, _mac(_FORM_DOMAIN, body.encode("ascii"))):
        raise _error()
    try:
        raw = base64.b64decode(
            body + "=" * ((-len(body)) % 4), altchars=b"-_", validate=True
        )
        data, canonical = closed_object(raw.decode("utf-8"), max_bytes=1536)
        current = _now(now)
        if (
            _b64(canonical) != body
            or set(data) != {"v", "facts", "issuedAt", "expiresAt"}
            or type(data["v"]) is not int
            or data["v"] != 1
            or type(data["issuedAt"]) is not int
            or type(data["expiresAt"]) is not int
            or data["expiresAt"] - data["issuedAt"] != 300
            or data["issuedAt"] > current + 2
            or current >= data["expiresAt"]
            or FormFacts.model_validate(data["facts"]) != facts
        ):
            raise _error()
    except (ValueError, UnicodeError, ValidationError, BusinessException, TypeError):
        raise _error() from None


@read_phase("projection")
async def observed_form_facts(db, tx, context, *, mode, target_id=None, group=None):
    tx.assert_session(db)
    tx.assert_barrier(context.tenant.tenant_id)
    g = await db.scalar(
        select(UserFieldPolicyGlobalGuard.epoch).where(
            UserFieldPolicyGlobalGuard.guard_id == 1
        )
    )
    b = await db.scalar(
        select(UserFieldPolicyState.epoch).where(
            UserFieldPolicyState.tenant_id == context.tenant.tenant_id
        )
    )
    return FormFacts(
        tenant_id=context.tenant.tenant_id,
        actor_id=context.actor.user_id,
        actor_version=context.actor.auth_version,
        tenant_version=context.tenant.tenant_version,
        object_id=next(iter(context.catalog.actions[context.action].object_ids)),
        mode=mode,
        group=group,
        target_id=target_id,
        catalog_digest=context.catalog.digest,
        g_epoch=g,
        b_epoch=b,
    )


def _revision_facts(facts, row, publication, kind):
    if type(facts) is not FormFacts:
        raise _error()
    content = {
        key: row[key]
        for key in (
            ("tenant_id", "config_id", "is_public")
            if kind == "revoke"
            else (
                "tenant_id",
                "config_id",
                "config_key",
                "config_value",
                "config_type",
                "status",
                "is_public",
                "value_sensitivity",
            )
        )
    }
    prior = {
        key: value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value
        for key, value in (publication or {}).items()
    }
    _, encoded = closed_object(
        {
            "kind": kind,
            "facts": facts.model_dump(),
            "content": content,
            "publication": prior,
        },
        max_bytes=16384,
    )
    return encoded


def publication_revision(facts, row, publication, *, now=None, kind="publish"):
    if kind not in {"publish", "revoke"}:
        raise _error()
    issued = _now(now)
    _, encoded = closed_object(
        {"v": 1, "issuedAt": issued, "expiresAt": issued + 300}, max_bytes=256
    )
    body = _b64(encoded)
    signature = _mac(
        _REVISION_DOMAIN,
        body.encode("ascii") + b"\x00" + _revision_facts(facts, row, publication, kind),
    )
    return body + "." + signature


def require_publication_revision(
    token, facts, row, publication, *, now=None, kind="publish"
):
    try:
        if (
            type(token) is not str
            or not token.isascii()
            or len(token) > 2048
            or token.count(".") != 1
        ):
            raise ValueError
        body, signature = token.split(".")
        raw = base64.b64decode(
            body + "=" * ((-len(body)) % 4), altchars=b"-_", validate=True
        )
        data, canonical = closed_object(raw.decode("utf-8"), max_bytes=256)
        current = _now(now)
        if (
            _b64(canonical) != body
            or set(data) != {"v", "issuedAt", "expiresAt"}
            or type(data["v"]) is not int
            or data["v"] != 1
            or type(data["issuedAt"]) is not int
            or type(data["expiresAt"]) is not int
            or data["expiresAt"] - data["issuedAt"] != 300
            or data["issuedAt"] > current + 2
            or current >= data["expiresAt"]
            or not hmac.compare_digest(
                signature,
                _mac(
                    _REVISION_DOMAIN,
                    body.encode("ascii")
                    + b"\x00"
                    + _revision_facts(facts, row, publication, kind),
                ),
            )
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, BusinessException):
        raise BusinessException(
            409,
            "Publication changed; reload before confirming",
            error_code="CONFIG_PUBLICATION_CONFLICT",
        ) from None
