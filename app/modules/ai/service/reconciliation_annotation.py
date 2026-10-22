"""Server-only quarantine metadata. Presence is never completion evidence."""

import json
import re
from datetime import datetime
from uuid import UUID

from app.core.exceptions import BusinessRuleException

MARKER = "__v0162_reconciliation_v1"
UNKNOWN_CODE = "AI_EXECUTION_OUTCOME_UNKNOWN"
REASONS = {"LEGACY_RUNNING", "LEGACY_INTERRUPTED", "LEGACY_UNVERIFIED"}
NOTICE = "操作结果尚待核对，请勿重新办理；请刷新会话或联系管理员核对。"


def has_annotation(summary: str | None) -> bool:
    # Even broken JSON, an unknown disposition or extra keys must fail closed.
    return isinstance(summary, str) and MARKER in summary


def require_unannotated(summary: str | None) -> None:
    if has_annotation(summary):
        raise BusinessRuleException(NOTICE, error_code=UNKNOWN_CODE)


def read_annotation(summary: str) -> dict:
    """Strictly parse the one allowed envelope; never silently unwrap corruption."""

    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("duplicate annotation key")
            output[key] = value
        return output

    try:
        root = json.loads(summary, object_pairs_hook=unique)
        if not (isinstance(root, dict) and set(root) == {MARKER, "previousSummary"}):
            raise ValueError("invalid reconciliation envelope")
        if not (
            root["previousSummary"] is None or isinstance(root["previousSummary"], str)
        ):
            raise ValueError("invalid reconciliation envelope")
        if not (not has_annotation(root["previousSummary"])):
            raise ValueError("invalid reconciliation envelope")
        entry = root[MARKER]
        if not (
            isinstance(entry, dict)
            and set(entry)
            == {
                "caseId",
                "classification",
                "reasonCode",
                "actionId",
                "toolCallId",
                "disposition",
                "history",
            }
        ):
            raise ValueError("invalid reconciliation envelope")
        if not (str(UUID(entry["caseId"])) == entry["caseId"]):
            raise ValueError("invalid reconciliation envelope")
        if not (entry["classification"] == "legacy_unverified"):
            raise ValueError("invalid reconciliation envelope")
        if not (entry["disposition"] == "unverified"):
            raise ValueError("invalid reconciliation envelope")
        if entry["reasonCode"] not in REASONS:
            raise ValueError("invalid reconciliation envelope")
        if not (
            entry["actionId"] is None
            or re.fullmatch(r"[1-9][0-9]{0,19}", entry["actionId"])
        ):
            raise ValueError("invalid reconciliation envelope")
        if not (
            isinstance(entry["toolCallId"], str) and 0 < len(entry["toolCallId"]) <= 64
        ):
            raise ValueError("invalid reconciliation envelope")
        if not (
            isinstance(entry["history"], list) and 1 <= len(entry["history"]) <= 32
        ):
            raise ValueError("invalid reconciliation envelope")
        for item in entry["history"]:
            if not (
                set(item)
                == {
                    "operatorId",
                    "at",
                    "reasonCode",
                    "evidenceSha256",
                    "maintenanceSha256",
                    "baseline",
                }
            ):
                raise ValueError("invalid reconciliation envelope")
            if not (re.fullmatch(r"[1-9][0-9]{0,19}", item["operatorId"])):
                raise ValueError("invalid reconciliation envelope")
            if not (datetime.fromisoformat(item["at"]).utcoffset() is not None):
                raise ValueError("invalid reconciliation envelope")
            if item["reasonCode"] not in REASONS:
                raise ValueError("invalid reconciliation envelope")
            for key in ("evidenceSha256", "maintenanceSha256", "baseline"):
                if not (re.fullmatch(r"[0-9a-f]{64}", item[key])):
                    raise ValueError("invalid reconciliation envelope")
        return root
    except (AssertionError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise BusinessRuleException(
            "核对注解损坏，保留隔离并交由人工核查",
            error_code="AI_RECONCILIATION_QUARANTINED",
        ) from exc


def reconciliation_notice(content: str | None, tool_call_ids) -> str:
    """Use existing assistant text: old clients treat tool error codes as failure."""
    output = content or ""
    if NOTICE not in output:
        output = (output + "\n\n" + NOTICE).strip()
    for call_id in sorted(set(tool_call_ids)):
        if not isinstance(call_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", call_id
        ):
            continue
        line = f"[{UNKNOWN_CODE}] toolCallId={call_id}"
        if line not in output:
            output += "\n" + line
    return output
