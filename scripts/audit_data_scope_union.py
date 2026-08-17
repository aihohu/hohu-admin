# ruff: noqa: T201
"""Audit legacy and union data scopes without mutating authorization facts."""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload, sessionmaker

from app.constants import STATUS_ENABLED
from app.core.config import settings
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.base import role_depts, user_depts, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import (
    authorization_lock_service,
)
from app.utils.data_scope import (
    DATA_SCOPE_UNION_RESOLVER_VERSION,
    DataScopeResolution,
    resolve_data_scope,
    resolve_legacy_ai_data_scope,
    resolve_legacy_data_scope,
)

REPORT_SCHEMA_VERSION = 2
UNACKNOWLEDGED_EXPANSION_EXIT_CODE = 2
_BUILD_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{7,64}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _string_ids(values: set[int] | frozenset[int]) -> list[str]:
    return [str(value) for value in sorted(values)]


@dataclass(frozen=True)
class ScopeUnionAuditReport:
    payload: dict[str, Any]
    report_sha256: str
    expansion_count: int

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload, "reportSha256": self.report_sha256}


async def _materialize_user_ids(
    db: AsyncSession,
    resolution: DataScopeResolution,
    all_user_ids: frozenset[int],
) -> frozenset[int]:
    if resolution.unbounded:
        return all_user_ids
    assert resolution.accessible_user_scope is not None
    return frozenset(
        int(user_id)
        for user_id in (await db.execute(resolution.accessible_user_scope)).scalars()
    )


def _materialize_dept_ids(
    resolution: DataScopeResolution,
    all_dept_ids: frozenset[int],
) -> frozenset[int]:
    if resolution.unbounded:
        return all_dept_ids
    return resolution.accessible_dept_ids or frozenset()


async def _authorization_versions(db: AsyncSession) -> dict[str, str]:
    role_rows = (
        await db.execute(
            select(
                Role.role_id,
                Role.role_code,
                Role.status,
                Role.data_scope,
            ).order_by(Role.role_id)
        )
    ).all()
    role_dept_rows = (
        await db.execute(
            select(role_depts.c.role_id, role_depts.c.dept_id).order_by(
                role_depts.c.role_id,
                role_depts.c.dept_id,
            )
        )
    ).all()
    user_rows = (
        await db.execute(select(User.user_id, User.status).order_by(User.user_id))
    ).all()
    user_role_rows = (
        await db.execute(
            select(user_roles.c.user_id, user_roles.c.role_id).order_by(
                user_roles.c.user_id,
                user_roles.c.role_id,
            )
        )
    ).all()
    user_dept_rows = (
        await db.execute(
            select(
                user_depts.c.user_id,
                user_depts.c.dept_id,
                user_depts.c.is_primary,
            ).order_by(
                user_depts.c.user_id,
                user_depts.c.dept_id,
            )
        )
    ).all()
    dept_rows = (
        await db.execute(
            select(
                Dept.dept_id,
                Dept.parent_id,
                Dept.ancestors,
                Dept.status,
            ).order_by(Dept.dept_id)
        )
    ).all()
    return {
        "roles": _digest(
            {
                "roles": [list(row) for row in role_rows],
                "roleDepartments": [list(row) for row in role_dept_rows],
            }
        ),
        "memberships": _digest(
            {
                "users": [list(row) for row in user_rows],
                "userRoles": [list(row) for row in user_role_rows],
                "userDepartments": [list(row) for row in user_dept_rows],
            }
        ),
        "departments": _digest([list(row) for row in dept_rows]),
    }


def _scope_summary(
    resolution: DataScopeResolution,
    dept_ids: frozenset[int],
    user_ids: frozenset[int],
) -> dict[str, Any]:
    return {
        "scopeKinds": sorted(resolution.scope_kinds),
        "unbounded": resolution.unbounded,
        "departmentCount": len(dept_ids),
        "departmentDigest": _digest(_string_ids(dept_ids)),
        "userCount": len(user_ids),
        "userDigest": _digest(_string_ids(user_ids)),
    }


async def build_scope_union_report(
    db: AsyncSession,
    *,
    build_sha: str,
) -> ScopeUnionAuditReport:
    """Build a canonical report from the caller's consistent read snapshot."""
    users = (
        (
            await db.execute(
                select(User)
                .where(User.status == STATUS_ENABLED)
                .options(selectinload(User.roles), selectinload(User.depts))
                .order_by(User.user_id)
            )
        )
        .unique()
        .scalars()
        .all()
    )
    principals = [
        user
        for user in users
        if len([role for role in user.roles if role.status == STATUS_ENABLED]) >= 2
    ]
    all_user_ids = frozenset(
        int(user_id) for user_id in (await db.execute(select(User.user_id))).scalars()
    )
    all_dept_ids = frozenset(
        int(dept_id) for dept_id in (await db.execute(select(Dept.dept_id))).scalars()
    )

    principal_reports: list[dict[str, Any]] = []
    expansion_count = 0
    for principal in principals:
        legacy_api = await resolve_legacy_data_scope(db, principal)
        legacy_ai = await resolve_legacy_ai_data_scope(db, principal)
        union_scope = await resolve_data_scope(db, principal)
        legacy_api_dept_ids = _materialize_dept_ids(legacy_api, all_dept_ids)
        legacy_ai_dept_ids = _materialize_dept_ids(legacy_ai, all_dept_ids)
        union_dept_ids = _materialize_dept_ids(union_scope, all_dept_ids)
        legacy_api_user_ids = await _materialize_user_ids(
            db,
            legacy_api,
            all_user_ids,
        )
        legacy_ai_user_ids = await _materialize_user_ids(
            db,
            legacy_ai,
            all_user_ids,
        )
        union_user_ids = await _materialize_user_ids(
            db,
            union_scope,
            all_user_ids,
        )
        added_from_api_dept_ids = union_dept_ids - legacy_api_dept_ids
        added_from_api_user_ids = union_user_ids - legacy_api_user_ids
        added_from_ai_dept_ids = union_dept_ids - legacy_ai_dept_ids
        added_from_ai_user_ids = union_user_ids - legacy_ai_user_ids
        expanded = bool(
            added_from_api_dept_ids
            or added_from_api_user_ids
            or added_from_ai_dept_ids
            or added_from_ai_user_ids
        )
        expansion_count += int(expanded)
        principal_reports.append(
            {
                "userId": str(principal.user_id),
                "roleCodes": sorted(
                    role.role_code
                    for role in principal.roles
                    if role.status == STATUS_ENABLED
                ),
                "legacyApi": _scope_summary(
                    legacy_api,
                    legacy_api_dept_ids,
                    legacy_api_user_ids,
                ),
                "legacyAi": _scope_summary(
                    legacy_ai,
                    legacy_ai_dept_ids,
                    legacy_ai_user_ids,
                ),
                "union": _scope_summary(
                    union_scope,
                    union_dept_ids,
                    union_user_ids,
                ),
                "addedFromLegacyApiDeptIds": _string_ids(added_from_api_dept_ids),
                "addedFromLegacyApiUserIds": _string_ids(added_from_api_user_ids),
                "addedFromLegacyAiDeptIds": _string_ids(added_from_ai_dept_ids),
                "addedFromLegacyAiUserIds": _string_ids(added_from_ai_user_ids),
            }
        )

    payload: dict[str, Any] = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "tenantId": str(DEFAULT_TENANT_ID),
        "resolverVersion": DATA_SCOPE_UNION_RESOLVER_VERSION,
        "buildSha": build_sha,
        "versions": await _authorization_versions(db),
        "principalCount": len(principal_reports),
        "expansionCount": expansion_count,
        "principals": principal_reports,
    }
    return ScopeUnionAuditReport(
        payload=payload,
        report_sha256=_digest(payload),
        expansion_count=expansion_count,
    )


def verify_scope_union_ack(
    report: ScopeUnionAuditReport,
    acknowledged_sha256: str | None,
) -> bool:
    """Require an exact lowercase SHA-256 acknowledgement."""
    if acknowledged_sha256 is None or len(acknowledged_sha256) != 64:
        return False
    return hmac.compare_digest(report.report_sha256, acknowledged_sha256)


def audit_exit_code(
    report: ScopeUnionAuditReport,
    *,
    acknowledged_sha256: str | None = None,
) -> int:
    if report.expansion_count == 0:
        return 0
    if verify_scope_union_ack(report, acknowledged_sha256):
        return 0
    return UNACKNOWLEDGED_EXPANSION_EXIT_CODE


def release_gate_exit_code(
    report: ScopeUnionAuditReport,
    acknowledged_sha256: str | None,
) -> int:
    """Require exact acknowledgement even when the report has no expansion."""
    if verify_scope_union_ack(report, acknowledged_sha256):
        return 0
    return UNACKNOWLEDGED_EXPANSION_EXIT_CODE


def write_protected_report(
    output_path: Path,
    report: ScopeUnionAuditReport,
) -> None:
    """Create a private report file and refuse accidental overwrite."""
    payload = json.dumps(
        report.as_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(output_path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.write(b"\n")


def _current_build_sha() -> str:
    configured = os.environ.get("BUILD_SHA") or os.environ.get("GITHUB_SHA")
    if configured is None:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        configured = completed.stdout.strip()
    if _BUILD_SHA_PATTERN.fullmatch(configured) is None:
        raise RuntimeError("BUILD_SHA must be a 7-64 character hexadecimal SHA")
    return configured.lower()


def _parse_command(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("command must be a JSON string array") from exc
    if (
        not isinstance(parsed, list)
        or not parsed
        or any(not isinstance(item, str) or not item for item in parsed)
    ):
        raise argparse.ArgumentTypeError(
            "command must be a non-empty JSON array of non-empty strings"
        )
    return tuple(parsed)


async def _run_controlled_command(command: tuple[str, ...]) -> None:
    await asyncio.to_thread(subprocess.run, list(command), check=True)


@asynccontextmanager
async def _hold_authorization_migration(connection: AsyncConnection):
    await authorization_lock_service.lock_authorization_migration_session(connection)
    await connection.commit()
    try:
        yield
    finally:
        await authorization_lock_service.unlock_authorization_migration_session(
            connection
        )
        await connection.commit()


async def _run_locked_release(
    connection: AsyncConnection,
    *,
    maintenance_command: tuple[str, ...],
    switch_command: tuple[str, ...],
    audit_callback: Callable[[], Awaitable[tuple[ScopeUnionAuditReport, int]]],
) -> tuple[ScopeUnionAuditReport, int]:
    """Keep one session lock through maintenance, audit, and activation."""
    async with _hold_authorization_migration(connection):
        await _run_controlled_command(maintenance_command)
        report, exit_code = await audit_callback()
        if exit_code == 0:
            await _run_controlled_command(switch_command)
        return report, exit_code


async def _run_audit(args: argparse.Namespace) -> int:
    engine = create_async_engine(
        settings.DATABASE_URL,
        isolation_level="REPEATABLE READ",
    )
    async_session = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:

        async def audit_once(db: AsyncSession) -> tuple[ScopeUnionAuditReport, int]:
            async with db.begin():
                await db.execute(text("SET TRANSACTION READ ONLY"))
                current_report = await build_scope_union_report(
                    db,
                    build_sha=_current_build_sha(),
                )
                write_protected_report(args.output, current_report)
                current_exit_code = (
                    release_gate_exit_code(
                        current_report,
                        os.environ.get("DATA_SCOPE_UNION_ACK_SHA256"),
                    )
                    if args.verify_ack
                    else audit_exit_code(current_report)
                )
            return current_report, current_exit_code

        if args.verify_ack:
            async with engine.connect() as connection:

                async def locked_audit() -> tuple[ScopeUnionAuditReport, int]:
                    async with AsyncSession(
                        bind=connection,
                        expire_on_commit=False,
                    ) as db:
                        return await audit_once(db)

                report, exit_code = await _run_locked_release(
                    connection,
                    maintenance_command=args.maintenance_command,
                    switch_command=args.switch_command,
                    audit_callback=locked_audit,
                )
        else:
            async with async_session() as db:
                report, exit_code = await audit_once(db)
        print(f"scope-diff report SHA-256: {report.report_sha256}")
        if report.expansion_count:
            print(
                "data-scope expansions require exact controlled acknowledgement: "
                f"{report.expansion_count} principal(s)"
            )
        return exit_code
    finally:
        await engine.dispose()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit legacy and union data scopes in a read-only snapshot."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--verify-ack",
        action="store_true",
        help=(
            "Hold the migration lock through maintenance and activation, then "
            "verify DATA_SCOPE_UNION_ACK_SHA256."
        ),
    )
    parser.add_argument(
        "--maintenance-command",
        type=_parse_command,
        help="JSON argv that stops authorization writers after the lock is held.",
    )
    parser.add_argument(
        "--switch-command",
        type=_parse_command,
        help="JSON argv that activates and verifies the audited build.",
    )
    args = parser.parse_args(argv)
    release_commands = (args.maintenance_command, args.switch_command)
    if args.verify_ack and any(command is None for command in release_commands):
        parser.error("--verify-ack requires --maintenance-command and --switch-command")
    if not args.verify_ack and any(command is not None for command in release_commands):
        parser.error("release commands require --verify-ack")
    return args


def main() -> None:
    raise SystemExit(asyncio.run(_run_audit(_parse_args())))


if __name__ == "__main__":
    main()
