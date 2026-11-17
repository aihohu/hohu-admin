"""Minimal local security facts, written only outside authorization transactions."""

import json
import os
import re
from datetime import UTC, datetime


def record_event(directory, operation, code, identity):
    if operation not in {
        "login",
        "logout",
        "finish",
        "catalog",
        "roles",
        "object",
        "manifest",
        "receipt",
        "preview",
        "publish",
        "manifest-preview",
        "build",
        "freeze",
        "ready",
        "activate",
    }:
        operation = "unknown"
    if not re.fullmatch(r"[A-Za-z_]{1,96}", code or ""):
        code = "FIELD_POLICY_UNAVAILABLE"
    raw = (
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "operation": operation,
                "code": code,
                "tenantId": str(identity.tenant.tenant_id) if identity else None,
                "actorId": str(identity.tenant.actor_user_id) if identity else None,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if len(raw) > 1024:
        raise ValueError("Preparation event exceeds budget")
    # Controller serializes all appends; control-dir is operator-owned, like stop.json.
    with (directory / "policy-events.jsonl").open("ab") as output:
        if output.tell() + len(raw) > 8 * 1024 * 1024:
            raise ValueError("Preparation event log exceeds budget")
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
