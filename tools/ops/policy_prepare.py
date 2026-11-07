"""Loopback-only policy preparation inside the live installation controller.

No application lifespan, scheduler, business router or normal-admission bypass.
The warmed, closed-admission database connections belong to the stop controller.
"""

import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import RootModel, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.authorization_maintenance import (
    StoppedWritersEvidence,
    acquire_maintenance_fence,
)
from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog
from app.core.field_policy.documents import catalog_parts, read_json
from app.core.field_policy.maintenance import (
    require_normal_resume,  # noqa: F401 - shared protocol export
)
from app.core.process_identity import ProcessIdentity
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.auth.service import access_identity, get_current_user
from app.modules.system.api.field_management import (
    bounded_body,
    create_management_router,
)
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.service.generic_field_policy_migration import migrate_legacy
from app.modules.system.service.generic_field_policy_service import (
    command_bytes,
    request_identity,
)
from tools.ops.policy_preparation_identity import (
    PreparationSessions,
    credential_identity,
    require_preparation_administrators,
)


class PreparationCommand(RootModel[dict]):
    @staticmethod
    def normalize_keys(value, keys):
        if type(value) is not dict:
            raise ValueError("Expected an object")
        aliases = {to_camel(key): key for key in keys}
        converted = {}
        for key, item in value.items():
            name = aliases.get(key, key)
            if name in converted:
                raise ValueError("Ambiguous command")
            converted[name] = item
        return converted

    @model_validator(mode="before")
    @classmethod
    def closed_command(cls, value):
        if type(value) is not dict:
            raise ValueError("Expected a command")
        converted = cls.normalize_keys(
            value,
            (
                "request_id",
                "expected_version",
                "expected_epoch",
                "role_id",
                "object_id",
                "binding_version",
            ),
        )
        if "rules" in converted:
            if type(converted["rules"]) is not list or len(converted["rules"]) > 2048:
                raise ValueError("Invalid rule array")
            converted["rules"] = [
                cls.normalize_keys(rule, ("field_id", "mask_id"))
                for rule in converted["rules"]
            ]
        if "role_id" in converted and type(converted["role_id"]) is str:
            role = converted["role_id"]
            if (
                not role.isascii()
                or not role.isdecimal()
                or len(role) > 19
                or role.startswith("0")
            ):
                raise ValueError("Invalid role identity")
            converted["role_id"] = int(role)
        if converted.get("action") == "receipt":
            if set(converted) != {"action", "request_id"}:
                raise ValueError("Invalid receipt request")
            request_identity(converted["request_id"])
        else:
            command_bytes(converted)
        return converted


def _record(guard, value, *, create=False):
    path = guard.directory / "policy-prepare.json"
    temporary = guard.directory / "policy-prepare.pending"
    with temporary.open("x", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    if create:
        os.link(temporary, path)
        temporary.unlink()
    else:
        os.replace(temporary, path)


async def _evidence(guard, processes, bootstrap):
    if not bootstrap:
        return await guard.observe(processes)
    guard._require_unregistered()
    if guard._record().get("processes") is not None:
        raise BusinessException(
            503,
            "准备会话停写证明无效",
            error_code="AUTHORIZATION_MAINTENANCE_FENCE_REQUIRED",
        )
    await guard.require_quiescent()
    now = datetime.now(UTC)
    guard._save_observation(now, bootstrap=True)
    return StoppedWritersEvidence(
        guard.manifest.deployment_id,
        "0.2.0",
        guard.manifest.build_sha,
        guard.manifest.digest,
        now,
        frozenset({"api", "worker", "scheduler", "web-write-ingress"}),
        0,
        True,
    )


class PolicyPreparation:
    def __init__(self, guard, processes, bootstrap, catalog):
        self.guard, self.processes, self.bootstrap, self.catalog = (
            guard,
            processes,
            bootstrap,
            catalog,
        )
        self.lock = asyncio.Lock()
        self.runtime = AuthorizationRuntime(guard.runtime_engine)
        self.sessions = async_sessionmaker(guard.runtime_engine, expire_on_commit=False)
        self.finished = asyncio.Event()
        self.identities = PreparationSessions()

    def _event(self, operation, code, identity=None):
        from tools.ops.policy_preparation_events import record_event  # noqa: PLC0415

        try:
            record_event(self.guard.directory, operation, code, identity)
        except BaseException:
            self.identities.clear()
            self.finished.set()
            raise

    def _rejected(self, operation, error, identity=None):
        if not isinstance(error, BusinessException) or error.error_code in {
            "AUTHORIZATION_COMMIT_UNKNOWN",
            "AUTHORIZATION_ROLLBACK_UNKNOWN",
            "AUTHORIZATION_MAINTENANCE_FENCE_REQUIRED",
        }:
            self.identities.clear()
            self.finished.set()
        self._event(operation, getattr(error, "error_code", None), identity)

    async def login(self, payload):
        async with self.lock:
            self._require_open()
            identity = None
            try:
                self.identities.attempt()
                await _evidence(self.guard, self.processes, self.bootstrap)
                verified = await credential_identity(self.sessions, payload)
                # Revalidate versions and dedicated authority after password work.
                await self.runtime.field_management(
                    tenant=verified.tenant,
                    actor_user_version=verified.user_version,
                    catalog=self.catalog,
                    operation="catalog",
                    payload={},
                )
                identity = verified
                await self.guard.require_quiescent()
                token = self.identities.issue(identity)
                self._event("login", "accepted", identity)
                return {
                    "token": token,
                    "expiresIn": 900,
                    "tenantId": str(identity.tenant.tenant_id),
                    "actorId": str(identity.tenant.actor_user_id),
                    "protocol": "field-management/1",
                }
            except BaseException as error:
                self._rejected("login", error, identity)
                raise

    async def management(self, token, **request):
        async with self.lock:
            self._require_open()
            identity = None
            try:
                identity = self.identities.resolve(token)
                await _evidence(self.guard, self.processes, self.bootstrap)
                result = await self.runtime.field_management(
                    tenant=identity.tenant,
                    actor_user_version=identity.user_version,
                    catalog=self.catalog,
                    **request,
                )
                await self.guard.require_quiescent()
                return result
            except BaseException as error:
                self._rejected(request.get("operation"), error, identity)
                raise

    async def logout(self, token):
        async with self.lock:
            self.identities.revoke(token)
            self._event("logout", "accepted")
        return {"loggedOut": True}

    async def _authenticate(self, token):
        async with asyncio.timeout(5):
            async with self.sessions() as db:
                await db.execute(text("SET LOCAL statement_timeout = '2s'"))
                actor = await get_current_user(token=token, db=db)
                identity = access_identity(actor)
                await db.rollback()
                return identity

    def _require_open(self):
        if self.finished.is_set():
            raise BusinessException(
                409, "准备会话已结束", error_code="FIELD_POLICY_UNAVAILABLE"
            )

    async def _execute_locked(self, token, command):
        self._require_open()
        # Preparation catalog only contains the compatibility object. It cannot qualify full rollout.
        if command.get("action") in {"ready", "activate"}:
            raise BusinessException(
                409,
                "全对象入口验收尚未完成",
                error_code="FIELD_POLICY_ACTIVATION_BLOCKED",
            )
        try:
            await _evidence(self.guard, self.processes, self.bootstrap)
            identity = await self._authenticate(token)
            result = await self.runtime.generic_field_policy(
                tenant=identity.tenant,
                actor_user_version=identity.user_version,
                catalog=self.catalog,
                command=command,
            )
            await self.guard.require_quiescent()
            return result
        except BaseException as error:
            self._rejected(command.get("action"), error)
            raise

    async def execute(self, token, command):
        self._require_open()
        async with self.lock:
            return await self._execute_locked(token, command)

    async def finish(self, token):
        async with self.lock:
            try:
                if "." in token:
                    await self._execute_locked(token, {"action": "read"})
                else:
                    self._require_open()
                    identity = self.identities.resolve(token)
                    await _evidence(self.guard, self.processes, self.bootstrap)
                    await self.runtime.field_management(
                        tenant=identity.tenant,
                        actor_user_version=identity.user_version,
                        catalog=self.catalog,
                        operation="catalog",
                        payload={},
                    )
                    await self.guard.require_quiescent()
            except BaseException as error:
                self._rejected("finish", error)
                raise
            self.identities.clear()
            self.finished.set()
            self._event("finish", "accepted")
        return {"state": "preparing-admission-closed"}


def _wire(value, name=""):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {to_camel(key): _wire(item, key) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    if type(value) is int and (name.endswith("_id") or name.endswith("Id")):
        return str(value)
    return value


def create_app(controller, assets=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def token(request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not 1 <= len(header[7:]) <= 16384:
            raise BusinessException(401, "需要已有管理身份", error_code="TOKEN_EXPIRED")
        return header[7:]

    @app.exception_handler(BusinessException)
    async def rejected(_request, error):
        return JSONResponse(
            status_code=error.code,
            content={
                "code": error.code,
                "msg": error.message,
                "data": None,
                "errorCode": error.error_code,
            },
        )

    def response(value):
        return {"code": 200, "msg": "操作成功", "data": _wire(value)}

    @app.middleware("http")
    async def local_origin(request, call_next):
        # Exact loopback Host and same Origin prevent DNS rebinding/cross-site preparation.
        if request.url.hostname not in {"127.0.0.1", "localhost"} or (
            request.headers.get("origin") is not None
            and request.headers["origin"]
            != f"{request.url.scheme}://{request.headers.get('host')}"
        ):
            return JSONResponse(
                status_code=403,
                content={"code": 403, "msg": "请求来源无效", "data": None},
            )
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        return result

    @app.post("/policy-prepare/login")
    async def login(request: Request):
        return response(await controller.login(await bounded_body(request, 8192)))

    @app.post("/policy-prepare/logout")
    async def logout(request: Request):
        return response(await controller.logout(token(request)))

    async def management_context(request: Request):
        bearer = token(request)

        async def execute(**arguments):
            return await controller.management(bearer, **arguments)

        return execute

    app.include_router(
        create_management_router(management_context), prefix="/system/field-policy"
    )

    @app.get("/policy-prepare/catalog")
    async def catalog(request: Request):
        return response(await controller.execute(token(request), {"action": "read"}))

    @app.post("/policy-prepare/command")
    async def command(request: Request):
        bearer = token(request)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 524288:
                raise BusinessException(
                    413,
                    "策略命令超出预算",
                    error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
                )
            body.extend(chunk)
        try:
            payload = PreparationCommand.model_validate(read_json(body)).root
        except (ValueError, TypeError, UnicodeError):
            raise BusinessException(
                400, "策略命令无效", error_code="FIELD_POLICY_INVALID"
            ) from None
        return response(await controller.execute(bearer, payload))

    @app.post("/policy-prepare/finish")
    async def finish(request: Request):
        return response(await controller.finish(token(request)))

    if assets is not None:
        assets.mount(app)
    return app


async def serve(
    guard, processes, *, bootstrap, port, web_directory=None, web_commit=None
):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Policy preparation requires an unprivileged loopback port")
    from tools.ops.policy_preparation_assets import PreparationAssets  # noqa: PLC0415

    if bool(web_directory) != bool(web_commit):
        raise ValueError(
            "Preparation Web directory and exact commit are required together"
        )
    assets = PreparationAssets(web_directory, web_commit) if web_directory else None
    catalog = Catalog((user_compat_object(),), ())
    sessions = async_sessionmaker(guard.runtime_engine, expire_on_commit=False)
    await _evidence(guard, processes, bootstrap)
    await require_preparation_administrators(sessions)
    await guard.require_quiescent()
    action, adapter = catalog_parts(catalog)
    identity = ProcessIdentity.read(os.getpid())
    record = {
        "schema_version": 2,
        "state": "preparing",
        "build_sha": guard.manifest.build_sha,
        "database_oid": guard.database_oid,
        "manifest_digest": guard.manifest.digest,
        "identity": asdict(identity),
        "catalog_digest": catalog.digest,
        "action_digest": action,
        "adapter_digest": adapter,
        "address": "127.0.0.1",
        "port": port,
        "business_background_tasks": [],
        "web_commit": assets.source_commit if assets else None,
        "web_manifest_digest": assets.digest if assets else None,
    }
    _record(guard, record, create=True)

    async def observer():
        return await _evidence(guard, processes, bootstrap)

    permit = await acquire_maintenance_fence(
        observer, action="migration", expected_build_sha=guard.manifest.build_sha
    )
    async with sessions() as db:
        try:
            async with asyncio.timeout(25):
                await db.execute(text("SET LOCAL lock_timeout = '2s'"))
                await db.execute(text("SET LOCAL statement_timeout = '5s'"))
                result = await migrate_legacy(
                    db,
                    fence=permit,
                    catalog=catalog,
                    catalog_revision="system-fields/2-prepare",
                    source_build=guard.manifest.build_sha,
                )
                await db.commit()
        except BaseException:
            await db.rollback()
            raise
    await guard.require_quiescent()
    record["migration"] = result
    _record(guard, record)
    controller = PolicyPreparation(guard, processes, bootstrap, catalog)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(controller, assets),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
            lifespan="off",
            limit_concurrency=16,
            timeout_keep_alive=2,
        )
    )

    async def end():
        await controller.finished.wait()
        server.should_exit = True

    watcher = asyncio.create_task(end())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    await guard.require_quiescent()
    record["state"] = "preparing-stopped"
    _record(guard, record)
    return record
