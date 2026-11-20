"""Short-lived preview MACs contain a fingerprint, never rules or business values."""

import hashlib
import hmac
import time

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.documents import digest


def stale():
    return BusinessException(
        409, "预览已失效，请重新预览", error_code="FIELD_POLICY_PREVIEW_STALE"
    )


def _mac(body):
    return hmac.new(
        settings.SECRET_KEY.encode(),
        ("field-management/1:" + body).encode(),
        hashlib.sha256,
    ).hexdigest()


def issue_preview(context):
    expires = int(time.time()) + 300
    body = f"{expires}.{digest(context, 4 * 1024 * 1024)}"
    return f"{body}.{_mac(body)}", expires


def verify_preview(token, context):
    try:
        expires, fingerprint, signature = token.split(".")
        valid = (
            expires.isascii()
            and expires.isdecimal()
            and len(expires) <= 12
            and int(time.time()) < int(expires) <= int(time.time()) + 300
            and hmac.compare_digest(signature, _mac(f"{expires}.{fingerprint}"))
            and hmac.compare_digest(fingerprint, digest(context, 4 * 1024 * 1024))
        )
    except (ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise stale()
