"""An encoded template is released only after the current authority is re-proved."""

import pytest
from sqlalchemy import delete, insert, update

from app.core.exceptions import BusinessException
from app.db import authorization_transfer_read_owner as module
from app.db.authorization_transfer_read_owner import TransferReadOwner
from app.db.base import role_menus
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("fault", ["disabled", "permission", "global", "role-source"])
async def test_template_cannot_deliver_after_encoding_changes_authority(
    import_sessions, monkeypatch, active, fault
):
    factory, plain, tenant = import_sessions
    if fault == "permission":
        # R_SUPER deliberately bypasses ordinary menu permissions. Exercise a
        # real ordinary importer whose explicit import permission can be revoked.
        async with plain() as db:
            await db.execute(
                update(Role).where(Role.role_id == 201).values(role_code="R_IMPORTER")
            )
            await db.commit()
    if active:
        await activate_relations((factory, plain), monkeypatch)
    original = module.file_transform_worker.run
    encoded = []

    async def transform(*args, **kwargs):
        payload = await original(*args, **kwargs)
        encoded.append(payload)
        async with plain() as db:
            if fault == "disabled":
                await db.execute(
                    update(User).where(User.user_id == 101).values(status="2")
                )
            elif fault == "permission":
                await db.execute(
                    delete(role_menus).where(
                        role_menus.c.tenant_id == 0, role_menus.c.menu_id == 401
                    )
                )
            elif fault == "global":
                await db.execute(
                    update(UserFieldPolicyGlobalGuard).values(
                        epoch=UserFieldPolicyGlobalGuard.epoch + 1
                    )
                )
            else:
                # The dictionary labels are unchanged, but the real role source is different.
                await db.execute(
                    delete(Role).where(Role.tenant_id == 0, Role.role_id == 202)
                )
                await db.execute(
                    insert(Role).values(
                        tenant_id=0,
                        role_id=203,
                        role_name="R_USER",
                        role_code="R_USER",
                        data_scope="5",
                        status="1",
                    )
                )
            await db.commit()
        return payload

    monkeypatch.setattr(module.file_transform_worker, "run", transform)
    with pytest.raises(BusinessException):
        await TransferReadOwner(factory).execute(
            tenant=tenant, actor_user_version=1, operation="template"
        )
    assert len(encoded) == 1 and encoded[0].startswith(b"PK")


async def test_template_never_releases_bytes_after_unknown_second_rollback(
    import_sessions, monkeypatch
):
    factory, _, tenant = import_sessions
    original = module.rollback_authorization_session
    calls = []

    async def rollback(db):
        calls.append(True)
        await original(db)
        if len(calls) == 2:
            raise BusinessException(
                503,
                "Injected unknown rollback",
                error_code="AUTHORIZATION_ROLLBACK_UNKNOWN",
            )

    monkeypatch.setattr(module, "rollback_authorization_session", rollback)
    with pytest.raises(BusinessException) as error:
        await TransferReadOwner(factory).execute(
            tenant=tenant, actor_user_version=1, operation="template"
        )
    assert error.value.error_code == "AUTHORIZATION_ROLLBACK_UNKNOWN"
    assert len(calls) == 2
