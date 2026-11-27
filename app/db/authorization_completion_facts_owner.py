"""Fixed, bounded RR READ ONLY completion facts; never grants execution authority."""

import asyncio
import logging
from collections.abc import Iterable

from sqlalchemy import and_, case, or_, select, text

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.db.authorization_owner import (
    authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_system_owner import require_candidate_session
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.completion_read_state_service import (
    RECONCILIATION_COMPLETION,
    UNKNOWN_COMPLETION,
    EffectiveCompletion,
    completion_read_state_service,
)
from app.modules.ai.service.reconciliation_annotation import has_annotation

logger = logging.getLogger(__name__)
_READ_TIMEOUT_SEC = 8


class CompletionFactsOwner:
    def __init__(self, factory):
        self.factory = factory

    async def read(
        self,
        *,
        tenant: TenantContext,
        tool_call_ids: Iterable[str] | None = None,
        conversation_id: int | None = None,
        user_id: int | None = None,
    ) -> dict[str, EffectiveCompletion]:
        if tool_call_ids is None and (conversation_id is None or user_id is None):
            raise ValueError("completion reads require IDs or an owned conversation")
        identifiers_set = set()
        if isinstance(tool_call_ids, (str, bytes)):
            return {"": UNKNOWN_COMPLETION}
        for count, value in enumerate(tool_call_ids or (), 1):
            if count > 256 or not isinstance(value, str) or len(value) > 256:
                return {"": UNKNOWN_COMPLETION}
            if value:
                identifiers_set.add(value)
        identifiers = tuple(sorted(identifiers_set))
        if tool_call_ids is not None and not identifiers:
            return {}
        log_scope = [AiOperationLog.tenant_id == tenant.tenant_id]
        action_scope = [AiPreparedAction.tenant_id == tenant.tenant_id]
        if tool_call_ids is not None:
            log_scope.append(AiOperationLog.tool_call_id.in_(identifiers))
            action_scope.append(AiPreparedAction.execute_tool_call_id.in_(identifiers))
        if conversation_id is not None:
            log_scope.append(AiOperationLog.conversation_id == conversation_id)
            action_scope.append(AiPreparedAction.conversation_id == conversation_id)
        if user_id is not None:
            log_scope.append(AiOperationLog.user_id == user_id)
            action_scope.append(AiPreparedAction.user_id == user_id)
        statement = (
            select(AiPreparedAction, AiOperationLog, AiConversation, AiMessage)
            .select_from(AiOperationLog)
            .join(
                AiPreparedAction,
                and_(
                    AiPreparedAction.tenant_id == AiOperationLog.tenant_id,
                    AiPreparedAction.execute_tool_call_id
                    == AiOperationLog.tool_call_id,
                ),
                full=True,
            )
            .outerjoin(
                AiConversation,
                and_(
                    AiConversation.tenant_id
                    == case(
                        (
                            AiPreparedAction.tenant_id.is_not(None),
                            AiPreparedAction.tenant_id,
                        ),
                        else_=AiOperationLog.tenant_id,
                    ),
                    AiConversation.conversation_id
                    == case(
                        (
                            AiPreparedAction.conversation_id.is_not(None),
                            AiPreparedAction.conversation_id,
                        ),
                        else_=AiOperationLog.conversation_id,
                    ),
                ),
            )
            .outerjoin(
                AiMessage,
                and_(
                    AiMessage.tenant_id
                    == case(
                        (
                            AiPreparedAction.tenant_id.is_not(None),
                            AiPreparedAction.tenant_id,
                        ),
                        else_=AiOperationLog.tenant_id,
                    ),
                    AiMessage.message_id
                    == case(
                        (
                            AiPreparedAction.source_user_message_id.is_not(None),
                            AiPreparedAction.source_user_message_id,
                        ),
                        else_=AiOperationLog.source_user_message_id,
                    ),
                ),
            )
            .where(or_(and_(*log_scope), and_(*action_scope)))
            .execution_options(populate_existing=True)
        )
        try:
            async with (
                asyncio.timeout(_READ_TIMEOUT_SEC),
                authorization_session(self.factory) as db,
            ):
                try:
                    require_candidate_session(db)
                    journal = db._authorization_journal
                    journal.phase = "active"
                    journal.deadline = (
                        asyncio.get_running_loop().time() + _READ_TIMEOUT_SEC
                    )
                    permit = object()
                    journal.permit = permit
                    await db.execute(
                        text(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        ).execution_options(_authorization_plan_permit=permit)
                    )
                    journal.permit = None
                    rows = (await db.execute(statement.limit(16385))).all()
                    if len(rows) > 16384:
                        return {"": UNKNOWN_COMPLETION}
                    from app.modules.system.service.authorization_read_budget import (  # noqa: PLC0415
                        _size,
                    )

                    if sum(_size(row) for row in rows) > 64 * 1024 * 1024:
                        return {"": UNKNOWN_COMPLETION}
                    projected = {}
                    for action, log, conversation, source in rows:
                        if log is not None and has_annotation(log.result_summary):
                            projected[log.tool_call_id] = RECONCILIATION_COMPLETION
                            continue
                        if not completion_read_state_service._protected(action, log):
                            continue
                        call_id = (
                            action.execute_tool_call_id if action else log.tool_call_id
                        )
                        if action is None:
                            from app.modules.ai.service.preflight_completion_service import (  # noqa: PLC0415
                                is_verified_preflight,
                            )

                            if await is_verified_preflight(
                                db,
                                log,
                                conversation,
                                source,
                                tenant_id=tenant.tenant_id,
                                user_id=log.user_id,
                            ):
                                projected[call_id] = EffectiveCompletion(
                                    log.status,
                                    log.error_code,
                                    log.finished_at,
                                    log.duration_ms,
                                    verified=True,
                                )
                                continue
                        if action is None and log.tool_name == "user.import_preview":
                            from app.modules.ai.service.preview_completion_service import (  # noqa: PLC0415
                                verify_historical_preview,
                            )

                            try:
                                await verify_historical_preview(
                                    db,
                                    log,
                                    conversation,
                                    source,
                                    tenant_id=tenant.tenant_id,
                                    user_id=log.user_id,
                                )
                                projected[call_id] = EffectiveCompletion(
                                    log.status,
                                    log.error_code,
                                    log.finished_at,
                                    log.duration_ms,
                                    verified=True,
                                )
                            except (BusinessRuleException, TypeError, ValueError):
                                projected[call_id] = UNKNOWN_COMPLETION
                        else:
                            projected[call_id] = completion_read_state_service._project(
                                action, log, conversation, source, tenant=tenant
                            )
                    return projected
                finally:
                    await rollback_authorization_session(db)
        except (Exception, asyncio.CancelledError):
            logger.warning("completion read snapshot unavailable", exc_info=True)
            # Do not expose an unverified raw terminal after a read failure.
            # A scope-only reader uses the values to block admission; the empty
            # internal key never becomes a public tool-call identifier.
            return {"": UNKNOWN_COMPLETION}
