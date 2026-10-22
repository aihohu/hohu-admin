"""ChatCommand 运行守卫与 assistant 终态收口器。"""

import re
import secrets
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.modules.ai.agents.gateway.quota import is_write_tool
from app.modules.ai.agents.hitl.events import (
    AiStreamEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
    _ui_to_dict,
    stringify_large_ints,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    LockedClaim,
    LockedCompletion,
    atomic_completion_service,
    has_protocol_marker,
)
from app.modules.ai.service.preflight_completion_service import has_preflight_marker
from app.modules.ai.service.reconciliation_annotation import (
    has_annotation,
    reconciliation_notice,
)
from app.modules.ai.service.result_projection_service import (
    ProjectionLineage,
    result_projection_service,
)
from app.modules.auth.service import load_live_user_authority

_RENEW_GUARD_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_GUARD_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

_MANAGEMENT_AGENT_CODES = frozenset({"dept_mgmt", "role_mgmt", "user_mgmt"})
_SAFE_UNVERIFIED_WRITE_MESSAGE = (
    "本轮未产生可验证的写工具结果，因此没有确认任何业务变更。请重新发起操作。"
)
_SAFE_IMPORT_FIELD_ERROR_MESSAGE = (
    "导入文件字段校验失败。请根据工具错误中的行号、字段、原因和错误码修正文件；"
    "为保护数据，原始单元格值与整行内容未显示。"
)
_WRITE_CLAIM_PATTERN = re.compile(
    r"(?:已|已经)成功(?:地)?(?:创建|新增|更新|修改|移动|调整|设置|绑定|授权|分配|禁用|启用|删除|重置|导入)"
    r"|(?:创建|新增|更新|修改|移动|调整|设置|绑定|授权|分配|禁用|启用|删除|重置|导入)(?:已)?(?:成功|完成)"
    r"|(?:has|have)\s+been\s+(?:successfully\s+)?(?:created|updated|moved|assigned|enabled|disabled|deleted|reset|imported)"
    r"|(?:was|were)\s+successfully\s+(?:created|updated|moved|assigned|enabled|disabled|deleted|reset|imported)"
    r"|(?:created|updated|moved|assigned|enabled|disabled|deleted|reset|imported)\s+successfully",
    re.IGNORECASE,
)


def enforce_grounded_management_write_claim(
    content: str,
    *,
    agent_code: str | None,
    tool_calls: list[dict[str, Any]] | None,
) -> tuple[str, bool]:
    """Replace an asserted write success that has no successful write tool result."""
    if agent_code not in _MANAGEMENT_AGENT_CODES or not _WRITE_CLAIM_PATTERN.search(
        content
    ):
        return content, False

    registry = ToolRegistry.get()
    for call in tool_calls or ():
        if call.get("ok") is not True:
            continue
        registered = registry.find(str(call.get("tool") or ""))
        if registered is None:
            continue
        result = call.get("result")
        if registered.meta.interaction_flow == "prepared":
            if isinstance(result, dict) and result.get("actionStatus") == "executed":
                return content, False
            continue
        if is_write_tool(registered.meta):
            return content, False
    return _SAFE_UNVERIFIED_WRITE_MESSAGE, True


def enforce_import_error_output_redaction(
    content: str,
    *,
    tool_calls: list[dict[str, Any]] | None,
) -> tuple[str, bool]:
    """Replace Provider prose after field errors with a deterministic safe summary."""
    failures = [
        call
        for call in tool_calls or ()
        if (
            call.get("ok") is False
            and call.get("error_code") == "AI_IMPORT_FIELD_ERRORS"
        )
    ]
    if failures:
        # Only deterministic parser diagnostics, never provider prose/cell values.
        details = "\n\n".join(
            str(call.get("error_msg") or "")[:6000] for call in failures
        )
        return _SAFE_IMPORT_FIELD_ERROR_MESSAGE + "\n\n" + details, True
    return content, False


class ToolCallCollector:
    """按 started ordinal 固定槽位，result 只回填，不改变展示顺序。"""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._positions: dict[str, int] = {}
        self._projections: dict[str, Any] = {}

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
        self._projections[event.tool_call_id] = event.projection
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

    def snapshot_projection(self) -> tuple[list[str], list[dict[str, str]], bool, bool]:
        """Return tools, stable subjects, scope binding, and completeness."""
        tool_codes: list[str] = []
        subject_refs: list[dict[str, str]] = []
        scope_bound = False
        complete = True
        for item in self._items:
            tool_code = item.get("tool")
            tool_call_id = item.get("tool_call_id")
            if isinstance(tool_code, str):
                tool_codes.append(tool_code)
            projection = self._projections.get(str(tool_call_id))
            if projection is None:
                complete = False
                continue
            subject_refs.extend(projection.subject_refs)
            scope_bound = scope_bound or projection.scope_bound
        return tool_codes, subject_refs, scope_bound, complete


class ChatRunGuard:
    """conversation-scoped Redis lease；Redis 是互斥加速器，不是 action 事实源。"""

    prefix = "ai:chat:run"

    @staticmethod
    def generate_owner_token() -> str:
        return secrets.token_urlsafe(24)

    @classmethod
    def key(cls, conversation_id: int, *, tenant: TenantContext) -> str:
        return f"{cls.prefix}:tenant:{tenant.tenant_id}:{conversation_id}"

    async def acquire(
        self,
        redis: Redis,
        *,
        conversation_id: int,
        owner_token: str,
        tenant: TenantContext,
        ttl_sec: int | None = None,
    ) -> bool:
        ttl = ttl_sec or settings.AI_CHAT_RUN_GUARD_TTL_SEC
        return bool(
            await redis.set(
                self.key(conversation_id, tenant=tenant),
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
        tenant: TenantContext,
        ttl_sec: int | None = None,
    ) -> bool:
        ttl = ttl_sec or settings.AI_CHAT_RUN_GUARD_TTL_SEC
        result = await redis.eval(
            _RENEW_GUARD_LUA,
            1,
            self.key(conversation_id, tenant=tenant),
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
        tenant: TenantContext,
    ) -> bool:
        ttl = confirmation_ttl_sec + settings.AI_CHAT_RUN_GUARD_PENDING_GRACE_SEC
        return await self.renew(
            redis,
            conversation_id=conversation_id,
            owner_token=owner_token,
            ttl_sec=ttl,
            tenant=tenant,
        )

    async def release(
        self,
        redis: Redis,
        *,
        conversation_id: int,
        owner_token: str,
        tenant: TenantContext,
    ) -> bool:
        result = await redis.eval(
            _RELEASE_GUARD_LUA,
            1,
            self.key(conversation_id, tenant=tenant),
            owner_token,
        )
        return bool(result)


class ChatRunFinalizer:
    """按 (conversation, trace) 幂等创建或合并 terminal assistant projection。"""

    async def _lock_projection_facts(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        source_user_message_id: int,
        trace_id: str,
        tenant: TenantContext,
    ) -> tuple[AiConversation | None, AiMessage | None, list[AiPreparedAction], dict]:
        """Lock every source fact before taking any assistant lock."""
        await db.execute(select(func.set_config("lock_timeout", "2s", True)))
        conversation = await db.scalar(
            select(AiConversation)
            .where(
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.conversation_id == conversation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        source = await db.scalar(
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.message_id == source_user_message_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        actions = list(
            (
                await db.scalars(
                    select(AiPreparedAction)
                    .where(
                        AiPreparedAction.tenant_id == tenant.tenant_id,
                        AiPreparedAction.conversation_id == conversation_id,
                        AiPreparedAction.source_user_message_id
                        == source_user_message_id,
                        AiPreparedAction.trace_id == trace_id,
                    )
                    .order_by(AiPreparedAction.action_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        logs = {
            log.tool_call_id: log
            for log in await db.scalars(
                select(AiOperationLog)
                .where(
                    AiOperationLog.tenant_id == tenant.tenant_id,
                    or_(
                        AiOperationLog.tool_call_id.in_(
                            [action.execute_tool_call_id for action in actions]
                        ),
                        and_(
                            AiOperationLog.conversation_id == conversation_id,
                            AiOperationLog.trace_id == trace_id,
                            AiOperationLog.user_id == tenant.actor_user_id,
                        ),
                    ),
                )
                .order_by(AiOperationLog.log_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        return conversation, source, actions, logs

    async def _protected_cards(
        self,
        db: AsyncSession,
        *,
        conversation: AiConversation,
        source: AiMessage,
        actions: list[AiPreparedAction],
        logs: dict,
        tenant: TenantContext,
    ) -> tuple[dict[str, dict | None], bool, list[ProjectionLineage]]:
        protected: dict[str, dict | None] = {}
        unknown = False
        lineages = []
        actor = None
        for action in actions:
            bound_log = logs.get(action.execute_tool_call_id)
            if not (
                has_protocol_marker(action.execution_owner)
                or (bound_log is not None and has_annotation(bound_log.result_summary))
                or (
                    action.status
                    in {
                        "approved",
                        "running",
                        "succeeded",
                        "failed",
                        "rejected",
                        "expired",
                    }
                    and action.execute_tool_name in DB_TOOLS | FILE_TOOLS
                )
            ):
                continue
            tool_call_id = action.execute_tool_call_id
            card = {"tool": action.execute_tool_name, "tool_call_id": tool_call_id}
            protected[tool_call_id] = card
            log = logs.get(tool_call_id)
            try:
                if log is None or (
                    action.execute_tool_name in FILE_TOOLS
                    and action.status in {"running", "failed"}
                ):
                    raise BusinessRuleException("缺少可信完成事实")
                validator = (
                    atomic_completion_service.validate_locked_unexecuted_terminal
                    if action.status in {"rejected", "expired"}
                    else atomic_completion_service.validate_locked_terminal
                )
                validator(
                    action, log, tenant=tenant, conversation=conversation, source=source
                )
            except BusinessRuleException:
                unknown = True
                continue
            if actor is None:
                actor = await load_live_user_authority(db, tenant=tenant)
            lineage = result_projection_service.lineage_from_record(action)
            if not await result_projection_service.authorize_result_projection(
                db, actor, owner_user_id=action.user_id, lineage=lineage
            ):
                protected[tool_call_id] = None
                continue
            ok = action.status == "succeeded"
            card.update(
                ok=ok,
                result=stringify_large_ints(action.result_data) if ok else None,
                ui=stringify_large_ints(action.result_ui) if ok else None,
                error_code=None if ok else action.error_code,
                error_msg=(
                    None
                    if ok
                    else {
                        "rejected": "用户已取消此操作",
                        "expired": "确认已过期，请重新发起",
                    }.get(action.status, "操作未完成，业务修改已回滚。")
                ),
                duration_ms=action.duration_ms or 0,
                summary=str(
                    action.presentation.get("title") or action.execute_tool_name
                ),
                args=stringify_large_ints(action.presentation),
                trace_id=action.trace_id,
                risk=action.risk_level,
                chip_target=action.chip_target,
            )
            if lineage is not None:
                lineages.append(lineage)
        bound_tool_calls = {action.execute_tool_call_id for action in actions}
        for tool_call_id, log in logs.items():
            if tool_call_id not in bound_tool_calls and (
                not log.readonly_snapshot or has_preflight_marker(log.result_summary)
            ):
                protected[tool_call_id] = {
                    "tool": log.tool_name,
                    "tool_call_id": tool_call_id,
                }
                from app.modules.ai.service.preflight_completion_service import (  # noqa: PLC0415
                    is_verified_preflight,
                )

                if await is_verified_preflight(
                    db,
                    log,
                    conversation,
                    source,
                    tenant_id=tenant.tenant_id,
                    user_id=tenant.actor_user_id,
                ):
                    protected[tool_call_id].update(
                        ok=False,
                        error_code=log.error_code,
                        duration_ms=log.duration_ms,
                    )
                    continue
                from app.modules.ai.service.preview_completion_service import (  # noqa: PLC0415
                    preview_lineage,
                    verify_historical_preview,
                )

                try:
                    audit = await verify_historical_preview(
                        db,
                        log,
                        conversation,
                        source,
                        tenant_id=tenant.tenant_id,
                        user_id=tenant.actor_user_id,
                    )
                except (BusinessRuleException, TypeError, ValueError):
                    unknown = True
                    continue
                if actor is None:
                    actor = await load_live_user_authority(db, tenant=tenant)
                lineage = preview_lineage(log, audit, tenant)
                if not await result_projection_service.authorize_result_projection(
                    db,
                    actor,
                    owner_user_id=log.user_id,
                    lineage=lineage,
                ):
                    protected[tool_call_id] = None
                    continue
                protected[tool_call_id].update(
                    ok=True,
                    error_code=None,
                    duration_ms=log.duration_ms,
                    result=stringify_large_ints(audit["preview_data"]),
                    ui=stringify_large_ints(audit["preview_ui"]),
                )
                lineages.append(lineage)
        return protected, unknown, lineages

    @staticmethod
    def _replace_protected_cards(cards, protected):  # noqa: ANN001
        result = []
        for card in cards or []:
            tool_call_id = card.get("tool_call_id")
            if tool_call_id in protected:
                replacement = protected[tool_call_id]
                if replacement is not None:
                    result.append(dict(replacement))
            else:
                result.append(card)
        return result or None

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
        lineage: ProjectionLineage | None = None,
        projection_dependency_message_ids: list[int] | tuple[int, ...] = (),
        tenant: TenantContext,
        _prepared_action: AiPreparedAction | None = None,
        _locked_completion: LockedCompletion | LockedClaim | None = None,
    ) -> AiMessage | None:
        if lineage is not None and lineage.tenant_id != tenant.tenant_id:
            raise RuntimeError("projection lineage tenant mismatch")
        tool_calls = stringify_large_ints(tool_calls)
        if _prepared_action is None:
            conversation, source, actions, logs = await self._lock_projection_facts(
                db,
                conversation_id=conversation_id,
                source_user_message_id=source_user_message_id,
                trace_id=trace_id,
                tenant=tenant,
            )
        else:
            # This caller owns one complete C/source/PA/log plan. Appending
            # another action now would invert the global lock order.
            conversation = await db.get(AiConversation, conversation_id)
            source = await db.get(AiMessage, source_user_message_id)
            actions = []
            logs = {}
            if _locked_completion is not None:
                if _locked_completion.action is not _prepared_action:
                    raise BusinessRuleException("完成投影的锁定身份不一致")
                actions = [_prepared_action]
                logs = {_prepared_action.execute_tool_call_id: _locked_completion.log}
            elif has_protocol_marker(_prepared_action.execution_owner) or (
                _prepared_action.status in {"succeeded", "failed"}
                and _prepared_action.execute_tool_name in DB_TOOLS | FILE_TOOLS
            ):
                raise BusinessRuleException("完成投影缺少锁定事实")
        if (
            conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.user_id != tenant.actor_user_id
            or conversation.deleted_at is not None
            or source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.role != "user"
            or source.trace_id != trace_id
            or not source.is_active
        ):
            return None
        protected, unknown, factual_lineages = await self._protected_cards(
            db,
            conversation=conversation,
            source=source,
            actions=actions,
            logs=logs,
            tenant=tenant,
        )
        from app.modules.ai.agents.gateway.atomic_execution import (  # noqa: PLC0415
            OUTCOME_UNKNOWN_MESSAGE,
        )

        if unknown:
            content = reconciliation_notice(
                content,
                [
                    key
                    for key, card in protected.items()
                    if card is not None and "ok" not in card
                ],
            )
        elif protected and not unknown and OUTCOME_UNKNOWN_MESSAGE in content:
            content = (
                content.replace(OUTCOME_UNKNOWN_MESSAGE, "")
                .replace("（AI_EXECUTION_OUTCOME_UNKNOWN）", "")
                .strip()
            )
        tool_calls = self._replace_protected_cards(tool_calls, protected)
        for factual_lineage in factual_lineages:
            if lineage is None:
                lineage = factual_lineage
            elif lineage.agent_code == factual_lineage.agent_code:
                lineage = result_projection_service.freeze_lineage(
                    tenant=tenant,
                    agent_code=lineage.agent_code,
                    tool_codes=(*lineage.tool_codes, *factual_lineage.tool_codes),
                    subject_refs=(*lineage.subject_refs, *factual_lineage.subject_refs),
                    data_scope_hash=lineage.data_scope_hash
                    or factual_lineage.data_scope_hash,
                    projection_dependency_message_ids=(
                        *lineage.projection_dependency_message_ids,
                        *factual_lineage.projection_dependency_message_ids,
                    ),
                )

        stmt = (
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.conversation_id == conversation_id,
                AiMessage.role == "assistant",
                AiMessage.trace_id == trace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
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
                    tenant_id=tenant.tenant_id,
                    conversation_id=conversation_id,
                    parent_message_id=source_user_message_id,
                    role="assistant",
                    message_type="text",
                    content=content,
                    tool_calls=tool_calls or None,
                    trace_id=trace_id,
                    agent_code=agent_code,
                    tool_codes=list(lineage.tool_codes) if lineage else None,
                    subject_refs=list(lineage.subject_refs) if lineage else None,
                    subject_refs_hash=lineage.subject_refs_hash if lineage else None,
                    data_scope_hash=lineage.data_scope_hash if lineage else None,
                    resolver_version=lineage.resolver_version if lineage else None,
                    projection_dependency_message_ids=[
                        str(value) for value in projection_dependency_message_ids
                    ],
                    is_active=True,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "conversation_id", "trace_id"],
                    index_where=text("role = 'assistant' AND trace_id IS NOT NULL"),
                )
            )
            await db.execute(create_stmt)
            message = (
                (
                    await db.execute(
                        select(AiMessage)
                        .where(
                            AiMessage.tenant_id == tenant.tenant_id,
                            AiMessage.conversation_id == conversation_id,
                            AiMessage.role == "assistant",
                            AiMessage.trace_id == trace_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .one()
            )
        if lineage is not None:
            existing_lineage = result_projection_service.lineage_from_record(message)
            if existing_lineage is not None:
                if (
                    existing_lineage.agent_code != lineage.agent_code
                    or existing_lineage.resolver_version != lineage.resolver_version
                    or (
                        existing_lineage.data_scope_hash is not None
                        and lineage.data_scope_hash is not None
                        and existing_lineage.data_scope_hash != lineage.data_scope_hash
                    )
                ):
                    raise RuntimeError("incompatible incremental projection lineage")
                lineage = result_projection_service.freeze_lineage(
                    tenant=tenant,
                    agent_code=lineage.agent_code,
                    tool_codes=(*existing_lineage.tool_codes, *lineage.tool_codes),
                    subject_refs=(
                        *existing_lineage.subject_refs,
                        *lineage.subject_refs,
                    ),
                    data_scope_hash=(
                        existing_lineage.data_scope_hash or lineage.data_scope_hash
                    ),
                    resolver_version=lineage.resolver_version,
                    projection_dependency_message_ids=(
                        *existing_lineage.projection_dependency_message_ids,
                        *lineage.projection_dependency_message_ids,
                    ),
                )
        if (
            protected
            and not unknown
            and OUTCOME_UNKNOWN_MESSAGE in (message.content or "")
        ):
            message.content = (
                message.content.replace(OUTCOME_UNKNOWN_MESSAGE, "")
                .replace("（AI_EXECUTION_OUTCOME_UNKNOWN）", "")
                .strip()
            )
        if not message.content and content:
            message.content = content
        elif unknown:
            message.content = reconciliation_notice(
                message.content,
                [
                    key
                    for key, card in protected.items()
                    if card is not None and "ok" not in card
                ],
            )
        message.tool_calls = self._merge_tool_calls(
            self._replace_protected_cards(message.tool_calls, protected),
            tool_calls,
        )
        if message.parent_message_id is None:
            message.parent_message_id = source_user_message_id
        if message.agent_code is None:
            message.agent_code = agent_code
        if lineage is not None:
            message.tenant_id = lineage.tenant_id
            message.tool_codes = list(lineage.tool_codes)
            message.subject_refs = list(lineage.subject_refs)
            message.subject_refs_hash = lineage.subject_refs_hash
            message.data_scope_hash = lineage.data_scope_hash
            message.resolver_version = lineage.resolver_version
            message.projection_dependency_message_ids = [
                str(value) for value in lineage.projection_dependency_message_ids
            ]
        if message.projection_dependency_message_ids is None:
            message.projection_dependency_message_ids = [
                str(value) for value in projection_dependency_message_ids
            ]
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
        tenant: TenantContext,
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
            tenant=tenant,
        )

    async def finalize_prepared_action(
        self,
        db: AsyncSession,
        *,
        action: AiPreparedAction,
        ok: bool,
        duration_ms: int = 0,
        result: Any = None,
        result_ui: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_msg: str | None = None,
        tenant: TenantContext,
        locked_completion: LockedCompletion | LockedClaim | None = None,
    ) -> AiMessage | None:
        """Project one durable authorization without exposing frozen capability data."""
        from app.modules.ai.agents.tools.registry import ToolRegistry  # noqa: PLC0415

        source = ToolRegistry.get().prepared_source_for(action.execute_tool_name)
        presentation = stringify_large_ints(action.presentation)
        title = str(action.presentation.get("title") or action.execute_tool_name)
        cards: list[dict[str, Any]] = []
        if action.prepare_tool_call_id:
            cards.append(
                {
                    "tool": source.meta.name if source else "prepared.preview",
                    "tool_call_id": action.prepare_tool_call_id,
                    "summary": title,
                    "args": {},
                    "risk": source.meta.risk if source else "low",
                    "trace_id": action.trace_id,
                    "chip_target": source.meta.chip_target if source else None,
                    "ok": True,
                    "result": presentation,
                    "affected_rows": None,
                    "error_code": None,
                    "error_msg": None,
                    "duration_ms": 0,
                    "ui": None,
                }
            )
        cards.append(
            {
                "tool": action.execute_tool_name,
                "tool_call_id": action.execute_tool_call_id,
                "summary": title,
                "args": presentation,
                "risk": action.risk_level,
                "trace_id": action.trace_id,
                "chip_target": action.chip_target,
                "ok": ok,
                "result": stringify_large_ints(result),
                "affected_rows": None,
                "error_code": error_code,
                "error_msg": error_msg,
                "duration_ms": duration_ms,
                "ui": stringify_large_ints(result_ui),
            }
        )
        return await self.finalize_assistant_turn(
            db,
            conversation_id=action.conversation_id,
            trace_id=action.trace_id,
            source_user_message_id=action.source_user_message_id,
            content="",
            tool_calls=cards,
            agent_code=action.agent_code,
            lineage=result_projection_service.lineage_from_record(action),
            tenant=tenant,
            _prepared_action=action,
            _locked_completion=locked_completion,
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
