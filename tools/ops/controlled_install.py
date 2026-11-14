"""Migrate and seed through one closed-admission maintenance session."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from anyio import CancelScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.utils.validators import validate_password
from tools.ops import authorization_maintenance as ops

REVISION = "e020f3b00001"
ROOT = Path(__file__).resolve().parents[2]


def _alembic(connection):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    # The supplied connection is mandatory; this URL is never connected to.
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://unused/unused")
    config.attributes["connection"] = connection
    if ScriptDirectory.from_config(config).get_heads() != [REVISION]:
        raise ops._required()
    command.upgrade(config, REVISION)


async def migrate(guard, bootstrap):
    """Use a warmed connection; new logins remain forbidden throughout DDL."""
    if len(guard.roles) != 1 or guard.roles[0] == guard.engine.url.username:
        raise ops._required()
    async with asyncio.timeout(180):
        async with guard.runtime_engine.begin() as db:
            await db.execute(text("SET LOCAL search_path = public"))
            await db.execute(text("SET LOCAL lock_timeout = '5s'"))
            await db.execute(text("SET LOCAL statement_timeout = '120s'"))
            role = guard.roles[0]
            privileged = await db.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname=:role"),
                {"role": role},
            )
            if privileged is not False:
                raise ops._required()
            if bootstrap:
                occupied = await db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname NOT LIKE 'pg_%' "
                        "AND n.nspname <> 'information_schema' "
                        "AND c.relkind IN ('r','p','v','m','f','S'))"
                    )
                )
                if occupied:
                    raise ops._required()
            else:
                heads = await db.run_sync(
                    lambda connection: MigrationContext.configure(
                        connection
                    ).get_current_heads()
                )
                if heads not in {
                    ("d01010100101",),
                    ("e0204a000001",),
                    ("e020f1200001",),
                    ("e020f2a00001",),
                    ("e020f2b00001",),
                    ("e020f2c00001",),
                    ("e020f3a00001",),
                    (REVISION,),
                }:
                    raise ops._required()
            # Parameterized role selection preserves application table ownership.
            await db.execute(
                text("SELECT set_config('role', :role, true)"), {"role": role}
            )
            await db.run_sync(_alembic)
            heads = await db.run_sync(
                lambda connection: MigrationContext.configure(
                    connection
                ).get_current_heads()
            )
            if heads != (REVISION,):
                raise ops._required()


def _receipt(guard, bootstrap):
    path = guard.directory / "stop.json"
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ops._required()
    policy_digest = None
    policy_path = guard.directory / "policy-prepare.json"
    if policy_path.exists():
        with policy_path.open("rb") as source:
            policy_record = source.read(65537)
        if len(policy_record) > 65536:
            raise ops._required()
        policy_digest = hashlib.sha256(policy_record).hexdigest()
    return {
        "schema": 2,
        "build": guard.manifest.build_sha,
        "manifest": guard.manifest.digest,
        "database_oid": guard.database_oid,
        "revision": REVISION,
        "bootstrap": bootstrap,
        "policy_prepare_sha256": policy_digest,
        "stop_sha256": hashlib.sha256(raw).hexdigest(),
    }


def write_receipt(guard, bootstrap):
    """Only a completed migration, seed and final observation reaches this write."""
    record = _receipt(guard, bootstrap)
    temporary = guard.directory / "install.pending.json"
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(record, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    # An incomplete write is never a usable receipt; linking is exclusive.
    os.link(temporary, guard.directory / "install.json")
    temporary.unlink()


def require_receipt(guard, bootstrap):
    try:
        record = ops.read_object(guard.directory / "install.json")
        if (
            type(record.get("schema")) is not int
            or type(record.get("database_oid")) is not int
            or type(record.get("bootstrap")) is not bool
            or record != _receipt(guard, bootstrap)
        ):
            raise ops._required()
        return record
    except (OSError, ValueError, TypeError):
        raise ops._required() from None


async def prepare(
    guard,
    processes,
    bootstrap,
    *,
    policy_port=None,
    policy_web_directory=None,
    policy_web_commit=None,
):
    if (guard.directory / "install.json").exists():
        raise ops._required()
    if bootstrap:
        await guard.prepare_bootstrap()
        await guard.require_quiescent()
    else:
        await guard.prepare(processes)
        if type(processes) is ops.DirectProcesses:
            await ops.wait_registered_drain(guard.directory, processes)
        await processes.stop()
        if type(processes) is ops.DirectProcesses:
            ops.require_registered_inventory(guard.directory, processes)
        await guard.observe(processes)
        if policy_port is not None:
            from tools.ops.policy_preparation_identity import (  # noqa: PLC0415
                require_preparation_administrators,
            )

            await require_preparation_administrators(
                async_sessionmaker(guard.runtime_engine, expire_on_commit=False)
            )
    await migrate(guard, bootstrap)
    await guard.require_quiescent()
    async with asyncio.timeout(180):
        if bootstrap:
            permit = await guard.bootstrap_fence()
            await ops.AuthorizationRuntime(guard.runtime_engine).seed(
                action="seed.system",
                fence=permit,
                admin_password=os.environ["AUTHORIZATION_MAINTENANCE_PASSWORD"],
            )
        else:
            await ops.execute_action(guard, processes, "seed.system", None)
    await guard.require_quiescent()
    if policy_port is not None:
        from tools.ops.policy_prepare import (  # noqa: PLC0415 - optional preparation listener
            serve,
        )

        web = (
            {"web_directory": policy_web_directory, "web_commit": policy_web_commit}
            if policy_web_directory or policy_web_commit
            else {}
        )
        await serve(guard, processes, bootstrap=bootstrap, port=policy_port, **web)
        await guard.require_quiescent()
    write_receipt(guard, bootstrap)
    return {"state": "prepared-admission-closed", "revision": REVISION}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "policy-prepare", "resume"))
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--policy-port", type=int, default=8765)
    parser.add_argument("--policy-web-directory", type=Path)
    parser.add_argument("--policy-web-commit")
    return parser.parse_args(argv)


async def run(args):
    manifest, processes, roles = ops.read_inventory(
        args.inventory, bootstrap=args.bootstrap
    )
    url = os.environ.get("AUTHORIZATION_MAINTENANCE_DATABASE_URL")
    if (
        manifest.build_sha != ops.settings.RELEASE_BUILD_SHA
        or not url
        or len(roles) != 1
        or (
            args.command in {"prepare", "policy-prepare"}
            and args.bootstrap
            and not os.environ.get("AUTHORIZATION_MAINTENANCE_PASSWORD")
        )
    ):
        raise ops._required()
    if type(processes) is ops.DirectProcesses:
        ops.require_registered_inventory(args.control_dir, processes)
    password = os.environ.get("AUTHORIZATION_MAINTENANCE_PASSWORD")
    if args.command in {"prepare", "policy-prepare"} and password is not None:
        validate_password(password)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with ops.PostgresStop(engine, args.control_dir, manifest, roles) as guard:
            if args.command in {"prepare", "policy-prepare"}:
                return await prepare(
                    guard,
                    processes,
                    args.bootstrap,
                    policy_port=args.policy_port
                    if args.command == "policy-prepare"
                    else None,
                    policy_web_directory=getattr(args, "policy_web_directory", None),
                    policy_web_commit=getattr(args, "policy_web_commit", None),
                )
            require_receipt(guard, args.bootstrap)
            if args.bootstrap:
                await guard.resume_bootstrap()
            else:
                await guard.resume(processes)
            return {"state": "admission-restored", "processes_restarted": False}
    finally:
        with CancelScope(shield=True):
            async with asyncio.timeout(10):
                await engine.dispose()


def main():
    args = arguments()
    try:
        result = asyncio.run(run(args))
    except Exception:
        raise SystemExit(
            "Controlled installation failed; do not start applications. "
            "Admission remains closed if fencing began; inspect protected records."
        ) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
