"""Dual-tenant regression tests for the System user aggregate."""

import pytest

from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserQuery
from app.modules.system.service.user_service import user_service
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_user_reads_and_direct_ids_are_tenant_scoped(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="user-b")
    marker = next_id()
    user_a = User(
        tenant_id=0,
        user_name=f"shared-user-{marker}",
        nickname="Tenant A",
        hashed_password="x",
        status="1",
    )
    user_b = User(
        tenant_id=tenant_b.tenant_id,
        user_name=user_a.user_name,
        nickname="Tenant B",
        hashed_password="x",
        status="1",
    )
    db_session.add_all([user_a, user_b])
    await db_session.flush()

    tenant_a_ctx = tenant_context(actor_user_id=user_a.user_id)
    tenant_b_ctx = tenant_context(
        tenant_id=tenant_b.tenant_id, actor_user_id=user_b.user_id
    )
    page_a = await user_service.get_user_list(
        db_session, UserQuery(user_name=user_a.user_name), tenant=tenant_a_ctx
    )
    page_b = await user_service.get_user_list(
        db_session, UserQuery(user_name=user_a.user_name), tenant=tenant_b_ctx
    )

    assert [row.user_id for row in page_a.records] == [user_a.user_id]
    assert [row.user_id for row in page_b.records] == [user_b.user_id]
    assert not await user_service.user_exists(
        db_session, user_b.user_id, tenant=tenant_a_ctx
    )
    with pytest.raises(NotFoundException):
        await user_service.delete_user(db_session, user_b.user_id, tenant=tenant_a_ctx)
