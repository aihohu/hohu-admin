"""Bounded completion for explicit authorization transaction owners.

Unknown ACKs are never replayed or treated as rollback evidence. The quarantined
session and pending operation remain strongly referenced until invalidation.
"""

import asyncio
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.db.session import SESSION_QUARANTINED_KEY

QUARANTINE_KEY = "authorization_owner_quarantined"
_QUARANTINED = {}
_COMMIT_SECONDS = 8.0
_CLEANUP_SECONDS = 0.25


class PostCommitEffects:
    """Coalesced auxiliary cache invalidation; owners release only after ACK."""

    def __init__(self):
        self._cache = {}

    def cache(self, key, callback):
        if not isinstance(key, str) or len(key) > 512 or not callable(callback):
            raise ValueError("Invalid auxiliary cache effect")
        if key not in self._cache and len(self._cache) >= 256:
            raise BusinessException(
                422,
                "Too many cache effects",
                error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
            )
        self._cache[key] = callback

    def discard(self):
        self._cache.clear()

    async def confirmed(self):
        callbacks, self._cache = tuple(self._cache.values()), {}
        try:
            async with asyncio.timeout(1):
                for callback in callbacks:
                    with suppress(Exception):
                        await callback()
        except TimeoutError:
            pass


async def _quarantine(db, pending):
    if db.info.get(QUARANTINE_KEY):
        return
    db.info[QUARANTINE_KEY] = True
    db.info[SESSION_QUARANTINED_KEY] = True

    async def invalidate_when_idle():
        await asyncio.gather(pending, return_exceptions=True)
        # Trusted infrastructure only: the candidate session deliberately rejects
        # its public invalidate method. No business SQL/callback is permitted.
        await AsyncSession.invalidate(db)

    cleanup = asyncio.create_task(invalidate_when_idle())
    key = id(db)
    _QUARANTINED[key] = (db, pending, cleanup)

    def finished(task):
        try:
            task.result()
        except BaseException:
            # Failed invalidation never permits a normal close/pool return.
            return
        _QUARANTINED.pop(key, None)

    cleanup.add_done_callback(finished)
    with suppress(Exception, asyncio.CancelledError):
        async with asyncio.timeout(_CLEANUP_SECONDS):
            await asyncio.shield(cleanup)


def _require(db):
    if not isinstance(db, AuthorizationPlanSession) or db.info.get(QUARANTINE_KEY):
        raise _order_error()


async def commit_authorization_session(db, effects=None):
    """Confirm the sole outer COMMIT, or quarantine without replaying business."""
    _require(db)
    effects = effects or PostCommitEffects()
    journal = db._authorization_journal
    if (
        journal.phase != "closed"
        or journal.unsafe
        or not journal.valid_bind
        or db.new
        or db.dirty
        or db.deleted
        or not db.in_transaction()
    ):
        effects.discard()
        journal.reject()
    # The spec gives ACK a separate phase after the successfully closed proof.
    # Closed sessions reject business SQL, so this cannot extend a writer scope.
    journal.deadline = asyncio.get_running_loop().time() + _COMMIT_SECONDS
    pending = asyncio.create_task(db.commit())
    try:
        done, _ = await asyncio.wait({pending}, timeout=_COMMIT_SECONDS)
        if pending not in done:
            raise TimeoutError("Commit acknowledgement timed out")
        pending.result()
    except BaseException as exc:
        effects.discard()
        if not pending.done():
            pending.cancel()
        await _quarantine(db, pending)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise BusinessException(
            503,
            "Commit acknowledgement is unknown; do not replay",
            error_code="AUTHORIZATION_COMMIT_UNKNOWN",
        ) from None
    await effects.confirmed()


async def rollback_authorization_session(db, effects=None):
    """Bound cleanup; retain a failed/cancellation-resistant operation in quarantine."""
    if effects is not None:
        effects.discard()
    _require(db)
    pending = asyncio.create_task(db.rollback())
    try:
        done, _ = await asyncio.wait({pending}, timeout=_CLEANUP_SECONDS)
        if pending not in done:
            raise TimeoutError("Rollback acknowledgement timed out")
        pending.result()
    except BaseException as exc:
        if not pending.done():
            pending.cancel()
        await _quarantine(db, pending)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise BusinessException(
            503,
            "Rollback acknowledgement unavailable; session quarantined",
            error_code="AUTHORIZATION_ROLLBACK_UNKNOWN",
        ) from None
