"""Real image metadata and disk reads must have separate ownership phases."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event

from app.core.authorization_io import file_transform_worker
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.security import create_access_token
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.main import app
from app.modules.auth.service import access_identity
from app.modules.system.api.file import read_chat_image
from app.modules.system.models.file import File
from app.modules.system.models.user import User
from app.modules.system.service.file_service import file_service
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import authenticated_actor
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_chat_images import PNG


@pytest.fixture
async def image_case(completion_case, tmp_path, monkeypatch):
    case = completion_case
    marker = uuid4().hex
    path = tmp_path / (marker + ".png")
    path.write_bytes(PNG)
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    async with AsyncSessionLocal() as db, db.begin():
        record = File(
            tenant_id=case.tenant.tenant_id,
            owner_user_id=case.tenant.actor_user_id,
            original_name="photo.png",
            file_name=path.name,
            file_path=str(path),
            file_url="/uploads/tenant-0/" + path.name,
            file_size=len(PNG),
            file_ext=".png",
            mime_type="image/png",
            business_type="ai-chat-image",
            del_flag="0",
        )
        db.add(record)
        await db.flush()
    try:
        yield case, record
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(File).where(
                    File.tenant_id == case.tenant.tenant_id,
                    File.file_id == record.file_id,
                )
            )


async def test_actual_image_entry_reads_disk_outside_database(image_case, monkeypatch):
    case, record = image_case
    active = set()
    observations = []
    locks, commits = [], []
    validator = file_service._validate_public_image

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    def validate(*args, **kwargs):
        observations.append(len(active))
        return validator(*args, **kwargs)

    def lock(_connection, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            locks.append(sql)

    def commit(_connection):
        commits.append(True)

    monkeypatch.setattr(file_service, "_validate_public_image", validate)
    listeners = (
        ("begin", begin),
        ("commit", end),
        ("rollback", end),
        ("before_cursor_execute", lock),
        ("commit", commit),
    )
    for name, callback in listeners:
        event.listen(engine.sync_engine, name, callback)
    try:
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(case, db)
            response = await read_chat_image(record.file_url, db, actor, case.tenant)
            assert response.body == PNG
    finally:
        for name, callback in listeners:
            event.remove(engine.sync_engine, name, callback)
    assert observations and not any(observations), observations
    assert commits == []
    assert locks and "sys_user_field_policy_global_guard" in locks[0]


async def test_chat_image_http_checks_original_jwt_version(image_case):
    case, record = image_case
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for version, success in (
            (identity.user_version, True),
            (identity.user_version + 1, False),
        ):
            token = create_access_token(
                subject=str(case.tenant.actor_user_id),
                tenant_id=case.tenant.tenant_id,
                tenant_version=case.tenant.tenant_version,
                user_version=version,
            )
            response = await client.get(
                "/system/file/chat-image",
                params={"fileUrl": record.file_url},
                headers={"Authorization": "Bearer " + token},
            )
            if success:
                assert response.status_code == 200, response.text
                assert response.content == PNG
                assert response.headers["cache-control"] == "no-store"
            else:
                assert response.status_code == 401, response.text
                assert response.content != PNG


@pytest.mark.parametrize("fault", ["deleted", "owner", "path", "version"])
async def test_image_changed_during_io_never_returns_bytes(
    image_case, monkeypatch, fault
):
    case, record = image_case
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
    original = file_transform_worker.run

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with AsyncSessionLocal() as db, db.begin():
            if fault == "version":
                row = await db.get(User, case.tenant.actor_user_id)
                row.auth_version += 1
            else:
                row = await db.get(File, record.file_id)
                if fault == "deleted":
                    row.del_flag = "1"
                elif fault == "owner":
                    row.owner_user_id = None
                else:
                    row.file_path += ".changed"
        return result

    monkeypatch.setattr(file_transform_worker, "run", changed)
    try:
        with pytest.raises(BusinessException):
            await AuthorizationRuntime(engine).upload.read_chat_image(
                tenant=case.tenant,
                actor_user_version=identity.user_version,
                file_url=record.file_url,
            )
    finally:
        if fault == "version":
            async with AsyncSessionLocal() as db, db.begin():
                row = await db.get(User, case.tenant.actor_user_id)
                row.auth_version = identity.user_version
