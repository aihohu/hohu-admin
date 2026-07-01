"""跨模块复用的 Pydantic 类型。

目前只有 LocalNaiveDatetime：解决"前端时间范围查询触发 asyncpg aware/naive
TypeError + 跨时区 8 小时偏差"的陷阱。详见 tests/schemas/test_types.py。
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BeforeValidator

_MS_MIN = 10**12  # 2001-09-09 09:46:40 UTC；小于此值大概率是秒级 timestamp
_MS_MAX = 10**13  # 2286-11-20 17:46:40 UTC；超过此值疑似纳秒


def _to_local_naive_datetime(v: Any) -> Any:
    """统一把输入转成 naive 本地 datetime。

    输入支持：
    - int (unix ms timestamp)：NDatePicker value 的原生类型
    - 数字字符串 ("1751241600000")：FastAPI query string 透传时变成 str
    - ISO 8601 字符串 (带或不带 tz)：兼容旧前端、curl 手测
    - datetime (aware/naive)：兼容后端内部直接构造

    None 原样返回（让 Optional 字段处理）。

    设计取舍：
    - ms timestamp 在 [_MS_MIN, _MS_MAX) 区间外拒绝，防止秒级误传导致 1970 错乱
    - aware datetime / ISO → astimezone() 转本地（无参数 = 系统时区）
    - naive datetime / ISO → 假定已是本地时间，原样保留
    """
    if v is None:
        return None

    # int / 数字字符串 → 按 ms timestamp 处理（NDatePicker value 原生类型）
    if isinstance(v, int) or (isinstance(v, str) and v.lstrip("-").isdigit()):
        ts = int(v)
        if not _MS_MIN <= ts < _MS_MAX:
            raise ValueError(
                f"timestamp 必须是毫秒级（{_MS_MIN}-{_MS_MAX}），收到 {v}"
                "（可能是秒级 timestamp，请前端发 ms）"
            )
        return datetime.fromtimestamp(ts / 1000)

    if isinstance(v, str):
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    elif isinstance(v, datetime):
        parsed = v
    else:
        raise TypeError(f"不支持的类型: {type(v).__name__}")

    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


LocalNaiveDatetime = Annotated[datetime, BeforeValidator(_to_local_naive_datetime)]
"""统一的 naive datetime 类型，与 PG TIMESTAMP WITHOUT TIME ZONE 列对齐。

Query schema 里凡是 datetime 字段（特别是时间范围查询），用：

    class XxxQuery(BaseModel):
        start_time: LocalNaiveDatetime | None = None
        end_time: LocalNaiveDatetime | None = None

会自动接受 NDatePicker ms timestamp / ISO 字符串 / datetime，转成本地 naive。
"""
