"""Real migration and seed on fenced connections, never a mocked database."""

import asyncio
import json

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from app.core.exceptions import BusinessException
from app.core.security import get_password_hash
from app.db import authorization_http, session
from app.db.base import role_menus, user_roles
from app.db.request_session import RequestSession
from app.main import app
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from scripts.init_db import build_init_roles
from tests.scripts.console_install_client import complete_install
from tests.scripts.test_authorization_maintenance_database import (
    deployment_database as deployment_database,
)
from tests.scripts.test_policy_preparation_assets import bundle
from tools.ops import controlled_install as install


def options(tmp_path, role, monkeypatch, admin, *, bootstrap):
    value = {
        "manifest": {
            "deployment_id": "install-test",
            "build_sha": "a" * 40,
            "mode": "direct",
            "surfaces": ["api", "scheduler"],
        },
        "application_roles": [role],
    }
    if bootstrap:
        value["bootstrap"] = True
    else:
        value["processes"] = {
            "mode": "direct",
            "identities": [{"pid": 987654321, "birth": "windows:123"}],
        }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(install.ops.settings, "RELEASE_BUILD_SHA", "a" * 40)
    monkeypatch.setenv(
        "AUTHORIZATION_MAINTENANCE_DATABASE_URL",
        admin.url.render_as_string(hide_password=False),
    )
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Install-Test-123!")
    args = ["prepare", "--inventory", str(path), "--control-dir", str(tmp_path)]
    bundle(tmp_path)
    args += ["--policy-web-directory", str(tmp_path), "--policy-web-commit", "b" * 40]
    if bootstrap:
        args.append("--bootstrap")
    return install.arguments(args)


async def assert_closed(admin, writer, tmp_path):
    assert (tmp_path / "stop.json").exists()
    for engine in (admin, writer):
        with pytest.raises(Exception) as error:
            async with engine.connect():
                pytest.fail("new login admitted during controlled installation")
        assert getattr(error.value, "sqlstate", None) == "55000"


async def initialize_governed_old_schema(writer):
    def old_schema(connection):
        config = Config()
        config.set_main_option("script_location", str(install.ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://unused/unused")
        config.attributes["connection"] = connection
        command.upgrade(config, "d01010100101")

    async with writer.begin() as db:
        await db.run_sync(old_schema)
        await db.execute(
            text("CREATE TABLE retained_business (id integer PRIMARY KEY)")
        )
        await db.execute(text("INSERT INTO retained_business VALUES (42)"))
    # A governed 0.1.6 schema already has an explicitly appointed administrator.
    async with async_sessionmaker(writer).begin() as db:
        admin_role, ordinary_role = build_init_roles()
        actor = User(
            tenant_id=0,
            user_name="admin",
            hashed_password=get_password_hash("Install-Test-123!"),
            status="1",
        )
        db.add_all([admin_role, ordinary_role, actor])
        await db.flush()
        await db.execute(
            insert(user_roles).values(
                tenant_id=0, user_id=actor.user_id, role_id=admin_role.role_id
            )
        )
        for suffix in ("view", "edit", "activate"):
            menu = Menu(
                tenant_id=0,
                menu_name=suffix,
                menu_type="F",
                status="1",
                permission="system:role:field-policy-" + suffix,
            )
            db.add(menu)
            await db.flush()
            await db.execute(
                insert(role_menus).values(
                    tenant_id=0, role_id=admin_role.role_id, menu_id=menu.menu_id
                )
            )


@pytest.mark.parametrize("bootstrap", [True, False])
async def test_install_and_explicit_resume_preserve_data_and_application_access(
    deployment_database, tmp_path, monkeypatch, bootstrap
):
    admin, writer, role, _ = deployment_database
    if not bootstrap:
        await initialize_governed_old_schema(writer)
    args = options(tmp_path, role, monkeypatch, admin, bootstrap=bootstrap)
    result = await complete_install(args, monkeypatch)
    assert result["state"] == "prepared-admission-closed"
    await assert_closed(admin, writer, tmp_path)
    args.command = "resume"
    with pytest.raises(BusinessException):
        await install.run(args)
    await assert_closed(admin, writer, tmp_path)
    # Exercise actual permit expiry, rather than editing the protected timestamp.
    await asyncio.sleep(31)
    assert (await install.run(args))["processes_restarted"] is False
    monkeypatch.setattr(authorization_http, "engine", writer)
    monkeypatch.setattr(
        session,
        "AsyncSessionLocal",
        async_sessionmaker(
            writer, class_=RequestSession, expire_on_commit=False, autoflush=False
        ),
    )
    async with Redis.from_url(
        install.ops.settings.REDIS_URL, decode_responses=True
    ) as redis:
        monkeypatch.setattr("app.modules.auth.service.redis_client", redis)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            login = await client.post(
                "/auth/login",
                json={"userName": "admin", "password": "Install-Test-123!"},
            )
            assert login.status_code == 200, login.text
            client.headers["Authorization"] = "Bearer " + login.json()["data"]["token"]
            denied = await client.get("/system/role/list")
            assert (denied.status_code, denied.json()["errorCode"]) == (
                403,
                "FIELD_POLICY_DENIED",
            )
            for path in (
                "/auth/getUserInfo",
                "/auth/getUserRoutes",
                "/system/field-policy/catalog",
                "/system/field-policy/roles",
            ):
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)
            roles = response.json()["data"]["records"]
            assert roles and all(set(row) == {"roleId"} for row in roles)
            target = (
                "/system/field-policy/roles/"
                + roles[0]["roleId"]
                + "/objects/system.user"
            )
            assert (await client.get(target)).status_code == 200
    async with writer.connect() as db:
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == install.REVISION
        )
        assert await db.scalar(text("SELECT count(*) FROM sys_user")) >= 1
        assert (
            await db.scalar(
                text("SELECT mode FROM sys_user_field_policy_state WHERE tenant_id=0")
            )
            == "enforcing"
        )
        owner = await db.scalar(
            text(
                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='sys_user_field_policy_state'::regclass"
            )
        )
        assert owner == role
        if not bootstrap:
            assert await db.scalar(text("SELECT id FROM retained_business")) == 42
    with pytest.raises(BusinessException):
        await install.run(args)
    if not bootstrap:
        repeated = tmp_path / "repeat"
        repeated.mkdir()
        args = options(repeated, role, monkeypatch, admin, bootstrap=False)
        assert (await complete_install(args, monkeypatch))[
            "state"
        ] == "prepared-admission-closed"
        await assert_closed(admin, writer, repeated)


@pytest.mark.parametrize(
    "failure", ["migration", "seed", "occupied", "unknown-revision", "inflight"]
)
async def test_real_failures_keep_database_closed_without_success_receipt(
    deployment_database, tmp_path, monkeypatch, failure
):
    admin, writer, role, _ = deployment_database
    bootstrap = failure != "unknown-revision"
    if failure == "occupied":
        async with writer.begin() as db:
            await db.execute(text("CREATE TABLE retained (id integer)"))
    elif failure == "unknown-revision":
        async with writer.begin() as db:
            await db.execute(
                text("CREATE TABLE alembic_version (version_num varchar(32))")
            )
            await db.execute(text("INSERT INTO alembic_version VALUES ('unknown')"))
    elif failure == "migration":

        def broken(connection):
            connection.execute(text("CREATE TABLE should_rollback (id integer)"))
            raise RuntimeError("migration failure")

        monkeypatch.setattr(install, "_alembic", broken)
    elif failure == "seed":

        async def broken_seed(*_args, **_kwargs):
            raise RuntimeError("seed failure")

        monkeypatch.setattr(install.ops.AuthorizationRuntime, "seed", broken_seed)
    inflight = None
    if failure == "inflight":
        inflight = await writer.connect()
        await inflight.execute(text("SELECT 1"))
    try:
        args = options(tmp_path, role, monkeypatch, admin, bootstrap=bootstrap)
        with pytest.raises((BusinessException, RuntimeError)):
            await install.run(args)
        assert not (tmp_path / "install.json").exists()
        await assert_closed(admin, writer, tmp_path)
        assert (
            json.loads((tmp_path / "stop.json").read_text())["policy_prepare_required"]
            is True
        )
        args.command = "resume"
        with pytest.raises(BusinessException):
            await install.run(args)
    finally:
        if inflight is not None:
            await inflight.close()
