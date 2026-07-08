"""读操作 chip 跳转回放查询端点 — spec §2.9 / §8.7

GET /ai/query-cache/<trace_id>
  用途：前端 chip 跳模块页后，模块页 mounted 时反查此端点回放筛选
  权限：仅 trace_id 对应 user_id 本人（防越权）
  返回：最新写入的 field；hash 不存在或已过期 → data=null
"""

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.core.base_response import ResponseModel
from app.core.exceptions import AuthorizationException
from app.core.redis import redis_client
from app.modules.ai.agents.hitl.query_cache import get_query_cache
from app.modules.ai.schemas.query_cache import QueryCacheOut
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


class EmptyData(BaseModel):
    """data=null 时占位（FastAPI response_model None 处理麻烦）"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


@router.get("/{trace_id}", summary="按 trace_id 查 chip 跳转回放筛选条件")
async def get_query_cache_endpoint(
    trace_id: str,
    tool_name: str | None = Query(None, description="指定 tool_name 取特定 field"),
    _current_user: User = Depends(get_current_user),
) -> ResponseModel[QueryCacheOut | None]:
    """spec §8.7: chip 跳转回放——返回 trace_id 对应的最新 query_cache entry

    权限：仅 trace_id 对应 user_id 本人查询（防越权）
    返回规则：取 hash 中最新写入（按 created_at 降序）的 field；不传 tool_name 时
             默认最新；hash 不存在或已过期返回 data=null。
    """
    entry = await get_query_cache(redis_client, trace_id, tool_name=tool_name)
    if entry is None:
        # spec §8.7: hash 不存在或已过期 → data=null（不是 404）
        return ResponseModel.success(data=None)

    # owner 校验（spec §8.7: 防越权）
    if entry.user_id != _current_user.user_id:
        logger.info(
            "query_cache denied: user=%s cache_user=%s trace_id=%s",
            _current_user.user_name,
            entry.user_id,
            trace_id,
        )
        raise AuthorizationException(error_code="AI_QUERY_CACHE_FORBIDDEN")

    return ResponseModel.success(data=QueryCacheOut.model_validate(entry))
