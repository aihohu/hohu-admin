"""Missing heads never reopen legacy image delivery; each test owns its schema."""

from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.core.authorization_io import file_transform_worker
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.contracts import primitive
from app.core.field_policy.documents import catalog_document, catalog_parts, digest
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus
from app.modules.system.field_policy.delivery_catalog import DELIVERY_CATALOG_REVISION
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.field_policy import (
    FieldCatalogObject,
    FieldCatalogSnapshot,
    FieldPolicyHead,
    FieldPolicyObjectState,
)
from app.modules.system.models.file import File
from app.modules.system.models.menu import Menu
from tests.modules.ai.test_chat_images import PNG
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize("phase", ["before", "during_io"])
async def test_other_tenant_head_blocks_missing_local_image_head(
    system_sessions, tmp_path, monkeypatch, phase
):
    factory, plain = system_sessions
    catalog = production_catalog()
    path = tmp_path / (uuid4().hex + ".png")
    path.write_bytes(PNG)
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    file_url = "/uploads/tenant-0/" + path.name
    async with plain() as db, db.begin():
        assert await db.get(FieldPolicyHead, 0) is None
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=999991,
                menu_name="chat",
                menu_type="F",
                status="1",
                permission="ai:chat:use",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=999991)
        )
        await db.execute(
            insert(File).values(
                tenant_id=0,
                file_id=9001,
                owner_user_id=101,
                original_name="owned.png",
                file_name=path.name,
                file_path=str(path),
                file_url=file_url,
                file_size=len(PNG),
                file_ext=".png",
                mime_type="image/png",
                business_type="ai-chat-image",
                del_flag="0",
            )
        )

    async def install_other_head():
        async with plain() as db, db.begin():
            action, adapter = catalog_parts(catalog)
            await db.execute(
                insert(FieldCatalogSnapshot).values(
                    catalog_digest=catalog.digest,
                    schema_version=2,
                    catalog_revision=DELIVERY_CATALOG_REVISION,
                    action_catalog_digest=action,
                    adapter_digest=adapter,
                    canonical_document=catalog_document(catalog),
                )
            )
            for obj in catalog.objects.values():
                await db.execute(
                    insert(FieldCatalogObject).values(
                        catalog_digest=catalog.digest,
                        object_id=obj.object_id,
                        domain_kind=obj.domain_kind,
                        schema_digest=digest(primitive(obj)),
                        adapter_version=obj.scope_adapter,
                    )
                )
            await db.execute(
                insert(FieldPolicyHead).values(
                    tenant_id=1, catalog_digest=catalog.digest
                )
            )
            await db.execute(
                insert(FieldPolicyObjectState),
                [
                    {"tenant_id": 1, "object_id": obj, "catalog_digest": catalog.digest}
                    for obj in catalog.objects
                ],
            )

    calls = []
    original = file_transform_worker.run

    async def work(*args, **kwargs):
        calls.append(True)
        result = await original(*args, **kwargs)
        if phase == "during_io":
            await install_other_head()
        return result

    monkeypatch.setattr(file_transform_worker, "run", work)
    if phase == "before":
        await install_other_head()
    with pytest.raises(BusinessException) as caught:
        await AuthorizationRuntime(plain.kw["bind"]).upload.read_chat_image(
            tenant=TENANT, actor_user_version=1, file_url=file_url
        )
    assert caught.value.error_code == "FIELD_POLICY_UNAVAILABLE"
    assert caught.value.code == 503
    assert len(calls) == (0 if phase == "before" else 1)
    # The fixture drops its exact disposable schema, preserving append-only guards.
