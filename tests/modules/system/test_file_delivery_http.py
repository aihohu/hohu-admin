"""The real upload and file routes share current field and source authority."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event, select, update

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db import authorization_file_read_owner as file_owner
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.models.file import File
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_system_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_upload_http import file_http as file_http
from tests.modules.system.test_relation_field_writes import activate_relations


async def upload(client, content, **values):
    return await client.post(
        "/file/upload", files={"file": ("photo.png", content, "image/png")}, data=values
    )


async def test_ordinary_upload_is_private_with_real_bytes_bound_to_source(file_http):
    client, plain, content = file_http
    response = await upload(client, content)
    assert response.status_code == 200, response.text
    identifier = int(response.json()["data"]["fileId"])
    async with plain() as db:
        record = await db.get(File, identifier)
        assert record.delivery_class == "owned-private"
        assert record.source_manifest["file_id"] == str(identifier)
        assert Path(record.file_path).is_relative_to(Path(settings.PRIVATE_UPLOAD_DIR))
        assert record.file_url == "" and record.content_sha256


async def test_bad_later_upload_does_not_leave_earlier_batch_bytes(file_http):
    client, plain, content = file_http
    response = await client.post(
        "/file/batch-upload",
        files=[
            ("files", ("photo.png", content, "image/png")),
            ("files", ("disguised.pdf", b"not a PDF", "application/pdf")),
        ],
    )
    assert response.status_code == 400, response.text
    assert not await asyncio.to_thread(
        lambda: list(Path(settings.PRIVATE_UPLOAD_DIR).rglob("*.png"))
    )
    async with plain() as db:
        assert not (await db.scalars(select(File.file_id))).all()


async def test_public_brand_is_an_explicit_verified_category(file_http):
    client, plain, content = file_http
    response = await upload(client, content, business_type="brand")
    assert response.status_code == 200, response.text
    async with plain() as db:
        record = await db.get(File, int(response.json()["data"]["fileId"]))
        assert record.delivery_class == "public-image" and record.source_manifest
        assert Path(record.file_path).is_relative_to(Path(settings.UPLOAD_DIR))


async def test_business_id_is_rejected_before_file_publication(file_http):
    client, plain, content = file_http
    response = await upload(client, content, business_type="product", business_id="301")
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "FILE_BUSINESS_SOURCE_REQUIRED"
    async with plain() as db:
        assert await db.scalar(select(File.file_id)) is None


async def test_generic_file_create_deny_cannot_be_bypassed_by_upload_function(
    file_http, monkeypatch
):
    client, plain, content = file_http
    runtime = AuthorizationRuntime(plain.kw["bind"])
    await activate_relations(
        (runtime.sessions, plain),
        monkeypatch,
        missing=(("system.file", "content", "create"),),
    )
    response = await upload(client, content)
    assert response.status_code == 403, response.text
    assert not await asyncio.to_thread(
        lambda: list(Path(settings.PRIVATE_UPLOAD_DIR).rglob("*.png"))
    )
    async with plain() as db:
        assert await db.scalar(select(File.file_id)) is None


async def test_generic_file_list_and_detail_are_sparse(file_http, monkeypatch):
    client, plain, content = file_http
    response = await upload(client, content)
    identifier = response.json()["data"]["fileId"]
    runtime = AuthorizationRuntime(plain.kw["bind"])
    await activate_relations(
        (runtime.sessions, plain),
        monkeypatch,
        missing=(("system.file", "original_name", "read"),),
    )
    page = await client.get("/file/list")
    assert page.status_code == 200, page.text
    assert "fieldPolicy" in page.json()["data"]
    row = page.json()["data"]["records"][0]
    assert "originalName" not in row and "fileUrl" not in row
    assert "filePath" not in row and "fileName" not in row and "businessId" not in row
    detail = await client.get("/file/" + identifier)
    assert detail.status_code == 200, detail.text
    assert "originalName" not in detail.json()["data"]


async def test_protected_download_uses_current_source_and_no_store(file_http):
    client, _, content = file_http
    response = await upload(client, content)
    identifier = response.json()["data"]["fileId"]
    downloaded = await client.get(f"/file/{identifier}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == content
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"


async def test_chat_image_cannot_bypass_generic_file_content_deny(
    file_http, monkeypatch
):
    client, plain, content = file_http
    response = await upload(client, content, business_type="ai-chat")
    assert response.status_code == 200, response.text
    url = response.json()["data"]["fileUrl"]
    runtime = AuthorizationRuntime(plain.kw["bind"])
    await activate_relations(
        (runtime.sessions, plain),
        monkeypatch,
        missing=(("system.file", "content", "read"),),
    )
    with pytest.raises(BusinessException) as caught:
        await runtime.upload.read_chat_image(
            tenant=TENANT, actor_user_version=1, file_url=url
        )
    assert caught.value.error_code == "FIELD_READ_DENIED"


async def test_download_worker_has_no_database_transaction(file_http, monkeypatch):
    client, plain, content = file_http
    response = await upload(client, content)
    identifier = response.json()["data"]["fileId"]
    active, calls = set(), []
    engine = plain.kw["bind"].sync_engine
    original = file_owner.read_file_content

    def begin(connection):
        active.add(connection)

    def finish(connection):
        active.discard(connection)

    def read(*args):
        assert not active
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(file_owner, "read_file_content", read)
    for name, fn in (("begin", begin), ("rollback", finish), ("commit", finish)):
        event.listen(engine, name, fn)
    try:
        result = await client.get(f"/file/{identifier}/download")
        assert result.status_code == 200, result.text
        assert result.content == content and calls == [True]
    finally:
        for name, fn in (("begin", begin), ("rollback", finish), ("commit", finish)):
            event.remove(engine, name, fn)


async def test_revocation_after_reading_disk_delivers_no_file_bytes(
    file_http, monkeypatch
):
    client, plain, content = file_http
    response = await upload(client, content)
    identifier = response.json()["data"]["fileId"]
    original = file_owner.file_transform_worker.run

    async def changed(function, *args, **kwargs):
        value = await original(function, *args, **kwargs)
        async with plain() as db:
            await db.execute(
                update(User)
                .where(User.user_id == 101, User.tenant_id == 0)
                .values(auth_version=2)
            )
            await db.commit()
        return value

    monkeypatch.setattr(file_owner.file_transform_worker, "run", changed)
    result = await client.get(f"/file/{identifier}/download")
    assert result.status_code in {401, 403}, result.text
    assert result.content != content
