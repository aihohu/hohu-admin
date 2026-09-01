"""Dual-tenant regression tests for department trees."""

import pytest

from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.system.models.dept import Dept
from app.modules.system.service.dept_service import dept_service
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_department_tree_and_direct_lookup_are_tenant_scoped(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="dept-b")
    marker = next_id()
    dept_a = Dept(
        tenant_id=0,
        dept_name=f"Shared dept {marker}",
        ancestors="0",
        order_num=1,
        status="1",
    )
    dept_b = Dept(
        tenant_id=tenant_b.tenant_id,
        dept_name=dept_a.dept_name,
        ancestors="0",
        order_num=1,
        status="1",
    )
    db_session.add_all([dept_a, dept_b])
    await db_session.flush()

    tenant_a_ctx = tenant_context()
    tenant_b_ctx = tenant_context(tenant_id=tenant_b.tenant_id)
    assert dept_a in await dept_service.get_all(db_session, tenant=tenant_a_ctx)
    assert dept_b in await dept_service.get_all(db_session, tenant=tenant_b_ctx)
    with pytest.raises(NotFoundException):
        await dept_service.get_by_id(db_session, dept_b.dept_id, tenant=tenant_a_ctx)
