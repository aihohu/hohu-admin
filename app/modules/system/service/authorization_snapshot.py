"""Canonical snapshots for materialized role-set authorization facts."""

import hashlib
import json
from typing import Any, Protocol


class RoleSetAuthoritySnapshotSource(Protocol):
    """Structural contract shared by authorization snapshot producers."""

    permission_codes: frozenset[str]
    menu_ids: frozenset[int]
    agent_ids: frozenset[int]
    accessible_dept_ids: frozenset[int] | None
    accessible_user_ids: frozenset[int] | None
    role_definition_signature: tuple[Any, ...]


def canonical_authorization_hash(payload: Any) -> str:
    """Hash one JSON-compatible payload with canonical encoding."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope_snapshot(values: frozenset[int] | None) -> dict[str, Any]:
    payload = {
        "unbounded": values is None,
        "ids": [] if values is None else sorted(values),
    }
    return {
        "unbounded": payload["unbounded"],
        "count": None if values is None else len(values),
        "hash": canonical_authorization_hash(payload),
    }


def materialized_role_set_snapshot(
    value: RoleSetAuthoritySnapshotSource,
) -> dict[str, Any]:
    """Return stable hashes for one materialized effective authorization set."""
    scope = {
        "departments": _scope_snapshot(value.accessible_dept_ids),
        "users": _scope_snapshot(value.accessible_user_ids),
    }
    authorization = {
        "permissionCodes": sorted(value.permission_codes),
        "menuIds": sorted(value.menu_ids),
        "agentIds": sorted(value.agent_ids),
        "scope": scope,
        "roleDefinitions": value.role_definition_signature,
    }
    return {
        "authorizationHash": canonical_authorization_hash(authorization),
        "scopeHash": canonical_authorization_hash(scope),
        "roleDefinitionHash": canonical_authorization_hash(
            value.role_definition_signature
        ),
    }
