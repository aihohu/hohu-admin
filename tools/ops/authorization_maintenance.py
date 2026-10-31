"""Maintenance adapters for direct processes and Docker Compose deployments.

Adapters only stop the explicitly inventoried deployment. A successful stop
command is never sufficient: the controller also fences and observes PostgreSQL.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from anyio import CancelScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.authorization_bootstrap import FreshDatabaseEvidence, issue_bootstrap
from app.core.authorization_maintenance import (
    StoppedWritersEvidence,
    _required,
    acquire_maintenance_fence,
    require_maintenance,
)
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.process_identity import (
    ProcessIdentity,
    linux_process,
    linux_signal,
    windows_birth,
    windows_process,
)
from app.db.authorization_runtime import AuthorizationRuntime
from tools.ops.authorization_bootstrap import require_fresh_database


@dataclass(frozen=True)
class StopManifest:
    deployment_id: str
    build_sha: str
    mode: str
    surfaces: tuple[str, ...]

    def __post_init__(self):
        if (
            re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.deployment_id) is None
            or re.fullmatch(r"[a-f0-9]{40}", self.build_sha) is None
            or self.mode not in {"direct", "compose"}
            or set(self.surfaces) != {"api", "scheduler"}
        ):
            raise _required()

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()

    def persist(self, directory, **details):
        if set(details) & (set(asdict(self)) | {"manifest_digest"}):
            raise _required()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: a stale or failed maintenance operation stays closed.
        with (directory / "stop.json").open("x", encoding="utf-8") as output:
            json.dump(
                {**asdict(self), "manifest_digest": self.digest, **details}, output
            )
            output.flush()
            os.fsync(output.fileno())


class DirectProcesses:
    def __init__(self, identities):
        self.identities = tuple(identities)
        if (
            not self.identities
            or len(self.identities) > 256
            or any(
                type(item) is not ProcessIdentity or item.pid == os.getpid()
                for item in self.identities
            )
            or len({item.pid for item in self.identities}) != len(self.identities)
            or any(
                type(item.pid) is not int
                or not 0 < item.pid < 2**31
                or type(item.birth) is not str
                or not 1 <= len(item.birth) <= 128
                for item in self.identities
            )
        ):
            raise _required()

    async def stopped(self):
        for expected in self.identities:
            current = ProcessIdentity.read(expected.pid)
            if current is not None:
                # A reused PID is unknown, not proof the inventoried deployment stopped.
                if current != expected:
                    raise _required()
                return False
        return True

    async def stop(self):
        # Validate the whole inventory before signaling the first process.
        for expected in self.identities:
            current = ProcessIdentity.read(expected.pid)
            if current is not None and current != expected:
                raise _required()
        for expected in self.identities:
            if os.name == "nt":
                with windows_process(expected.pid, terminate=True) as (handle, kernel):
                    birth = windows_birth(handle, kernel)
                    if birth is None:
                        continue
                    if birth != expected.birth or not kernel.TerminateProcess(
                        handle, 1
                    ):
                        raise _required()
            else:
                # pidfd binds the signal to this process generation, even after PID reuse.
                try:
                    with linux_process(expected.pid) as descriptor:
                        current = ProcessIdentity.read(expected.pid)
                        if current is None:
                            continue
                        if current != expected:
                            raise _required()
                        linux_signal(descriptor, signal.SIGTERM)
                except ProcessLookupError:
                    continue
        async with asyncio.timeout(70):
            while not await self.stopped():  # noqa: ASYNC110 - observe OS process exit
                await asyncio.sleep(0.05)


class ComposeProcesses:
    def __init__(self, project, services):
        if (
            not isinstance(project, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project) is None
            or not isinstance(services, dict)
            or not services
            or len(services) > 32
        ):
            raise _required()
        self.project = project
        self.services = {name: tuple(ids) for name, ids in services.items()}
        ids = [cid for values in self.services.values() for cid in values]
        if (
            not ids
            or len(ids) > 256
            or len(set(ids)) != len(ids)
            or any(not values for values in self.services.values())
            or any(
                re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) is None
                for name in self.services
            )
            or any(re.fullmatch(r"[a-f0-9]{64}", cid) is None for cid in ids)
        ):
            raise _required()

    async def _docker(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(75):
                output, error = await asyncio.gather(
                    process.stdout.read(1024 * 1024 + 1),
                    process.stderr.read(1024 * 1024 + 1),
                )
                if (
                    max(len(output), len(error)) > 1024 * 1024
                    or await process.wait() != 0
                ):
                    raise _required()
                return output.decode("utf-8")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _inventory(self):
        ids = (
            await self._docker(
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + self.project,
            )
        ).split()
        if not ids or len(ids) > 1024:
            raise _required()
        rows = json.loads(await self._docker("inspect", *ids))
        matched = {}
        for row in rows:
            labels = row["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if service not in self.services:
                continue
            if labels.get("com.docker.compose.project") != self.project:
                raise _required()
            matched[row["Id"]] = row
        if set(matched) != {cid for ids in self.services.values() for cid in ids}:
            raise _required()
        for service, expected in self.services.items():
            if any(
                matched[cid]["Config"]["Labels"]["com.docker.compose.service"]
                != service
                for cid in expected
            ):
                raise _required()
        return matched

    async def stopped(self):
        rows = await self._inventory()
        return all(
            not any(
                row["State"][flag]
                for flag in ("Running", "Restarting", "Paused", "Dead")
            )
            and row["HostConfig"]["RestartPolicy"]["Name"] == "no"
            for row in rows.values()
        )

    async def stop(self):
        rows = await self._inventory()
        # Persistent restart inhibition survives controller/daemon restarts.
        await self._docker("update", "--restart=no", *sorted(rows))
        await self._docker("stop", "--time", "60", *sorted(rows))


class PostgresStop:
    """Persistent database admission fence, held by a separate operator identity.

    ALLOW_CONNECTIONS false excludes all new application logins, including old
    binaries and superusers. Two fixed maintenance pool connections are opened
    beforehand, identified by PID/backend_start and kept free of transactions.
    Exiting this controller NEVER automatically reopens the deployment.
    """

    def __init__(self, engine, directory, manifest, application_roles):
        if not isinstance(engine, AsyncEngine) or type(manifest) is not StopManifest:
            raise _required()
        self.engine, self.directory, self.manifest = engine, Path(directory), manifest
        self.roles = tuple(sorted(set(application_roles)))
        self.database = engine.url.database
        if (
            not self.roles
            or len(self.roles) > 32
            or not isinstance(self.database, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", self.database) is None
            or any(
                not isinstance(role, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", role) is None
                for role in self.roles
            )
        ):
            raise _required()
        self.connection = None
        self.original_allow = None
        self.database_oid = None
        control_database = "template1" if self.database == "postgres" else "postgres"
        self._control_engine = create_async_engine(
            engine.url.set(database=control_database),
            poolclass=NullPool,
            connect_args={"timeout": 3},
        )
        self.runtime_engine = create_async_engine(
            engine.url,
            pool_size=2,
            max_overflow=0,
            pool_timeout=2,
            pool_pre_ping=False,
            pool_recycle=-1,
            connect_args={"timeout": 3},
        )
        self._owned_backends = {}

    async def __aenter__(self):
        try:
            self.connection = await self._control_engine.connect()
            await self.connection.execution_options(isolation_level="AUTOCOMMIT")
            await self.connection.execute(text("SET statement_timeout = '3s'"))
            row = (
                await self.connection.execute(
                    text(
                        "SELECT d.oid, d.datname, d.datallowconn, r.rolsuper FROM pg_database d "
                        "JOIN pg_roles r ON r.rolname=current_user WHERE d.datname=:database"
                    ),
                    {"database": self.database},
                )
            ).one()
            if not row.rolsuper or row.datname != self.database:
                raise _required()
            self.database_oid, self.original_allow = row.oid, row.datallowconn
            locked = await self.connection.scalar(
                text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                {"key": (0x484F4855 << 32) | self.database_oid},
            )
            if not locked:
                raise _required()
            rows = (
                await self.connection.execute(
                    text(
                        "SELECT rolname, rolsuper FROM pg_roles WHERE rolname = ANY(:roles)"
                    ),
                    {"roles": list(self.roles)},
                )
            ).all()
            if {r.rolname for r in rows} != set(self.roles):
                raise _required()
            # Arbitrary database background workers are outside these application
            # adapters. Refuse configurations which could write without a login.
            preload = await self.connection.scalar(
                text("SHOW shared_preload_libraries")
            )
            if set(filter(None, (item.strip() for item in preload.split(",")))) - {
                "pg_stat_statements"
            }:
                raise _required()
            subscriptions = await self.connection.scalar(
                text(
                    "SELECT count(*) FROM pg_subscription WHERE subdbid=:oid AND subenabled"
                ),
                {"oid": self.database_oid},
            )
            if subscriptions:
                raise _required()
            return self
        except BaseException:
            # Session advisory locks must not leak back into an engine pool.
            with CancelScope(shield=True):
                async with asyncio.timeout(10):
                    try:
                        if self.connection is not None:
                            await self.connection.invalidate()
                            await self.connection.close()
                            self.connection = None
                    finally:
                        await self.runtime_engine.dispose()
                        await self._control_engine.dispose()
            raise

    async def __aexit__(self, *_args):
        with CancelScope(shield=True):
            async with asyncio.timeout(10):
                try:
                    await self.runtime_engine.dispose()
                    self._owned_backends.clear()
                finally:
                    try:
                        if self.connection is not None:
                            try:
                                if not self.connection.invalidated:
                                    await self.connection.execute(
                                        text(
                                            "SELECT pg_advisory_unlock(CAST(:key AS bigint))"
                                        ),
                                        {"key": (0x484F4855 << 32) | self.database_oid},
                                    )
                            finally:
                                await self.connection.close()
                                self.connection = None
                    finally:
                        await self._control_engine.dispose()

    async def _allow(self, value):
        if self.connection is None or type(value) is not bool:
            raise _required()
        # PostgreSQL identifiers cannot be bind parameters; constructor validates
        # this fixed database name; the value is one of two fixed SQL literals.
        await self.connection.execute(
            text(
                'ALTER DATABASE "'
                + self.database
                + '" ALLOW_CONNECTIONS '
                + ("true" if value else "false")
            )
        )

    async def _warm_runtime(self):
        connections = []
        try:
            for _ in range(2):
                connection = await self.runtime_engine.connect()
                connections.append(connection)
                row = (
                    await connection.execute(
                        text(
                            "SELECT pid, backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
                        )
                    )
                ).one()
                self._owned_backends[row.pid] = row.backend_start
                await connection.rollback()
        finally:
            for connection in connections:
                await connection.close()

    @staticmethod
    def _processes(processes):
        if type(processes) is DirectProcesses:
            return {
                "mode": "direct",
                "identities": [asdict(item) for item in processes.identities],
            }
        if type(processes) is ComposeProcesses:
            return {
                "mode": "compose",
                "project": processes.project,
                "services": {
                    name: list(ids) for name, ids in processes.services.items()
                },
            }
        raise _required()

    async def prepare(self, processes=None):
        if self.connection is None or self.original_allow is not True:
            raise _required()
        await self._warm_runtime()
        policies = {}
        if type(processes) is ComposeProcesses:
            policies = {
                cid: row["HostConfig"]["RestartPolicy"]
                for cid, row in (await processes._inventory()).items()
            }
        self.manifest.persist(
            self.directory,
            database=self.database,
            database_oid=self.database_oid,
            original_allow=self.original_allow,
            application_roles=self.roles,
            processes=self._processes(processes) if processes is not None else None,
            original_restart_policies=policies,
        )
        await self._allow(False)

    def _require_unregistered(self):
        if next(self.directory.glob("process-*.json"), None) is not None:
            raise _required()

    async def prepare_bootstrap(self):
        self._require_unregistered()
        await self.prepare()

    async def bootstrap_fence(self):
        self._require_unregistered()
        if self._record().get("processes") is not None:
            raise _required()
        await self.require_quiescent()
        async with asyncio.timeout(8):
            async with self.runtime_engine.connect() as db:
                await db.execute(text("SET LOCAL statement_timeout = '2s'"))
                await require_fresh_database(db)
        await self.require_quiescent()
        now = datetime.now(UTC)
        self._save_observation(now, bootstrap=True)
        return issue_bootstrap(
            FreshDatabaseEvidence(
                self.database_oid, self.manifest.build_sha, self.manifest.digest, now
            ),
            self.runtime_engine,
        )

    def _save_observation(self, now, *, bootstrap=False):
        record = self._record()
        record["observed_at"] = now.isoformat()
        if bootstrap:
            record["bootstrap"] = True
        temporary = self.directory / "observation.tmp"
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.directory / "stop.json")
        return record

    def _record(self):
        try:
            value = read_object(self.directory / "stop.json")
            if (
                value["manifest_digest"] != self.manifest.digest
                or value["database"] != self.database
                or value["database_oid"] != self.database_oid
                or tuple(value["application_roles"]) != self.roles
                or type(value["original_allow"]) is not bool
            ):
                raise _required()
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise _required() from None

    async def require_quiescent(self):
        self._record()
        if self.connection is None or getattr(self.connection, "invalidated", False):
            raise _required()
        try:
            # Leave time for the last normal observation to finish. An actual
            # hung query still exhausts the outer deadline and invalidates this
            # controller; it must never silently reconnect and lose its lock.
            observe_until = asyncio.get_running_loop().time() + 2.9
            async with asyncio.timeout(3):
                while not await self._observe_quiescent():  # noqa: ASYNC110 - external PostgreSQL state
                    # ReadyForQuery can reach the client before pg_stat_activity
                    # exposes idle. Only wait for our exact owned generations.
                    await asyncio.sleep(0.025)
                    if asyncio.get_running_loop().time() >= observe_until:
                        raise _required()
        except TimeoutError:
            raise _required() from None

    async def _observe_quiescent(self):
        allowed = await self.connection.scalar(
            text("SELECT datallowconn FROM pg_database WHERE oid=:oid"),
            {"oid": self.database_oid},
        )
        # Count idle connections too: their next statement can still write.
        sessions = (
            await self.connection.execute(
                text(
                    "SELECT pid, backend_start, state, xact_start FROM pg_stat_activity "
                    "WHERE datid=:oid ORDER BY pid LIMIT 1025"
                ),
                {"oid": self.database_oid},
            )
        ).all()
        prepared = await self.connection.scalar(
            text("SELECT count(*) FROM pg_prepared_xacts WHERE database=:database"),
            {"database": self.database},
        )
        if (
            allowed is not False
            or prepared != 0
            or {row.pid for row in sessions} != set(self._owned_backends)
            or any(
                row.backend_start != self._owned_backends[row.pid] for row in sessions
            )
        ):
            raise _required()
        return all(row.state == "idle" and row.xact_start is None for row in sessions)

    async def observe(self, processes):
        if type(processes) not in {DirectProcesses, ComposeProcesses}:
            raise _required()
        if (self.manifest.mode == "direct") != (type(processes) is DirectProcesses):
            raise _required()
        if self._record().get("processes") != self._processes(processes):
            raise _required()
        if not await processes.stopped():
            raise _required()
        await self.require_quiescent()
        now = datetime.now(UTC)
        record = self._save_observation(now)
        inventory = {
            key: value for key, value in record.items() if key != "observed_at"
        }
        digest = hashlib.sha256(
            json.dumps(inventory, sort_keys=True).encode()
        ).hexdigest()
        return StoppedWritersEvidence(
            deployment_id=self.manifest.deployment_id,
            component_version="0.2.0",
            build_sha=self.manifest.build_sha,
            manifest_digest=digest,
            observed_at=now,
            stopped_surfaces=frozenset(
                {"api", "worker", "scheduler", "web-write-ingress"}
            ),
            active_writer_count=0,
            ingress_fenced=True,
        )

    async def resume(self, processes):
        """Explicit operator action; never a finally/exception recovery path."""
        record = self._record()
        if record.get("processes") != self._processes(processes):
            raise _required()
        if not await processes.stopped():
            raise _required()
        await self._resume_admission(record)

    async def resume_bootstrap(self):
        self._require_unregistered()
        record = self._record()
        if record.get("processes") is not None:
            raise _required()
        await self._resume_admission(record)

    async def _resume_admission(self, record):
        await self.require_quiescent()
        observed = record.get("observed_at")
        if observed is not None:
            observed = datetime.fromisoformat(observed)
            if (
                observed.tzinfo is None
                or (datetime.now(UTC) - observed).total_seconds() <= 30
            ):
                raise _required()
        await self.runtime_engine.dispose()
        self._owned_backends.clear()
        await self.require_quiescent()
        await self._allow(record["original_allow"])
        try:
            (self.directory / "stop.json").unlink()
        except BaseException:
            await self._allow(False)
            raise


def read_object(path):
    """Read operator metadata with a hard byte limit, never executable content."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise _required()
        value = json.loads(raw)
        if type(value) is not dict:
            raise _required()
        return value
    except (OSError, ValueError, TypeError):
        raise _required() from None


def read_inventory(path, *, bootstrap=False):
    value = read_object(path)
    try:
        expected = {
            "manifest",
            "application_roles",
            "bootstrap" if bootstrap else "processes",
        }
        if set(value) != expected:
            raise _required()
        manifest = StopManifest(**value["manifest"])
        roles = value["application_roles"]
        if (
            type(roles) is not list
            or not 1 <= len(roles) <= 32
            or len(set(roles)) != len(roles)
            or any(
                type(role) is not str
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", role) is None
                for role in roles
            )
        ):
            raise _required()
        if bootstrap:
            if value["bootstrap"] is not True:
                raise _required()
            return manifest, None, tuple(roles)
        description = value["processes"]
        if description["mode"] != manifest.mode:
            raise _required()
        if manifest.mode == "direct":
            if (
                set(description) != {"mode", "identities"}
                or type(description["identities"]) is not list
            ):
                raise _required()
            processes = DirectProcesses(
                ProcessIdentity(**item) for item in description["identities"]
            )
        else:
            if set(description) != {"mode", "project", "services"}:
                raise _required()
            processes = ComposeProcesses(
                description["project"], description["services"]
            )
        return manifest, processes, tuple(roles)
    except (KeyError, TypeError, ValueError):
        raise _required() from None


def require_registered_inventory(directory, processes):
    """Local records can reveal omissions, but never prove process termination."""
    if type(processes) is not DirectProcesses:
        raise _required()
    registered = []
    expected = set(processes.identities)
    for index, path in enumerate(Path(directory).glob("process-*.json")):
        if index >= 512:
            raise _required()
        value = read_object(path)
        try:
            identity = ProcessIdentity(**value["identity"])
            current = ProcessIdentity.read(identity.pid)
        except (KeyError, ValueError, TypeError):
            raise _required() from None
        if current is None:
            continue
        if current != identity or identity not in expected:
            raise _required()
        registered.append((identity, path))
    return registered


async def wait_registered_drain(directory, processes):
    registered = require_registered_inventory(directory, processes)
    # A failed cooperative drain is not evidence. OS termination and DB checks
    # remain mandatory even after every record says quiesced.
    async with asyncio.timeout(75):
        while registered:  # noqa: ASYNC110 - external processes update their records
            pending = []
            for identity, path in registered:
                if ProcessIdentity.read(identity.pid) is None:
                    continue
                value = read_object(path)
                if value.get("identity") != asdict(identity):
                    raise _required()
                if value.get("state") not in {"quiesced", "unverified", "exiting"}:
                    pending.append((identity, path))
            registered = pending
            if pending:
                await asyncio.sleep(0.1)


_EXECUTE_ACTIONS = (
    "ai.reconcile",
    "seed.system",
    "seed.agents",
    "seed.settings",
    "seed.menus",
    "principal.create",
    "principal.permissions",
)


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Fence and verify this deployment before fixed maintenance actions"
    )
    parser.add_argument(
        "command",
        choices=(
            "stop",
            "execute",
            "observe",
            "resume",
            "bootstrap",
            "bootstrap-resume",
        ),
    )
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument(
        "--control-dir", type=Path, default=Path(settings.AUTHORIZATION_CONTROL_DIR)
    )
    parser.add_argument("--action", choices=_EXECUTE_ACTIONS)
    parser.add_argument("--payload", type=Path)
    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        if args.action is not None or args.payload is not None:
            parser.error(
                "bootstrap only initializes seed.system and accepts no payload"
            )
        args.action = "seed.system"
        return args
    if (args.command == "execute") != (args.action is not None):
        parser.error("execute requires --action; other commands forbid it")
    if args.payload is not None and (args.action or "").startswith("seed."):
        parser.error("seed actions do not accept payload files")
    return args


def seed_arguments(action, argv=None):
    if action not in {"seed.system", "seed.settings", "seed.agents", "seed.menus"}:
        raise _required()
    parser = argparse.ArgumentParser(
        description="Run this seed only after verified process and database fencing"
    )
    parser.add_argument("--inventory", required=True, type=Path)
    if action == "seed.system":
        parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument(
        "--control-dir", type=Path, default=Path(settings.AUTHORIZATION_CONTROL_DIR)
    )
    args = parser.parse_args(argv)
    args.command = "bootstrap" if getattr(args, "bootstrap", False) else "execute"
    args.action, args.payload = action, None
    return args


async def run_seed_entry(action, argv=None):
    return await run_command(seed_arguments(action, argv))


def action_payload(action, path):
    if action is None or action.startswith("seed."):
        if path is not None:
            raise _required()
        return {}
    if path is None:
        raise _required()
    value = read_object(path)
    if action == "ai.reconcile":
        if set(value) - {"maintenance_sha256"} != {
            "tenant_id",
            "operator_id",
            "manifest",
            "reason",
            "evidence_sha256",
        }:
            raise _required()
        return value
    fields = {"principal_name", "permissions", "reason", "ticket_id", "correlation_id"}
    if action == "principal.create":
        fields.add("display_name")
    if set(value) != fields:
        raise _required()
    if not os.environ.get("AUTHORIZATION_MAINTENANCE_PASSWORD"):
        raise _required()
    return value


async def execute_action(guard, processes, action, payload):
    async def observe():
        if type(processes) is DirectProcesses:
            require_registered_inventory(guard.directory, processes)
        return await guard.observe(processes)

    fence = await acquire_maintenance_fence(
        observe, action=action, expected_build_sha=guard.manifest.build_sha
    )
    runtime = AuthorizationRuntime(guard.runtime_engine)
    if action == "ai.reconcile":
        digest = require_maintenance(fence, action).manifest_digest
        expected = payload.get("maintenance_sha256")
        if expected is not None and expected != digest:
            raise _required()
        return await runtime.reconciliation.annotate(
            **{**payload, "maintenance_sha256": digest}, fence=fence
        )
    if action.startswith("seed."):
        return await runtime.seed(
            action=action,
            fence=fence,
            admin_password=os.environ.get("AUTHORIZATION_MAINTENANCE_PASSWORD"),
        )
    password = os.environ["AUTHORIZATION_MAINTENANCE_PASSWORD"]
    if action == "principal.create":
        return await runtime.create_principal(**payload, password=password, fence=fence)
    if action == "principal.permissions":
        return await runtime.replace_principal_permissions(
            **payload, current_password=password, fence=fence
        )
    raise _required()


async def run_command(args, *, reconciliation=None):
    bootstrap = args.command in {"bootstrap", "bootstrap-resume"}
    manifest, processes, roles = read_inventory(args.inventory, bootstrap=bootstrap)
    if reconciliation is not None:
        if (
            args.command != "execute"
            or args.action != "ai.reconcile"
            or args.payload is not None
        ):
            raise _required()
        payload = reconciliation
    else:
        payload = action_payload(args.action, args.payload)
    if settings.RELEASE_BUILD_SHA != manifest.build_sha:
        raise _required()
    url = os.environ.get("AUTHORIZATION_MAINTENANCE_DATABASE_URL")
    if not url:
        raise _required()
    if args.command == "bootstrap" and not os.environ.get(
        "AUTHORIZATION_MAINTENANCE_PASSWORD"
    ):
        raise _required()
    if type(processes) is DirectProcesses:
        require_registered_inventory(args.control_dir, processes)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with PostgresStop(engine, args.control_dir, manifest, roles) as guard:
            if args.command == "bootstrap-resume":
                await guard.resume_bootstrap()
                return {"state": "admission-restored", "processes_restarted": False}
            if args.command == "bootstrap":
                await guard.prepare_bootstrap()
                fence = await guard.bootstrap_fence()
                result = await AuthorizationRuntime(guard.runtime_engine).seed(
                    action="seed.system",
                    fence=fence,
                    admin_password=os.environ["AUTHORIZATION_MAINTENANCE_PASSWORD"],
                )
                await guard.require_quiescent()
                return {"state": "initialized-admission-closed", "result": result}
            if args.command in {"stop", "execute"}:
                await guard.prepare(processes)
                if type(processes) is DirectProcesses:
                    await wait_registered_drain(args.control_dir, processes)
                await processes.stop()
                if type(processes) is DirectProcesses:
                    require_registered_inventory(args.control_dir, processes)
            if args.command == "resume":
                await guard.resume(processes)
                return {"state": "admission-restored", "processes_restarted": False}
            evidence = await guard.observe(processes)
            result = {
                "state": "stopped",
                "manifest_digest": evidence.manifest_digest,
                "observed_at": evidence.observed_at.isoformat(),
            }
            if args.command == "execute":
                result["result"] = await execute_action(
                    guard, processes, args.action, payload
                )
                await guard.require_quiescent()
            return result
    finally:
        with CancelScope(shield=True):
            async with asyncio.timeout(10):
                await engine.dispose()


def main():
    try:
        result = asyncio.run(run_command(arguments()))
    except (BusinessException, TimeoutError):
        # Never include URLs, passwords, driver exception text or user payloads.
        raise SystemExit(
            "Maintenance verification failed; admission remains closed if fencing began."
        ) from None
    except Exception:
        raise SystemExit(
            "Maintenance failed; inspect protected operator records before explicit recovery."
        ) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
