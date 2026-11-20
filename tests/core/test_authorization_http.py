"""HTTP read sessions cannot re-enter after handing off to a fixed owner."""

import asyncio

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.exceptions import BusinessException
from app.db.authorization_http import release_request_reads
from app.db.authorization_owner import _QUARANTINED
from app.db.request_session import RequestSession, request_reads_released
from app.db.session import is_session_quarantined
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_handoff_detaches_releases_and_permanently_rejects_reentry(
    system_sessions,
):
    _, plain = system_sessions
    async with RequestSession(bind=plain.kw["bind"], expire_on_commit=False) as db:
        user = await db.get(User, 101)
        assert db.in_transaction()
        await release_request_reads(db)
        assert user.user_name == "planner"
        assert not db.in_transaction()
        assert request_reads_released(db)
        for operation in (lambda: db.execute(select(User)), db.commit, db.begin):
            with pytest.raises(BusinessException) as error:
                await operation()
            assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


@pytest.mark.parametrize("mutation", ["dirty", "flushed", "core"])
async def test_handoff_refuses_any_prior_write_even_after_flush(
    system_sessions, mutation
):
    _, plain = system_sessions
    async with RequestSession(bind=plain.kw["bind"], expire_on_commit=False) as db:
        if mutation == "core":
            with pytest.raises(BusinessException):
                await db.execute(
                    update(User).where(User.user_id == 101).values(nickname="x")
                )
        else:
            user = await db.get(User, 101)
            user.nickname = "x"
            if mutation == "flushed":
                with pytest.raises(BusinessException):
                    await db.flush()
        with pytest.raises(BusinessException):
            await release_request_reads(db)
        await db.rollback()
        if mutation != "dirty":
            with pytest.raises(BusinessException):
                await release_request_reads(db)


async def test_handoff_bounds_unacknowledged_rollback_and_blocks_dispatch(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    async with plain() as observer:
        schema = await observer.scalar(text("SELECT current_schema()"))
    isolated = create_async_engine(
        plain.kw["bind"].url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
        connect_args={"server_settings": {"search_path": schema}},
    )
    db = RequestSession(bind=isolated, expire_on_commit=False)
    await db.get(User, 101)
    release = asyncio.Event()
    original = db.rollback

    async def blocked():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        await original()

    monkeypatch.setattr(db, "rollback", blocked)
    dispatched = []

    async def handoff():
        await release_request_reads(db)
        dispatched.append(True)

    task = asyncio.create_task(handoff())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.8)
        assert task in done
        with pytest.raises(BusinessException) as error:
            task.result()
        assert error.value.error_code == "AUTHORIZATION_ROLLBACK_UNKNOWN"
        assert is_session_quarantined(db) and request_reads_released(db)
        assert id(db) in _QUARANTINED
        assert isolated.pool.checkedout() == 1
        assert not dispatched
        with pytest.raises(BusinessException):
            await db.execute(select(User))
        with pytest.raises(BusinessException):
            await release_request_reads(db)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        held = _QUARANTINED.get(id(db))
        if held is not None:
            async with asyncio.timeout(2):
                await asyncio.shield(held[2])
        await db.close()
        try:
            async with RequestSession(bind=isolated) as fresh:
                assert await fresh.get(User, 101) is not None
        finally:
            await isolated.dispose()
