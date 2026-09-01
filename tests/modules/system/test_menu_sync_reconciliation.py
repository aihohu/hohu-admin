from sqlalchemy.ext.asyncio import AsyncSession

from app.core.id_generator import next_id
from app.modules.system.models.menu import Menu
from scripts.sync_menus import _reconcile_menu_partitions


def _menu(*, route_name: str, parent_id: int, component: str) -> Menu:
    return Menu(
        tenant_id=0,
        menu_id=next_id(),
        parent_id=parent_id,
        menu_name=route_name,
        menu_type="M" if component == "layout.base" else "C",
        component=component,
        route_name=route_name,
        route_path=f"/{route_name}",
        status="1",
    )


async def test_reconciliation_restores_separate_menu_partitions(
    db_session: AsyncSession,
) -> None:
    system = _menu(route_name="qa_system_root", parent_id=0, component="layout.base")
    auth = _menu(route_name="qa_auth_root", parent_id=0, component="layout.base")
    task = _menu(route_name="qa_task_root", parent_id=0, component="layout.base")
    dept = _menu(
        route_name="qa_system_dept",
        parent_id=system.menu_id,
        component="view.system_dept",
    )
    job = _menu(
        route_name="qa_system_job",
        parent_id=system.menu_id,
        component="view.system_job",
    )
    custom = _menu(
        route_name="qa_custom_child",
        parent_id=system.menu_id,
        component="view.custom",
    )
    db_session.add_all([system, auth, task, dept, job, custom])
    await db_session.flush()

    changed = await _reconcile_menu_partitions(
        db_session,
        partitions={
            "qa_auth_root": {"qa_system_dept": 1},
            "qa_task_root": {"qa_system_job": 2},
        },
    )
    await db_session.flush()

    assert changed is True
    assert (dept.parent_id, dept.order) == (auth.menu_id, 1)
    assert (job.parent_id, job.order) == (task.menu_id, 2)
    assert custom.parent_id == system.menu_id


async def test_reconciliation_is_noop_when_partition_roots_are_missing(
    db_session: AsyncSession,
) -> None:
    child = _menu(
        route_name="qa_known_child",
        parent_id=0,
        component="view.system_user",
    )
    db_session.add(child)
    await db_session.flush()

    changed = await _reconcile_menu_partitions(
        db_session,
        partitions={"qa_missing_root": {"qa_known_child": 1}},
    )

    assert changed is False
    assert child.parent_id is None
