"""Initialize an explicitly disposable test database through production proofs."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy.engine import make_url

from tools.ops import authorization_maintenance as ops


def write_inventory(directory, manifest, role):
    directory.mkdir(parents=True, exist_ok=True)
    inventory = directory / "bootstrap.json"
    with inventory.open("x", encoding="utf-8") as output:
        json.dump(
            {
                "manifest": ops.asdict(manifest),
                "application_roles": [role],
                "bootstrap": True,
            },
            output,
        )
    return inventory


async def bootstrap(directory: Path, build_sha: str):
    if (
        os.environ.get("ENV") != "test"
        or os.environ.get("HOHU_TEST_DATABASE_EPHEMERAL") != "true"
        or not os.environ.get("AUTHORIZATION_MAINTENANCE_PASSWORD")
    ):
        raise ValueError("Explicit disposable test database and seed password required")
    manifest = ops.StopManifest(
        "test-bootstrap", build_sha, "direct", ("api", "scheduler")
    )
    url = make_url(ops.settings.DATABASE_URL)
    if url.drivername != "postgresql+asyncpg" or not url.username:
        raise ValueError("PostgreSQL test database required")
    inventory = write_inventory(directory, manifest, url.username)
    common = ["--inventory", str(inventory), "--control-dir", str(directory)]
    await ops.run_command(ops.arguments(["bootstrap", *common]))
    # Explicit recovery only follows a known successful seed; never a finally.
    # Let every issued permit expire without modifying its persisted timestamp.
    await asyncio.sleep(31)
    await ops.run_command(ops.arguments(["bootstrap-resume", *common]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--build-sha", required=True)
    args = parser.parse_args()
    os.environ["AUTHORIZATION_MAINTENANCE_DATABASE_URL"] = str(
        ops.settings.DATABASE_URL
    )
    try:
        asyncio.run(bootstrap(args.control_dir, args.build_sha))
    except Exception:
        raise SystemExit(
            "Test bootstrap failed; inspect protected maintenance records"
        ) from None


if __name__ == "__main__":
    main()
