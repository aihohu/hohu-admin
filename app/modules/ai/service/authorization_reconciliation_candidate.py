"""Fixed legacy evidence annotations under one complete context plan."""

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.legacy_reconciliation_service import (
    _invalid,
)
from app.modules.ai.service.legacy_reconciliation_service import (
    legacy_reconciliation_service as legacy,
)
from app.modules.ai.service.reconciliation_annotation import (
    MARKER,
    REASONS,
    read_annotation,
    reconciliation_notice,
)
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)


def validate_ids(values):
    if (
        type(values) not in {list, tuple}
        or not 1 <= len(values) <= 100
        or any(type(value) is not int or not 0 < value < 2**63 for value in values)
        or len(set(values)) != len(values)
    ):
        raise _invalid()
    return tuple(sorted(values))


class ReconciliationCandidate:
    async def execute(
        self, db, tx, *, tenant_id, operator_id, log_ids, annotation=None
    ):
        tx.assert_planning(tenant_id, exclusive=annotation is not None)
        target = await db.get(Tenant, tenant_id)
        user_version = await db.scalar(
            select(User.auth_version).where(
                User.tenant_id == tenant_id, User.user_id == operator_id
            )
        )
        if target is None or user_version is None:
            raise _invalid()
        tenant = TenantContext(
            tenant_id,
            target.tenant_code,
            operator_id,
            target.row_version,
            "access_token",
        )
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=user_version,
            permissions=("ai:trace:view",),
        )
        references = list(
            (
                await db.execute(
                    select(AiOperationLog.log_id, AiOperationLog.conversation_id).where(
                        AiOperationLog.tenant_id == tenant_id,
                        AiOperationLog.log_id.in_(log_ids),
                    )
                )
            ).all()
        )
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant_id,
            conversation_ids=tuple(sorted({row.conversation_id for row in references})),
        )
        # Reserve at most one new assistant per trace, before acquiring any row lock.
        messages, new = {}, {}
        for log_id, _conversation in references:
            facts = await legacy._facts(db, tenant, log_id)
            log = facts[0]
            rows = list(
                await db.scalars(
                    select(AiMessage).where(
                        AiMessage.tenant_id == tenant_id,
                        AiMessage.conversation_id == log.conversation_id,
                        AiMessage.trace_id == log.trace_id,
                        AiMessage.role == "assistant",
                    )
                )
            )
            if len(rows) > 1:
                raise _invalid()
            key = (log.conversation_id, log.trace_id)
            messages[key] = rows[0] if rows else None
            if annotation is not None and not rows:
                new.setdefault(
                    key, ResourceKey(K.ASSISTANT_MESSAGE, tenant_id, next_id())
                )
        await tx.lock(
            LockPlan(
                existing=tuple({*identity.keys, *inventory.keys}),
                new=tuple(NewResource(key) for key in new.values()),
            )
        )
        await assert_identity(db, identity)
        await ai_context_candidate.recheck(db, tx, inventory)
        await legacy._authorize(db, tenant, write=annotation is not None)
        results = []
        for log_id in log_ids:
            facts = await legacy._facts(db, tenant, log_id)
            if annotation is None:
                results.append(legacy._inventory(facts))
                continue
            baseline, reason, evidence, maintenance = annotation[log_id]
            result, summary = self._annotation(
                facts, tenant, baseline, reason, evidence, maintenance
            )
            results.append(result)
            if summary is None:
                continue
            log, _conversation, source, _action = facts
            key = (log.conversation_id, log.trace_id)
            message = messages[key]
            if message is not None and message.parent_message_id != source.message_id:
                raise _invalid()
            old_calls = message.tool_calls if message is not None else []
            calls = list(old_calls or [])
            if not any(call.get("tool_call_id") == log.tool_call_id for call in calls):
                calls.append({"tool": log.tool_name, "tool_call_id": log.tool_call_id})
            values = {
                "content": reconciliation_notice(
                    message.content if message else None, [log.tool_call_id]
                ),
                "tool_calls": calls,
            }
            await tx.update_factual(
                ResourceKey(K.OPERATION_LOG, tenant_id, log_id),
                {"result_summary": summary},
                expected={"result_summary": log.result_summary},
            )
            if message is None:
                await tx.insert_reserved(
                    new[key],
                    values
                    | {
                        "conversation_id": log.conversation_id,
                        "parent_message_id": source.message_id,
                        "trace_id": log.trace_id,
                        "role": "assistant",
                        "is_active": source.is_active,
                    },
                )
                messages[key] = await db.get(AiMessage, new[key].identity[0])
            else:
                await tx.update_factual(
                    ResourceKey(K.ASSISTANT_MESSAGE, tenant_id, message.message_id),
                    values,
                    expected={
                        "content": message.content,
                        "tool_calls": message.tool_calls,
                    },
                )
                # Later log annotations in this same trace append to the fresh facts.
                await db.refresh(message)
        return results

    @staticmethod
    def _annotation(facts, tenant, baseline, reason, evidence, maintenance):
        if reason not in REASONS or not all(
            type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (baseline, evidence, maintenance)
        ):
            raise _invalid()
        inventory = legacy._inventory(facts)
        if inventory["state"] not in {"ready", "annotated"}:
            raise _invalid()
        log, _, _, action = facts
        event = {
            "operatorId": str(tenant.actor_user_id),
            "reasonCode": reason,
            "evidenceSha256": evidence,
            "maintenanceSha256": maintenance,
            "baseline": baseline,
        }
        if inventory["state"] == "annotated":
            root = read_annotation(log.result_summary)
            entry = root[MARKER]
            if any(
                all(item[key] == value for key, value in event.items())
                for item in entry["history"]
            ):
                return {"caseId": entry["caseId"], "disposition": "unverified"}, None
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
        entry["history"].append(event | {"at": datetime.now(UTC).isoformat()})
        return {"caseId": entry["caseId"], "disposition": "unverified"}, json.dumps(
            root, ensure_ascii=False, separators=(",", ":")
        )


reconciliation_candidate = ReconciliationCandidate()
