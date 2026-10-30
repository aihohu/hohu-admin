"""Committed owned rows let scheduler tests exercise fresh production sessions."""

import pytest
from sqlalchemy import delete

from app.db.session import AsyncSessionLocal, engine
from app.modules.job.models.job import SysJob


@pytest.fixture(name="db_session")
async def committed_job_session():
    async with AsyncSessionLocal() as session:
        session.info["owned_jobs"] = []
        try:
            yield session
        finally:
            await session.rollback()
            for tenant_id, job_id, key in session.info["owned_jobs"]:
                await session.execute(
                    delete(SysJob).where(
                        SysJob.tenant_id == tenant_id,
                        SysJob.job_id == job_id,
                        SysJob.job_key == key,
                    )
                )
            await session.commit()
    await engine.dispose()
