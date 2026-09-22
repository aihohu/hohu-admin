# ruff: noqa: T201

"""Backfill sys_dept.parent_id from ancestors (idempotent, all tenants).

Historical seeds wrote only the ancestors chain, leaving parent_id NULL on
non-root departments, which desynchronizes dept/list parentId and any
parent_id-driven logic from the actual hierarchy.

Usage:
    cd hohu-admin
    python scripts/fix_dept_parent_ids.py
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.modules.system.models.dept import Dept


async def fix_dept_parent_ids(db: AsyncSession) -> int:
    """Set parent_id from the last ancestors segment; returns rows updated."""
    rows = (
        (
            await db.execute(
                select(Dept).where(Dept.parent_id.is_(None), Dept.ancestors.isnot(None))
            )
        )
        .scalars()
        .all()
    )
    fixed = 0
    for dept in rows:
        parts = [p for p in (dept.ancestors or "").split(",") if p]
        if not parts:
            continue
        last = parts[-1]
        if last == "0":
            continue  # genuine root
        dept.parent_id = int(last)
        fixed += 1
    if fixed:
        await db.flush()
    return fixed


async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as db:
        fixed = await fix_dept_parent_ids(db)
        await db.commit()
    await engine.dispose()
    print(f"Backfilled parent_id on {fixed} departments.")


if __name__ == "__main__":
    asyncio.run(main())
