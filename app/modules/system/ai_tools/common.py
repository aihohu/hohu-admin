"""Shared presentation helpers for System AI tools."""

from typing import Any

from app.modules.ai.agents.gateway.result import ResultProjection


def _result_projection(
    subject_type: str | None = None,
    subject_ids: list[Any] | tuple[Any, ...] = (),
    *,
    scope_bound: bool = False,
) -> ResultProjection:
    refs = (
        tuple({"type": subject_type, "id": str(value)} for value in subject_ids)
        if subject_type is not None
        else ()
    )
    return ResultProjection(subject_refs=refs, scope_bound=scope_bound)


def _confirmation_display(value: Any) -> str | int | float:
    """Convert a frozen non-scalar argument into a safe scalar presentation."""
    if value is None or value == []:
        return "—"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, str | int | float) and not isinstance(value, bool):
        return value
    return str(value)


def _bound_confirmation_fields(
    execution_args: dict[str, Any],
    labels: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Bind safe display scalars to exact frozen execution arguments."""
    fields: list[dict[str, Any]] = []
    for label in labels:
        if label not in execution_args:
            continue
        raw_value = execution_args[label]
        field: dict[str, Any] = {"label": label, "value": raw_value}
        if raw_value is None or isinstance(raw_value, list):
            field["display_value"] = _confirmation_display(raw_value)
        fields.append(field)
    return fields


_LIST_MAX_LIMIT = 50
_LIST_DEFAULT_LIMIT = 20


def _coerce_list_limit(limit: int | None) -> int:
    """规范化 limit：None=默认 20；负数=默认；>50=截断到 50"""
    if limit is None or limit <= 0:
        return _LIST_DEFAULT_LIMIT
    return min(limit, _LIST_MAX_LIMIT)
