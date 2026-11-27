"""Closed, dormant control-plane config contracts; no runtime serializer switch."""

from app.core.exceptions import BusinessException, BusinessRuleException
from app.modules.ai.core.generation_settings import generation_settings
from app.utils.closed_json import closed_object


def validate_control_config(value, *, kind):
    """Validate supported extension values without modifying caller data."""
    if type(kind) is not str or kind not in {"provider", "model"}:
        raise BusinessRuleException(
            "Unknown configuration domain", error_code="AI_CONTROL_CONFIG_INVALID"
        )
    if value is None:
        return None
    clean, _ = closed_object(value, max_bytes=64 * 1024)
    if not clean.keys() <= ({"generation"} if kind == "model" else set()):
        raise BusinessRuleException(
            "Unsupported configuration fields", error_code="AI_CONTROL_CONFIG_INVALID"
        )
    if kind == "model":
        generation_settings(clean)
    return clean


def project_control_config(value, *, kind):
    """Unknown stored objects stay stored, but their values are not projected."""
    try:
        return validate_control_config(value, kind=kind)
    except BusinessException:
        return {}
