"""读操作 chip 跳转回放查询 schema — spec §8.7"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class QueryCacheOut(BaseModel):
    """/ai/query-cache/<trace_id> 响应 data 字段（spec §8.7）"""

    tool_name: str
    module: str = Field(..., description="模块页路由前缀，如 'system/user'")
    filters: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )
