"""Secret-safe projection helpers for platform-owned AI configuration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_SUFFIXES = (
    "apikey",
    "authorization",
    "bearertoken",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
)
_SENSITIVE_EXACT = frozenset({"cookie", "cookies", "header", "headers"})


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def redact_config(value: Any) -> Any:
    """Recursively redact credential-shaped keys in legacy or current config."""
    if isinstance(value, dict):
        result = {}
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_EXACT or normalized.endswith(
                _SENSITIVE_SUFFIXES
            ):
                result[key] = "***"
            else:
                result[key] = redact_config(nested)
        return result
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    if isinstance(value, tuple):
        return [redact_config(item) for item in value]
    return value


def redact_url(value: str | None) -> str | None:
    """Drop credentials/query/fragment from legacy URLs before projection."""
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
