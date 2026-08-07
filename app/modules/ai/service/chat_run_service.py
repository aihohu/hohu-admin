"""Task 35a.0 ChatCommand run guard 与 assistant terminal finalizer。"""

import secrets
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.id_generator import next_id
from app.modules.ai.agents.hitl.events import (
    AiStreamEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
    _ui_to_dict,
    stringify_large_ints,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.models.message import AiMessage

_RENEW_GUARD_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_GUARD_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class ToolCallCollector:
    """按 started ordinal 固定槽位，result 只回填，不改变展示顺序。"""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._positions: dict[str, int] = {}

    def record(self, event: AiStreamEvent) -> None:
        if isinstance(event, ToolCallStartedEvent):
            if event.tool_call_id in self._positions:
                return
            self._positions[event.tool_call_id] = len(self._items)
            self._items.append(
                {
                    "tool": event.tool,
                    "tool_call_id": event.tool_call_id,
                    "summary": event.summary,
                    "args": stringify_large_ints(event.args),
                    "risk": event.risk,
                    "trace_id": event.trace_id,
                    "chip_target": event.chip_target,
                }
            )
            return
        if not isinstance(event, ToolCallResultEvent):
            return
        position = self._positions.get(event.tool_call_id)
        if position is None:
            position = len(self._items)
            self._positions[event.tool_call_id] = position
            self._items.append(
                {
                    "tool": event.tool,
                    "tool_call_id": event.tool_call_id,
                }
            )
        self._items[position].update(
            {
                "ok": event.ok,
                "result": stringify_large_ints(event.result),
                "affected_rows": event.affected_rows,
                "error_code": event.error_code,
                "error_msg": event.error_msg,
                "duration_ms": event.duration_ms,
                "ui": _ui_to_dict(event.ui) if event.ui else None,
            }
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items]


class ChatRunGuard:
    """conversation-scoped Redis lease；Redis 是互斥加速器，不是 action 事实源。"""

    prefix = "ai:chat:run"

    @staticmethod
    def generate_owner_token() -> str:
        return secrets.token_urlsafe(24)

    @classmethod
    def key(cls, conversation_id: int) -> str:
        return f"{cls.prefix}:{conversation_id}"

    async def acquire(
        self,
        redis: Redis,
        *,
        conversation_id: int,
        owner_token: str,
        ttl_sec: int | None = None,
    ) -> bool:
        ttl = ttl_sec or settings.AI_CHAT_RUN_GUARD_TTL_SEC
        return bool(
            await redis.set(
                self.key(conversation_id),
                owner_token,
                nx=True,
                ex=ttl,
            )
        )

    async def renew(
        self,
        redis: Redis,
        *,
        conversation_id: int,
        owner_token: str,
        ttl_sec: int | None = None,
    ) -> bool:
        ttl = ttl_sec or settings.AI_CHAT_RUN_GUARD_TTL_SEC
        result = await redis.eval(
            _RENEW_GUARD_LUA,
            1,
            self.key(conversation_id),
            owner_token,
            ttl,
        )
        return bool(result)

    async def handoff_pending(
        self,
        redis: Redis,
        *,
        conversation_id: int,
        owner_token: str,
        confirmation_ttl_sec: int,
    ) -> bool:
        ttl = confirmation_ttl_sec + settings.AI_CHAT_RUN_GUARD_PENDING_GRACE_SEC
        return await self.renew(
            redis,
            conversation_id=conversation_id,
            owner_token=owner_token,
            ttl_sec=ttl,
        )

    async def release(
        self,
        redis: Redis,
        *,
        conversation_id: int,
        owner_token: str,
    ) -> bool:
        result = await redis.eval(
            _RELEASE_GUARD_LUA,
            1,
            self.key(conversation_id),
            owner_token,
        )
        return bool(result)


class ChatRunFinalizer:
    """按 (conversation, trace) 幂等创建或合并 terminal assistant projection。"""

    async def finalize_assistant_turn(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        trace_id: str,
        source_user_message_id: int,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
        agent_code: str | None,
    ) -> AiMessage | None:
        stmt = (
            select(AiMessage)
            .where(
                AiMessage.conversation_id == conversation_id,
                AiMessage.role == "assistant",
                AiMessage.trace_id == trace_id,
            )
            .with_for_update()
        )
        message = (await db.execute(stmt)).scalars().first()
        if message is None:
            if not content and not tool_calls:
                return None
            candidate_id = next_id()
            create_stmt = (
                insert(AiMessage)
                .values(
                    message_id=candidate_id,
                    conversation_id=conversation_id,
                    parent_message_id=source_user_message_id,
                    role="assistant",
                    message_type="text",
                    content=content,
                    tool_calls=tool_calls or None,
                    trace_id=trace_id,
                    agent_code=agent_code,
                    is_active=True,
                )
                .on_conflict_do_nothing(
                    index_elements=["conversation_id", "trace_id"],
                    index_where=text("role = 'assistant' AND trace_id IS NOT NULL"),
                )
            )
            await db.execute(create_stmt)
            message = (
                (
                    await db.execute(
                        select(AiMessage)
                        .where(
                            AiMessage.conversation_id == conversation_id,
                            AiMessage.role == "assistant",
                            AiMessage.trace_id == trace_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .one()
            )
        if not message.content and content:
            message.content = content
        message.tool_calls = self._merge_tool_calls(
            message.tool_calls,
            tool_calls,
        )
        if message.parent_message_id is None:
            message.parent_message_id = source_user_message_id
        if message.agent_code is None:
            message.agent_code = agent_code
        await db.flush()
        return message

    async def finalize_pending_turn(
        self,
        db: AsyncSession,
        *,
        pending: PendingPayload,
        ok: bool,
        duration_ms: int = 0,
        result: Any = None,
        error_code: str | None = None,
        error_msg: str | None = None,
    ) -> AiMessage | None:
        """离线/续传终态用 pending context 重建同一 tool-only assistant。"""
        if pending.source_user_message_id is None:
            return None
        tool_call = {
            "tool": pending.tool_name,
            "tool_call_id": pending.tool_call_id,
            "summary": f"tool={pending.tool_name}, mode=hitl",
            "args": stringify_large_ints(pending.args),
            "risk": pending.risk_level,
            "trace_id": pending.trace_id,
            "chip_target": pending.chip_target,
            "ok": ok,
            "result": stringify_large_ints(result),
            "affected_rows": None,
            "error_code": error_code,
            "error_msg": error_msg,
            "duration_ms": duration_ms,
            "ui": None,
        }
        return await self.finalize_assistant_turn(
            db,
            conversation_id=pending.conversation_id,
            trace_id=pending.trace_id,
            source_user_message_id=pending.source_user_message_id,
            content="",
            tool_calls=[tool_call],
            agent_code=pending.agent_code,
        )

    @staticmethod
    def _merge_tool_calls(
        existing: list[dict[str, Any]] | None,
        incoming: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        if not existing and not incoming:
            return None
        merged = [dict(item) for item in existing or []]
        positions = {
            item.get("tool_call_id"): index
            for index, item in enumerate(merged)
            if item.get("tool_call_id")
        }
        for item in incoming or []:
            tool_call_id = item.get("tool_call_id")
            if tool_call_id and tool_call_id in positions:
                position = positions[tool_call_id]
                merged[position] = {**merged[position], **item}
            else:
                if tool_call_id:
                    positions[tool_call_id] = len(merged)
                merged.append(dict(item))
        return merged


chat_run_guard = ChatRunGuard()
chat_run_finalizer = ChatRunFinalizer()
