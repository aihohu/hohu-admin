"""Fail-closed replay endpoint for readonly-tool query chips."""

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use
from app.core.base_response import ResponseModel
from app.core.exceptions import NotFoundException
from app.core.redis import redis_client
from app.db.session import get_db
from app.modules.ai.agents.hitl.query_cache import get_query_cache
from app.modules.ai.schemas.query_cache import QueryCacheOut
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


class EmptyData(BaseModel):
    """Compatibility placeholder retained for generated schema stability."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


@router.get("/{trace_id}", summary="按 trace_id 查 chip 跳转回放筛选条件")
async def get_query_cache_endpoint(
    trace_id: str,
    tool_name: str | None = Query(None, description="指定 tool_name 取特定 field"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
) -> ResponseModel[QueryCacheOut]:
    """Return a cache entry only after owner, tenant, and lineage reauthorization."""
    entry = await get_query_cache(redis_client, trace_id, tool_name=tool_name)
    allowed = False
    if entry is not None and entry.user_id == _current_user.user_id:
        allowed = await result_projection_service.authorize_result_projection(
            db,
            _current_user,
            owner_user_id=entry.user_id,
            lineage=result_projection_service.lineage_from_record(entry),
        )
    if not allowed:
        logger.info(
            "query_cache unavailable: user=%s trace_id=%s",
            _current_user.user_name,
            trace_id,
        )
        raise NotFoundException("AI query cache", error_code="AI_QUERY_CACHE_NOT_FOUND")

    return ResponseModel.success(data=QueryCacheOut.model_validate(entry))
