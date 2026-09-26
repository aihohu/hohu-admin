"""Validated per-model generation options; omitted options use provider defaults."""

import math

from app.core.exceptions import BusinessRuleException


def generation_settings(config: dict | None) -> dict:
    raw = (config or {}).get("generation")
    if raw is None:
        return {}
    error = BusinessRuleException(
        "Invalid model generation settings", error_code="AI_MODEL_GENERATION_INVALID"
    )
    if not isinstance(raw, dict) or not raw.keys() <= {"max_tokens", "temperature"}:
        raise error
    result = {key: value for key, value in raw.items() if value is not None}
    tokens = result.get("max_tokens")
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= 1_000_000):
        raise error
    temperature = result.get("temperature")
    if temperature is not None and (
        type(temperature) not in {float, int}
        or not math.isfinite(temperature)
        or not 0 <= temperature <= 2
    ):
        raise error
    return result
