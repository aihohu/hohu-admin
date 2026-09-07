"""Generate a clean-checkout, read-only hosted canary preflight report."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tenant_canary import build_canary_preflight_report
from app.db.session import engine

RISK_EXIT_CODE = 2
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check one hosted tenant canary without modifying application data."
    )
    parser.add_argument("--build-sha", required=True)
    parser.add_argument(
        "--phase",
        required=True,
        choices=("pre_activation", "post_activation"),
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _verified_build_sha(expected: str) -> str:
    normalized_expected = expected.strip().lower()
    if _BUILD_SHA_RE.fullmatch(normalized_expected) is None:
        raise ValueError("build SHA must be a full 40-character Git SHA")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    worktree = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    if worktree.stdout.strip():
        raise ValueError("canary preflight requires a clean Git checkout")
    actual = revision.stdout.strip().lower()
    if actual != normalized_expected:
        raise ValueError("build SHA does not match the checked-out source")
    if settings.RELEASE_BUILD_SHA and settings.RELEASE_BUILD_SHA != actual:
        raise ValueError("release build SHA does not match the checked-out source")
    return actual


async def _build(build_sha: str, phase: str):
    target_tenant_id = settings.TENANT_HOSTED_CANARY_TENANT_ID
    if (
        settings.TENANT_MODE != "hosted"
        or not settings.TENANT_HOSTED_LOGIN_ENABLED
        or target_tenant_id is None
    ):
        raise ValueError("hosted canary runtime configuration is incomplete")
    async with engine.connect() as connection:
        connection = await connection.execution_options(
            isolation_level="REPEATABLE READ"
        )
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            async with AsyncSession(bind=connection) as session:
                return await build_canary_preflight_report(
                    session,
                    build_sha=build_sha,
                    phase=phase,  # type: ignore[arg-type]
                    target_tenant_id=target_tenant_id,
                )


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    arguments = _arguments()
    try:
        report = asyncio.run(
            _build(_verified_build_sha(arguments.build_sha), arguments.phase)
        )
        _write_report(arguments.output, report.as_dict())
    except SQLAlchemyError:
        raise SystemExit("CANARY_PREFLIGHT_DATABASE_ERROR") from None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise SystemExit(f"CANARY_PREFLIGHT_INVALID: {error}") from None
    if report.risk_count:
        raise SystemExit(RISK_EXIT_CODE)


if __name__ == "__main__":
    main()
