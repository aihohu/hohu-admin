"""Real uploaded image input, with tenant/owner and immutable history boundaries."""

# ruff: noqa: ASYNC240

import base64
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, UploadFile
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers

from app.core import auth
from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
    BusinessRuleException,
    setup_exception_handlers,
)
from app.core.id_generator import next_id
from app.db.session import get_db
from app.modules.ai.api.chat import _convert_local_images_to_data_uri
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.auth.service import get_current_tenant_context, get_current_user
from app.modules.system.api.file import router as file_router
from app.modules.system.models.file import File
from app.modules.system.models.user import User
from app.modules.system.service.file_service import file_service
from tests.tenant_helpers import tenant_context

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def test_new_chat_image_is_private_but_owner_can_read(
    db_session, uploaded_image, tmp_path, monkeypatch
):
    _, tenant = uploaded_image
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path / "private"))
    record = await file_service.upload(
        db_session,
        UploadFile(
            BytesIO(PNG),
            filename="photo.png",
            headers=Headers({"content-type": "image/png"}),
        ),
        business_type="ai-chat",
        owner_user_id=tenant.actor_user_id,
        tenant=tenant,
    )
    await db_session.flush()
    assert record.business_type == "ai-chat-image"
    assert str(tmp_path / "private") in record.file_path
    assert record.file_url.startswith("/uploads/tenant-0/")
    assert await file_service.read_chat_image(
        db_session, record.file_url, tenant=tenant
    ) == (PNG, "image/png")


async def test_legacy_chat_image_is_never_classified_public(db_session, uploaded_image):
    record, _ = uploaded_image
    assert not await file_service.is_public_upload(db_session, record.file_url)
    record.business_type = "avatar"
    await db_session.flush()
    assert await file_service.is_public_upload(db_session, record.file_url)
    record.del_flag = "1"
    await db_session.flush()
    assert not await file_service.is_public_upload(db_session, record.file_url)
    assert not await file_service.is_public_upload(db_session, "/uploads/missing.png")


@pytest.mark.parametrize(
    "scenario",
    ["owner", "other-owner", "other-tenant", "revoked", "disabled", "anonymous"],
)
async def test_chat_image_http_live_access(
    db_session, uploaded_image, monkeypatch, scenario
):
    record, tenant = uploaded_image
    test_app = FastAPI()
    test_app.include_router(file_router, prefix="/system/file")
    setup_exception_handlers(test_app)
    monkeypatch.setattr(settings, "AI_MODULE_ENABLED", scenario != "disabled")
    monkeypatch.setattr(
        auth, "has_explicit_permission", lambda *_: scenario != "revoked"
    )
    if scenario == "other-owner":
        tenant = tenant_context(actor_user_id=tenant.actor_user_id + 1)
    elif scenario == "other-tenant":
        tenant = tenant_context(tenant_id=99, actor_user_id=tenant.actor_user_id)

    async def current_user():
        if scenario == "anonymous":
            raise AuthenticationException()
        return SimpleNamespace(user_name="test", user_id=tenant.actor_user_id)

    test_app.dependency_overrides[get_current_user] = current_user
    test_app.dependency_overrides[get_current_tenant_context] = lambda: tenant
    test_app.dependency_overrides[get_db] = lambda: db_session
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/system/file/chat-image", params={"fileUrl": record.file_url}
        )
    if scenario == "owner":
        assert response.status_code == 200
        assert response.content == PNG
        assert response.headers["cache-control"] == "no-store"
    else:
        assert response.status_code in {400, 401, 403}
        assert response.content != PNG


async def test_private_image_still_decodes_content_before_writing(
    db_session, uploaded_image
):
    _, tenant = uploaded_image
    with pytest.raises(BusinessRuleException):
        await file_service.upload(
            db_session,
            UploadFile(
                BytesIO(b"not-an-image"),
                filename="photo.png",
                headers=Headers({"content-type": "image/png"}),
            ),
            business_type="ai-chat-image",
            owner_user_id=tenant.actor_user_id,
            tenant=tenant,
        )


def test_text_only_model_rejects_image_before_provider():
    selected = SimpleNamespace(model=SimpleNamespace(capabilities=["text"]))
    with pytest.raises(BusinessRuleException) as error:
        model_authorization_service.ensure_image_support(selected, has_images=True)
    assert error.value.error_code == "AI_MODEL_VISION_REQUIRED"
    selected.model.capabilities.append("vision")
    model_authorization_service.ensure_image_support(selected, has_images=True)


@pytest.fixture
async def uploaded_image(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "public"))
    owner = next_id()
    db_session.add(
        User(
            user_id=owner,
            user_name=f"im{str(owner)[-10:]}",
            hashed_password="test-hash",
            tenant_id=0,
        )
    )
    await db_session.flush()
    path = tmp_path / "public" / "tenant-0" / "picture.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(PNG)
    record = File(
        file_id=next_id(),
        original_name="picture.png",
        file_name="picture",
        file_path=str(path),
        file_url="/uploads/tenant-0/picture.png",
        file_size=len(PNG),
        file_ext=".png",
        mime_type="image/png",
        business_type="ai-chat",
        owner_user_id=owner,
        tenant_id=0,
    )
    db_session.add(record)
    await db_session.flush()
    return record, tenant_context(actor_user_id=owner)


async def test_real_upload_record_reads_only_current_tenant_owner(
    db_session, uploaded_image
):
    record, tenant = uploaded_image
    assert await file_service.read_chat_image(
        db_session, record.file_url, tenant=tenant
    ) == (PNG, "image/png")
    for denied in (
        tenant_context(actor_user_id=tenant.actor_user_id + 1),
        tenant_context(tenant_id=99, actor_user_id=tenant.actor_user_id),
    ):
        with pytest.raises(BusinessRuleException) as error:
            await file_service.read_chat_image(
                db_session, record.file_url, tenant=denied
            )
        assert error.value.error_code == "AI_IMAGE_NOT_AVAILABLE"
    record.del_flag = "1"
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await file_service.read_chat_image(db_session, record.file_url, tenant=tenant)


@pytest.mark.parametrize(
    "failure", ["outside-root", "missing", "too-large", "not-image"]
)
async def test_unreadable_image_fails_safely(
    db_session, uploaded_image, monkeypatch, tmp_path, failure
):
    record, tenant = uploaded_image
    if failure == "outside-root":
        record.file_path = str(tmp_path / "secret.png")
    elif failure == "missing":
        record.file_path += ".missing"
    elif failure == "too-large":
        monkeypatch.setattr(settings, "UPLOAD_MAX_SIZE", 8)
    else:
        record.file_ext = ".xlsx"
    await db_session.flush()
    with pytest.raises(BusinessRuleException) as error:
        await file_service.read_chat_image(db_session, record.file_url, tenant=tenant)
    assert error.value.error_code == "AI_IMAGE_NOT_AVAILABLE"


@pytest.mark.parametrize(
    "prefix", ["", "http://127.0.0.1:8000", "http://localhost:9527"]
)
async def test_uploaded_image_becomes_inline_without_mutating_history(prefix):
    body = {
        "messages": [
            {
                "role": "user",
                "parts": [
                    {
                        "type": "file",
                        "url": prefix + "/uploads/tenant-0/test.png",
                        "mediaType": "image/png",
                    }
                ],
            }
        ]
    }
    before = deepcopy(body)
    with patch(
        "app.modules.system.service.file_service.file_service.read_chat_image",
        AsyncMock(return_value=(b"image-bytes", "image/png")),
        create=True,
    ) as read:
        result = await _convert_local_images_to_data_uri(
            body, db=AsyncMock(), tenant=tenant_context()
        )
    assert (
        result["messages"][0]["parts"][0]["url"]
        == "data:image/png;base64," + base64.b64encode(b"image-bytes").decode()
    )
    assert body == before
    assert read.await_args.args[1] == "/uploads/tenant-0/test.png"
    assert read.await_args.kwargs["tenant"].actor_user_id == 1


async def test_external_image_is_not_mapped_to_local_disk():
    body = {
        "messages": [
            {
                "role": "user",
                "parts": [
                    {
                        "type": "file",
                        "url": "https://example.org/picture.png",
                        "mediaType": "image/png",
                    }
                ],
            }
        ]
    }
    with patch(
        "app.modules.system.service.file_service.file_service.read_chat_image",
        AsyncMock(),
        create=True,
    ) as read:
        result = await _convert_local_images_to_data_uri(
            body, db=AsyncMock(), tenant=tenant_context()
        )
    assert result == body
    read.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "/uploads/../secret.png",
        "/uploads/%2e%2e/secret.png",
        "http://localhost/private_uploads/secret.png",
    ],
)
async def test_invalid_local_reference_fails_before_provider(url):
    body = {
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "file", "url": url, "mediaType": "image/png"}],
            }
        ]
    }
    with pytest.raises(BusinessRuleException) as error:
        await _convert_local_images_to_data_uri(
            body, db=AsyncMock(), tenant=tenant_context()
        )
    assert error.value.error_code == "AI_IMAGE_NOT_AVAILABLE"
