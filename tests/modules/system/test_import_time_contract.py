from datetime import UTC, datetime, timedelta, timezone

from app.modules.system.models.user_transfer import UserImportBatch, UserImportBatchLog
from app.modules.system.schemas.user_transfer import UserImportBatchQuery


def test_import_storage_preserves_timezones():
    for field in ("created_at", "started_at", "finished_at"):
        assert UserImportBatch.__table__.c[field].type.timezone
    assert UserImportBatchLog.__table__.c.created_at.type.timezone


def test_query_converts_equal_instants_to_aware_utc():
    instant = datetime(2026, 9, 16, 4, tzinfo=UTC)
    query = UserImportBatchQuery(
        start_time=int(instant.timestamp() * 1000),
        end_time=instant.astimezone(timezone(timedelta(hours=8))).isoformat(),
    )
    assert query.start_time == query.end_time == instant
    assert query.start_time.tzinfo is not None
