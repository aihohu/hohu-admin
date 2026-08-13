"""ai_routing_log 写入服务。

所有 /ai/chat 请求都写一条（不仅 "auto"），reason 区分 9 种类型.
input_message_hash 使用 HMAC-SHA256。
"""

import hashlib
import hmac
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.ai.models.routing_log import AiRoutingLog

if TYPE_CHECKING:
    from app.modules.ai.models.agent import AiAgent


def _hash_message(message: str, user_id: int) -> str:
    """计算 HMAC-SHA256(server_secret + user_id + message)。

    用 settings.SECRET_KEY（已必填）而非单独配置 AI_ROUTING_HMAC_SECRET，
    避免默认值导致跨部署 HMAC 等价（彩虹表反查风险）.
    """
    return hmac.new(
        settings.SECRET_KEY.encode(),
        f"{user_id}:{message}".encode(),
        hashlib.sha256,
    ).hexdigest()


class RoutingLogService:
    async def write_log(
        self,
        db: AsyncSession,
        *,
        trace_id: str,
        user_id: int,
        conversation_id: int | None,
        input_message: str,
        candidates: list[str] | list["AiAgent"],
        final_agent: str | None,
        llm_choice: str | None = None,
        reason: str,
        latency_ms: int,
        parent_log_id: int | None = None,
        plan_step_index: int | None = None,
    ) -> AiRoutingLog:
        """写一条 routing_log. 调用方负责 db.commit()."""
        # candidates 可能是 AiAgent 对象列表或 code 字符串列表
        if candidates and hasattr(candidates[0], "code"):
            candidates_codes = [c.code for c in candidates]  # type: ignore[attr-defined]
        else:
            candidates_codes = list(candidates)  # type: ignore[arg-type]

        log = AiRoutingLog(
            trace_id=trace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            input_message_hash=_hash_message(input_message, user_id),
            candidates=candidates_codes,
            llm_choice=llm_choice,
            final_agent=final_agent,
            reason=reason,
            latency_ms=latency_ms,
            parent_log_id=parent_log_id,
            plan_step_index=plan_step_index,
        )
        db.add(log)
        return log


routing_log_service = RoutingLogService()
