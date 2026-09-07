"""Emit the deterministic Plan 5-B-C tenant isolation release report."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_isolation_audit import build_tenant_isolation_report
from app.db.session import engine

RISK_EXIT_CODE = 2
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit tenant isolation without mutating application data."
    )
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


async def _build(build_sha: str):
    async with engine.connect() as connection:
        connection = await connection.execution_options(
            isolation_level="REPEATABLE READ"
        )
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            async with AsyncSession(bind=connection) as session:
                return await build_tenant_isolation_report(
                    session,
                    build_sha=build_sha,
                )


def _verified_build_sha(expected: str) -> str:
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
        raise ValueError("release audit requires a clean Git checkout")
    actual = revision.stdout.strip().lower()
    if not actual or not actual.startswith(expected.lower()):
        raise ValueError("build SHA does not match the checked-out source")
    return actual


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
    report = asyncio.run(_build(_verified_build_sha(arguments.build_sha)))
    _write_report(arguments.output, report.as_dict())
    if report.risk_count:
        raise SystemExit(RISK_EXIT_CODE)


if __name__ == "__main__":
    main()
