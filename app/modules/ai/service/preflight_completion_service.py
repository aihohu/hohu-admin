"""Bound evidence of a dry-run refusal before confirmation or execution."""

import json
import re
from hashlib import sha256

from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.reconciliation_annotation import has_annotation

MARKER = "__v0162_preflight_v1"


def preflight_facts(log):
    """Bind evidence to immutable identity and the exact failed audit facts."""
    names = (
        "log_id",
        "tenant_id",
        "user_id",
        "conversation_id",
        "source_user_message_id",
        "trace_id",
        "tool_call_id",
        "tool_name",
        "agent_code",
        "args_hash",
        "readonly_snapshot",
        "status",
        "error_code",
        "execution_mode",
        "risk_level",
        "confirmation_id",
        "approved_by",
        "target_summary",
        "duration_ms",
        "hitl_wait_ms",
    )
    facts = {name: getattr(log, name) for name in names}
    facts["args_summary_sha256"] = sha256((log.args_summary or "").encode()).hexdigest()
    for name in ("queued_at", "started_at", "finished_at"):
        value = getattr(log, name)
        facts[name] = value.isoformat() if value is not None else None
    return facts


def runtime_preflight_summary(log):
    return json.dumps(
        {
            MARKER: {
                "phase": "dry_run",
                "origin": "runtime",
                "facts": preflight_facts(log),
            }
        },
        sort_keys=True,
    )


def preflight_fingerprint(log):
    return sha256(json.dumps(preflight_facts(log), sort_keys=True).encode()).hexdigest()


def has_preflight_marker(summary):
    return isinstance(summary, str) and MARKER in summary


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate preflight evidence key")
        value[key] = item
    return value


async def verify_preflight(db, log, conversation, source, *, tenant_id, user_id):
    """Never infer nonexecution from a raw error code or absent PA alone."""
    invalid = BusinessRuleException(
        "预检完成事实尚待核对", error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
    )
    registered = ToolRegistry.get().find(log.tool_name) if log is not None else None
    if (
        log is None
        or conversation is None
        or source is None
        or has_annotation(log.result_summary)
        or registered is None
        or registered.meta.readonly
        or not registered.meta.dry_run_supported
        or log.agent_code != registered.meta.agent
        or log.tenant_id != tenant_id
        or log.user_id != user_id
        or log.readonly_snapshot is not False
        or log.status != "failed"
        or not log.error_code
        or log.started_at is not None
        or log.confirmation_id is not None
        or log.approved_by is not None
        or log.target_summary is not None
        or log.hitl_wait_ms is not None
        or log.queued_at is None
        or log.finished_at is None
        or log.finished_at < log.queued_at
        or type(log.duration_ms) is not int
        or log.duration_ms < 0
        or not re.fullmatch(r"[0-9a-f]{64}", log.args_hash or "")
        or not log.trace_id
        or not log.tool_call_id
        or conversation.tenant_id != tenant_id
        or conversation.user_id != user_id
        or conversation.conversation_id != log.conversation_id
        or source.tenant_id != tenant_id
        or source.role != "user"
        or source.conversation_id != log.conversation_id
        or source.message_id != log.source_user_message_id
        or source.trace_id != log.trace_id
    ):
        raise invalid
    try:
        value = json.loads(log.result_summary, object_pairs_hook=_unique_object)
        proof = value[MARKER]
        if not isinstance(proof, dict) or set(value) != {MARKER}:
            raise invalid
        keys = {"phase", "origin", "facts"}
        if proof.get("origin") == "maintenance_attestation":
            keys |= {"attestation"}
            attestation = proof.get("attestation")
            if (
                not isinstance(attestation, dict)
                or set(attestation)
                != {
                    "operator",
                    "recorded_at",
                    "database_identity",
                    "source_sha256",
                    "evidence_sha256",
                    "original_fingerprint",
                    "previous_summary",
                }
                or not isinstance(attestation["operator"], str)
                or not attestation["operator"]
                or not isinstance(attestation["recorded_at"], str)
                or not attestation["recorded_at"]
                or not isinstance(attestation["database_identity"], str)
                or not attestation["database_identity"]
                or attestation["previous_summary"] is not None
                or attestation["original_fingerprint"] != preflight_fingerprint(log)
                or any(
                    not re.fullmatch(r"[0-9a-f]{64}", attestation.get(key) or "")
                    for key in ("source_sha256", "evidence_sha256")
                )
                or log.error_code != "AI_PREVIEW_REJECTED"
            ):
                raise invalid
        elif proof.get("origin") != "runtime":
            raise invalid
        if (
            set(proof) != keys
            or proof["phase"] != "dry_run"
            or proof["facts"] != preflight_facts(log)
        ):
            raise invalid
    except (TypeError, ValueError, KeyError) as exc:
        raise invalid from exc
    if (
        await db.scalar(
            select(AiPreparedAction.action_id).where(
                AiPreparedAction.tenant_id == tenant_id,
                AiPreparedAction.execute_tool_call_id == log.tool_call_id,
            )
        )
        is not None
    ):
        raise invalid


async def is_verified_preflight(db, log, conversation, source, *, tenant_id, user_id):
    try:
        await verify_preflight(
            db, log, conversation, source, tenant_id=tenant_id, user_id=user_id
        )
    except (BusinessRuleException, TypeError, ValueError):
        return False
    return True
