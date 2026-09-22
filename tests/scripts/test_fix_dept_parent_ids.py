"""fix_dept_parent_ids: backfill parent_id from the ancestors chain."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models.dept import Dept
from scripts.fix_dept_parent_ids import fix_dept_parent_ids


async def _add(
    db: AsyncSession,
    *,
    dept_id: int,
    name: str,
    ancestors: str,
    parent_id: int | None = None,
) -> None:
    db.add(
        Dept(
            tenant_id=0,
            dept_id=dept_id,
            parent_id=parent_id,
            dept_name=name,
            ancestors=ancestors,
            order_num=0,
            status="1",
        )
    )


async def _get(db: AsyncSession, dept_id: int) -> Dept:
    return (
        await db.execute(
            select(Dept).where(Dept.tenant_id == 0, Dept.dept_id == dept_id)
        )
    ).scalar_one()


async def test_backfills_parent_from_ancestors_last_segment(
    db_session: AsyncSession,
) -> None:
    await _add(db_session, dept_id=9001, name="root", ancestors="0")
    await _add(db_session, dept_id=9002, name="branch", ancestors="0,9001")
    await _add(db_session, dept_id=9003, name="team", ancestors="0,9001,9002")
    await _add(
        db_session, dept_id=9004, name="already-ok", ancestors="0,9001", parent_id=9001
    )
    await db_session.flush()

    fixed = await fix_dept_parent_ids(db_session)

    # At least the two rows created here (the shared dev DB may contribute its
    # own legacy NULL-parent rows inside the rolled-back transaction).
    assert fixed >= 2
    assert (await _get(db_session, 9001)).parent_id is None
    assert (await _get(db_session, 9002)).parent_id == 9001
    assert (await _get(db_session, 9003)).parent_id == 9002
    assert (await _get(db_session, 9004)).parent_id == 9001

    assert await fix_dept_parent_ids(db_session) == 0  # idempotent
