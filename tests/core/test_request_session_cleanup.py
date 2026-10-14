"""Request cancellation must finish rollback/close before propagating."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import anyio
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import session as session_module


@pytest.fixture
def tracked_session(monkeypatch):
    calls = []

    async def completed(name):
        await anyio.lowlevel.checkpoint()
        calls.append(name)

    session = AsyncMock()
    session.__aenter__.return_value = session

    async def commit():
        await completed("commit")

    async def rollback():
        await completed("rollback")

    async def close():
        await completed("close")

    session.commit.side_effect = commit
    session.rollback.side_effect = rollback
    session.close.side_effect = close
    monkeypatch.setattr(
        session_module, "AsyncSessionLocal", MagicMock(return_value=session)
    )
    return session, calls


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "generator_close"])
async def test_request_cleanup_completes_without_committing_cancelled_work(
    tracked_session, managed, outcome
):
    session, calls = tracked_session
    request = SimpleNamespace(
        state=SimpleNamespace(platform_transaction_managed=managed)
    )
    dependency = session_module.get_db(request)
    assert await anext(dependency) is session
    platform = session_module.get_platform_db(request, db=session) if managed else None
    if platform:
        assert await anext(platform) is session
    target = platform or dependency
    if outcome == "success":
        with pytest.raises(StopAsyncIteration):
            await anext(target)
    elif outcome == "failure":
        with pytest.raises(ValueError, match="original failure"):
            await target.athrow(ValueError("original failure"))
    elif outcome == "generator_close":
        with anyio.CancelScope() as scope:
            scope.cancel()
            await target.aclose()
    else:
        with anyio.CancelScope() as scope:
            scope.cancel()
            with pytest.raises(asyncio.CancelledError):
                await target.athrow(asyncio.CancelledError())
    if platform:
        with anyio.CancelScope() as scope:
            if outcome in {"cancel", "generator_close"}:
                scope.cancel()
            await dependency.aclose()
    assert calls == (
        ["commit", "close"] if outcome == "success" else ["rollback", "close"]
    )


async def test_cancelled_request_returns_postgres_connection_and_rolls_back(
    monkeypatch,
):
    """A single-connection pool remains usable after repeated request disconnects."""
    engine = create_async_engine(
        session_module.settings.DATABASE_URL, pool_size=1, max_overflow=0
    )
    metadata = MetaData()
    probe = Table("test_disconnect_" + uuid4().hex, metadata, Column("value", Integer))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", factory)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        for index in range(3):
            request = SimpleNamespace(state=SimpleNamespace())
            dependency = session_module.get_db(request)
            session = await anext(dependency)
            try:
                await session.execute(insert(probe).values(value=index))
                assert engine.pool.checkedout() == 1
                with anyio.CancelScope() as scope:
                    scope.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await dependency.athrow(asyncio.CancelledError())
                assert engine.pool.checkedout() == 0
            finally:
                # Also rescue connections when this regression runs against old code.
                await dependency.aclose()
                await session.close()
            async with engine.connect() as connection:
                assert (await connection.execute(select(probe.c.value))).all() == []
        assert engine.pool.checkedout() == 0
    finally:
        # A failing regression may exhaust the pool under test.
        cleanup_engine = create_async_engine(
            session_module.settings.DATABASE_URL, poolclass=NullPool
        )
        try:
            async with cleanup_engine.begin() as connection:
                await connection.run_sync(metadata.drop_all)
        finally:
            await cleanup_engine.dispose()
            await engine.dispose()
