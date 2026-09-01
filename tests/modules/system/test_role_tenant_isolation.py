"""Dual-tenant regression tests for tenant-local RBAC roots."""

import pytest

from app.constants import DATA_SCOPE_ALL
from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.system.models.role import Role
from app.modules.system.service.role_service import role_service
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_role_codes_can_repeat_but_role_ids_cannot_cross_tenants(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="role-b")
    marker = next_id()
    role_a = Role(
        tenant_id=0,
        role_name=f"Shared role {marker}",
        role_code=f"R_SHARED_{marker}",
        data_scope=DATA_SCOPE_ALL,
        status="1",
    )
    role_b = Role(
        tenant_id=tenant_b.tenant_id,
        role_name=role_a.role_name,
        role_code=role_a.role_code,
        data_scope=DATA_SCOPE_ALL,
        status="1",
    )
    db_session.add_all([role_a, role_b])
    await db_session.flush()

    tenant_a_ctx = tenant_context()
    tenant_b_ctx = tenant_context(tenant_id=tenant_b.tenant_id)
    assert role_a in await role_service.get_all_roles(db_session, tenant=tenant_a_ctx)
    assert role_b in await role_service.get_all_roles(db_session, tenant=tenant_b_ctx)
    with pytest.raises(NotFoundException):
        await role_service.get_role_detail(
            db_session, role_b.role_id, tenant=tenant_a_ctx
        )
