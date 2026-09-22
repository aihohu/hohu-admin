"""UTC contract for legacy AI audit columns stored without a timezone."""

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BeforeValidator


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, int) or (
        isinstance(value, str) and value.lstrip("-").isdigit()
    ):
        timestamp = int(value)
        if not 10**12 <= timestamp < 10**13:
            raise ValueError("timestamp 必须是毫秒级时间戳")
        return datetime.fromtimestamp(timestamp / 1000, tz=UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("必须是 ISO 8601 时间或毫秒级时间戳")
    # These legacy AI columns have UTC semantics, unlike local business columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


AiUtcDatetime = Annotated[datetime, BeforeValidator(_utc_datetime)]
