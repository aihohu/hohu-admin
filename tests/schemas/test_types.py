"""LocalNaiveDatetime 类型单元测试。

设计目标：消除"前端 datetime 范围查询触发 asyncpg aware/naive TypeError"的
陷阱。前端 NDatePicker value 是 unix ms timestamp，直接发到后端；后端
LocalNaiveDatetime 接受 ms timestamp / ISO 字符串 / datetime，统一转成
**按服务器本地时区** 的 naive datetime，与 PG `TIMESTAMP WITHOUT TIME ZONE`
列对齐。

为什么按本地时区：DB 写入用 `func.now()` / `datetime.now()`（naive 本地），
读取过滤也必须用本地，否则跨时区服务器（如 UTC 云主机 + 中国用户）会出现
"用户选 7/1 00:00 → 实际查 6/30 16:00"的 8 小时偏差。
"""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.types import LocalNaiveDatetime


class _Wrap(BaseModel):
    """测试载体：单独验证类型行为，避免依赖具体业务 schema。"""

    t: LocalNaiveDatetime | None = None


class TestFromMsTimestamp:
    def test_ms_timestamp_converts_to_local_naive(self):
        """NDatePicker value（unix ms）→ 本地 naive datetime。"""
        ts_ms = 1700000000000  # 2023-11-14 22:13:20 UTC
        expected = datetime.fromtimestamp(ts_ms / 1000)  # 本地 naive
        m = _Wrap(t=ts_ms)
        assert m.t == expected
        assert m.t.tzinfo is None

    def test_numeric_string_treated_as_ms_timestamp(self):
        """FastAPI query string 透传时数字会变成 str。

        "1751241600000" 应被识别为 ms timestamp，而非 ISO 字符串。
        回归 bug：曾经因为只 isinstance(v, int) 检查导致 query string 走 ISO
        分支 → datetime.fromisoformat("1751241600000") → ValueError。
        """
        ts_ms = 1751241600000
        expected = datetime.fromtimestamp(ts_ms / 1000)
        m = _Wrap(t=str(ts_ms))
        assert m.t == expected
        assert m.t.tzinfo is None

    def test_seconds_timestamp_rejected(self):
        """秒级 timestamp 容易和 ms 混淆，必须拒绝。

        1700000000（10 位）若当成 ms 解析，得到 1970-01-20，明显错乱。
        合法 ms timestamp 在 [1e12, 1e13) 区间（2001-09 ~ 2286-11）。
        """
        with pytest.raises(ValidationError):
            _Wrap(t=1700000000)


class TestFromISO:
    def test_z_suffix_converted_to_local_naive(self):
        """ISO 'Z' → 按 UTC 解析 → 转本地 naive。

        UTC 00:00 在 UTC+8 服务器上 = 本地 08:00。
        """
        m = _Wrap(t="2026-07-01T00:00:00.000Z")
        expected = (
            datetime(2026, 7, 1, 0, 0, tzinfo=UTC).astimezone().replace(tzinfo=None)
        )
        assert m.t == expected
        assert m.t.tzinfo is None

    def test_offset_converted_to_local_naive(self):
        """ISO +08:00 → 等价 UTC 00:00 → 本地（UTC+8）08:00。"""
        m = _Wrap(t="2026-07-01T08:00:00+08:00")  # = UTC 00:00
        expected = (
            datetime(2026, 7, 1, 0, 0, tzinfo=UTC).astimezone().replace(tzinfo=None)
        )
        assert m.t == expected
        assert m.t.tzinfo is None

    def test_naive_iso_passes_through(self):
        """无 tz ISO 字符串 → 假定已是本地时间，原样解析为 naive。"""
        m = _Wrap(t="2026-07-01T12:00:00")
        assert m.t == datetime(2026, 7, 1, 12, 0, 0)
        assert m.t.tzinfo is None


class TestFromDatetime:
    def test_aware_converted_to_local_naive(self):
        d = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        m = _Wrap(t=d)
        assert m.t == d.astimezone().replace(tzinfo=None)
        assert m.t.tzinfo is None

    def test_naive_passes_through(self):
        d = datetime(2026, 7, 1, 12, 0, 0)
        m = _Wrap(t=d)
        assert m.t == d
        assert m.t.tzinfo is None


class TestOptionalNone:
    def test_optional_field_accepts_none(self):
        assert _Wrap().t is None
        assert _Wrap(t=None).t is None
