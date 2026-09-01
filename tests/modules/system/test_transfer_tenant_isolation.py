"""Tenant isolation for user import/export task records."""

from app.core.id_generator import next_id
from app.modules.system.constants import ExportTaskStatus
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask
from app.modules.system.service.user_export_service import get_export_task
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_export_task_id_cannot_be_reused_by_another_tenant(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="transfer-b")
    user_a = User(
        tenant_id=0,
        user_name=f"transfer-a-{next_id()}",
        nickname="A",
        hashed_password="x",
        status="1",
    )
    user_b = User(
        tenant_id=tenant_b.tenant_id,
        user_name=f"transfer-b-{next_id()}",
        nickname="B",
        hashed_password="x",
        status="1",
    )
    db_session.add_all([user_a, user_b])
    await db_session.flush()
    task_b = UserExportTask(
        tenant_id=tenant_b.tenant_id,
        operator_id=user_b.user_id,
        filter_snapshot={},
        reason="tenant isolation regression",
        status=ExportTaskStatus.CREATED,
    )
    db_session.add(task_b)
    await db_session.flush()

    tenant_a_ctx = tenant_context(actor_user_id=user_a.user_id)
    tenant_b_ctx = tenant_context(
        tenant_id=tenant_b.tenant_id, actor_user_id=user_b.user_id
    )
    assert (
        await get_export_task(
            db_session,
            str(task_b.export_id),
            operator_id=user_a.user_id,
            allow_cross_owner=True,
            tenant=tenant_a_ctx,
        )
        is None
    )
    assert (
        await get_export_task(
            db_session,
            str(task_b.export_id),
            operator_id=user_b.user_id,
            tenant=tenant_b_ctx,
        )
        is task_b
    )
