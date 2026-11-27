"""Operator-owned build qualification, never accepted from the public API.

This is a deployment assertion, not a stop-writers or independent-test proof.
Direct and Compose deployments supply the same bounded, read-only JSON file.
"""

import hashlib
import json
import re
from pathlib import Path

from app.core.config import settings
from app.modules.system.field_policy.documents import catalog_digest

SURFACES = frozenset(
    {
        "rest",
        "auth",
        "relations",
        "query",
        "transfer",
        "ai",
        "history",
        "old-web",
        "concurrency",
    }
)
CLIENTS = frozenset({"web", "app", "desktop", "cli"})


def adapter_digest():
    root = Path(__file__).resolve().parents[4]
    digest = hashlib.sha256()
    for directory in ("app", "alembic/versions"):
        for path in sorted((root / directory).rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def qualification_blockers():
    if not settings.USER_FIELD_POLICY_AVAILABLE:
        return ("FIELD_POLICY_DEPLOYMENT_DISABLED",)
    try:
        with Path(settings.USER_FIELD_POLICY_QUALIFICATION_FILE).open("rb") as source:
            raw = source.read(32769)
        if len(raw) > 32768:
            raise ValueError
        value = json.loads(raw)
        if type(value) is not dict or set(value) != {
            "schema",
            "build",
            "catalogDigest",
            "adapterDigest",
            "surfaces",
            "clients",
            "evidenceDigest",
            "localCandidate",
        }:
            raise ValueError
        if (
            type(value["schema"]) is not int
            or value["schema"] != 1
            or value["build"] != settings.RELEASE_BUILD_SHA
            or not re.fullmatch(r"[0-9a-f]{40}", value["build"])
            or value["catalogDigest"] != catalog_digest()
            or value["adapterDigest"] != adapter_digest()
            or type(value["localCandidate"]) is not bool
            or (settings.ENV == "prod" and value["localCandidate"])
            or not isinstance(value["evidenceDigest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["evidenceDigest"])
            or type(value["surfaces"]) is not dict
            or set(value["surfaces"]) != SURFACES
            or any(status != "passed" for status in value["surfaces"].values())
            or type(value["clients"]) is not dict
            or set(value["clients"]) != CLIENTS
        ):
            raise ValueError
        for name, client in value["clients"].items():
            if (
                type(client) is not dict
                or set(client) != {"build", "status"}
                or not isinstance(client["build"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", client["build"])
                or client["status"]
                not in (
                    {"passed"}
                    if name == "web"
                    else {"passed", "no-user-management-surface"}
                )
            ):
                raise ValueError
    except (OSError, ValueError, TypeError):
        return ("FIELD_POLICY_QUALIFICATION_REQUIRED",)
    return ()
