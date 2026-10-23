"""Explicit-ID legacy inventory and additive annotations; caller owns commit."""

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select

from app.core.auth import has_explicit_permission
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.rbac import is_super_admin
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.reconciliation_annotation import (
    MARKER,
    REASONS,
    has_annotation,
    read_annotation,
    reconciliation_notice,
)
from app.modules.auth.service import load_live_user_authority


def _invalid():
    return BusinessRuleException(
        "核对清单或绑定已变化，请重新只读核查",
        error_code="AI_RECONCILIATION_QUARANTINED",
    )


def _snapshot(row):
    if row is None:
        return None
    return {col.key: getattr(row, col.key) for col in row.__table__.columns}


def _digest(snapshots):
    return sha256(
        json.dumps(snapshots, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


class LegacyReconciliationService:
    async def lock_inventory(self, db, *, tenant, manifest):
        """Lock the complete multi-context plan before touching any later tier."""
        await self._authorize(db, tenant, write=True)
        await db.execute(select(func.set_config("lock_timeout", "2s", True)))
        for model, key, values in (
            (
                AiConversation,
                AiConversation.conversation_id,
                [int(item["conversationId"]) for item in manifest],
            ),
            (
                AiMessage,
                AiMessage.message_id,
                [int(item["sourceMessageId"]) for item in manifest],
            ),
            (
                AiPreparedAction,
                AiPreparedAction.action_id,
                [int(item["actionId"]) for item in manifest if item["actionId"]],
            ),
            (
                AiOperationLog,
                AiOperationLog.log_id,
                [int(item["logId"]) for item in manifest],
            ),
        ):
            await db.execute(
                select(model)
                .where(model.tenant_id == tenant.tenant_id, key.in_(values))
                .order_by(key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )

    async def _authorize(self, db, tenant, *, write=False):
        user = await load_live_user_authority(db, tenant=tenant)
        if not is_super_admin(user) and (
            write or not has_explicit_permission(user, "ai:trace:view")
        ):
            raise AuthorizationException("需要当前租户的核对权限")

    async def _facts(self, db, tenant, log_id, *, lock=False):
        if lock:
            await db.execute(select(func.set_config("lock_timeout", "2s", True)))
            # Existing lock protocol is C -> source -> PA -> log, with revalidation.
            log = await operation_log_service._get(db, log_id, tenant=tenant)
        else:
            log = await db.scalar(
                select(AiOperationLog)
                .where(
                    AiOperationLog.tenant_id == tenant.tenant_id,
                    AiOperationLog.log_id == log_id,
                )
                .execution_options(populate_existing=True)
            )
        if log is None:
            return None
        conversation = await db.scalar(
            select(AiConversation).where(
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.conversation_id == log.conversation_id,
            )
        )
        source = await db.scalar(
            select(AiMessage).where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.message_id == log.source_user_message_id,
            )
        )
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.execute_tool_call_id == log.tool_call_id,
            )
        )
        return log, conversation, source, action

    def _inventory(self, facts):
        if facts is None:
            return {"state": "missing"}
        log, conversation, source, action = facts
        snapshots = [_snapshot(row) for row in facts]
        baseline = _digest(snapshots)
        output = {
            "state": "ready",
            "baseline": baseline,
            "logId": str(log.log_id),
            "tenantId": str(log.tenant_id),
            "actorId": str(log.user_id),
            "conversationId": str(log.conversation_id),
            "sourceMessageId": str(log.source_user_message_id)
            if log.source_user_message_id
            else None,
            "actionId": str(action.action_id) if action else None,
            "toolCallId": log.tool_call_id,
            "traceId": log.trace_id,
            "originalStatus": log.status,
            "originalErrorCode": log.error_code
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", log.error_code or "")
            else None,
            "originalFinishedAt": log.finished_at.isoformat()
            if log.finished_at
            else None,
            "originalActionStatus": action.status if action else None,
        }
        valid = (
            conversation is not None
            and source is not None
            and conversation.user_id == log.user_id
            and source.conversation_id == log.conversation_id
            and source.role == "user"
            and source.trace_id == log.trace_id
            and not log.readonly_snapshot
            and bool(
                log.trace_id
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", log.tool_call_id or "")
            )
            and (
                log.status == "running"
                or (
                    log.status == "failed"
                    and log.error_code
                    in {
                        "AI_INTERNAL_ERROR",
                        "AI_EXECUTION_INTERRUPTED",
                        "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED",
                        "AI_HITL_EXECUTION_INTERRUPTED",
                    }
                )
            )
        )
        if action is not None:
            expected = {
                "user_id": log.user_id,
                "conversation_id": log.conversation_id,
                "source_user_message_id": log.source_user_message_id,
                "trace_id": log.trace_id,
                "confirmation_id": log.confirmation_id,
                "execute_tool_name": log.tool_name,
                "args_hash": log.args_hash,
                "agent_code": log.agent_code,
            }
            valid = valid and all(
                getattr(action, key) == value for key, value in expected.items()
            )
        # A missing PA is valid only for genuinely non-prepared legacy operations.
        elif log.confirmation_id is not None:
            valid = False
        if has_annotation(log.result_summary):
            try:
                root = read_annotation(log.result_summary)
                entry = root[MARKER]
                snapshots[0]["result_summary"] = root["previousSummary"]
                valid = (
                    valid
                    and entry["actionId"] == output["actionId"]
                    and entry["toolCallId"] == log.tool_call_id
                    and _digest(snapshots) == entry["history"][0]["baseline"]
                )
                output["state"] = "annotated"
                output["caseId"] = entry["caseId"]
            except BusinessRuleException:
                valid = False
        if not valid:
            output["state"] = "quarantined"
        return output

    async def inspect(self, db, *, tenant, log_id):
        await self._authorize(db, tenant)
        return self._inventory(await self._facts(db, tenant, log_id))

    async def annotate(
        self,
        db,
        *,
        tenant,
        log_id,
        baseline,
        reason,
        evidence_sha256,
        maintenance_sha256,
    ):
        await self._authorize(db, tenant, write=True)
        if reason not in REASONS or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (baseline, evidence_sha256, maintenance_sha256)
        ):
            raise _invalid()
        facts = await self._facts(db, tenant, log_id, lock=True)
        inventory = self._inventory(facts)
        if inventory["state"] not in {"ready", "annotated"}:
            raise _invalid()
        log, _, _, action = facts
        event = {
            "operatorId": str(tenant.actor_user_id),
            "reasonCode": reason,
            "evidenceSha256": evidence_sha256,
            "maintenanceSha256": maintenance_sha256,
            "baseline": baseline,
        }
        if inventory["state"] == "annotated":
            root = read_annotation(log.result_summary)
            entry = root[MARKER]
            if any(
                all(item[key] == value for key, value in event.items())
                for item in entry["history"]
            ):
                return {"caseId": entry["caseId"], "disposition": "unverified"}
            if len(entry["history"]) >= 32:
                raise _invalid()
        else:
            entry = {
                "caseId": str(uuid4()),
                "classification": "legacy_unverified",
                "reasonCode": reason,
                "actionId": str(action.action_id) if action else None,
                "toolCallId": log.tool_call_id,
                "disposition": "unverified",
                "history": [],
            }
            root = {MARKER: entry, "previousSummary": log.result_summary}
        if inventory["baseline"] != baseline:
            raise _invalid()
        entry["history"].append({**event, "at": datetime.now(UTC).isoformat()})
        # Preserve old assistant facts, adding only a minimal durable notice.
        # This also covers legacy interruption before assistant finalization.
        message = await db.scalar(
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.conversation_id == log.conversation_id,
                AiMessage.trace_id == log.trace_id,
                AiMessage.role == "assistant",
            )
            .with_for_update()
        )
        if (
            message is not None
            and message.parent_message_id != log.source_user_message_id
        ):
            raise _invalid()
        log.result_summary = json.dumps(root, ensure_ascii=False, separators=(",", ":"))
        if message is None:
            message = AiMessage(
                tenant_id=tenant.tenant_id,
                conversation_id=log.conversation_id,
                parent_message_id=log.source_user_message_id,
                role="assistant",
                trace_id=log.trace_id,
                is_active=facts[2].is_active,
                tool_calls=[],
            )
            db.add(message)
        message.content = reconciliation_notice(message.content, [log.tool_call_id])
        if not any(
            call.get("tool_call_id") == log.tool_call_id
            for call in message.tool_calls or []
        ):
            message.tool_calls = [
                *(message.tool_calls or []),
                {"tool": log.tool_name, "tool_call_id": log.tool_call_id},
            ]
        await db.flush()
        return {"caseId": entry["caseId"], "disposition": "unverified"}


legacy_reconciliation_service = LegacyReconciliationService()
