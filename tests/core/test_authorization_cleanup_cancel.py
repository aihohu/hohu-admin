"""Cleanup failures cannot replace an external cancellation."""

import asyncio

import anyio
import pytest

from app.core.exceptions import BusinessException
from app.db.authorization_owner import _QUARANTINED, rollback_authorization_session
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.session import is_session_quarantined


@pytest.mark.parametrize("boundary", ["except", "finally"])
async def test_rollback_unknown_preserves_active_cancellation(boundary, monkeypatch):
    db = AuthorizationPlanSession()
    cancelled = asyncio.CancelledError("external cancellation")

    async def unknown():
        raise RuntimeError("rollback acknowledgement lost")

    monkeypatch.setattr(db, "rollback", unknown)
    try:
        with pytest.raises(asyncio.CancelledError) as error:
            if boundary == "except":
                try:
                    raise cancelled
                except BaseException:
                    await rollback_authorization_session(db)
                    raise
            else:
                try:
                    raise cancelled
                finally:
                    await rollback_authorization_session(db)
        assert error.value is cancelled
        assert is_session_quarantined(db)
    finally:
        held = _QUARANTINED.get(id(db))
        if held is not None:
            await asyncio.shield(held[2])
        await db.close()


async def test_rollback_unknown_without_cancellation_remains_unknown(monkeypatch):
    db = AuthorizationPlanSession()

    async def unknown():
        raise RuntimeError("rollback acknowledgement lost")

    monkeypatch.setattr(db, "rollback", unknown)
    with pytest.raises(BusinessException) as error:
        await rollback_authorization_session(db)
    assert error.value.error_code == "AUTHORIZATION_ROLLBACK_UNKNOWN"
    assert is_session_quarantined(db)
    await db.close()


@pytest.mark.parametrize("source", ["task", "anyio"])
async def test_external_cancellation_survives_cleanup_failure(source, monkeypatch):
    db = AuthorizationPlanSession()
    entered = asyncio.Event()
    after = []

    async def unknown():
        raise RuntimeError("rollback acknowledgement lost")

    monkeypatch.setattr(db, "rollback", unknown)

    async def body():
        try:
            entered.set()
            await anyio.sleep_forever()
        except BaseException:
            await rollback_authorization_session(db)
            raise
        after.append(True)

    try:
        if source == "task":
            task = asyncio.create_task(body())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        else:
            with anyio.CancelScope() as scope:
                scope.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await body()
        assert is_session_quarantined(db) and not after
    finally:
        held = _QUARANTINED.get(id(db))
        if held is not None:
            await asyncio.shield(held[2])
        await db.close()
