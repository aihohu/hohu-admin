import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal, engine


@pytest.fixture
async def db_session() -> AsyncSession:
    """Rollback all platform model test data with an outer transaction."""
    async with engine.connect() as connection:
        outer = await connection.begin()
        try:
            async with AsyncSessionLocal(bind=connection) as session:
                yield session
        finally:
            await outer.rollback()
    try:
        await engine.dispose()
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise
