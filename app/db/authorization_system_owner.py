"""Explicit System candidate owners; production routes are wired in a later slice."""

import asyncio
from functools import partial

from app.constants import REDIS_USER_NAME_PREFIX
from app.core.redis import redis_client
from app.core.tenant_scope import tenant_cache_key
from app.db.authorization_owner import (
    QUARANTINE_KEY,
    PostCommitEffects,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.modules.system.service.authorization_import_candidate import (
    import_authorization_candidate,
)
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)


def require_candidate_session(db):
    """Refuse borrowed/dirty state before taking ownership of its completion."""
    if not isinstance(db, AuthorizationPlanSession) or db.info.get(QUARANTINE_KEY):
        raise _order_error()
    journal = db._authorization_journal
    if (
        db.in_transaction()
        or db.new
        or db.dirty
        or db.deleted
        or journal.phase != "idle"
        or journal.unsafe
        or not journal.valid_bind
    ):
        raise _order_error()


class SystemCandidateOwner:
    async def import_users(self, db, *, tenant, actor_user_version, **request):
        """Complete one import using its server-owned FILE execution identity.

        The caller supplies the existing ArtifactExecution and storage protocol;
        this owner never fabricates an identity to retry an unknown completion.
        """
        require_candidate_session(db)
        try:
            result = await import_authorization_candidate.execute(
                db, tenant=tenant, actor_user_version=actor_user_version, **request
            )
        except BaseException as exc:
            try:
                await rollback_authorization_session(db)
            except Exception:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise
            raise
        await commit_authorization_session(db)
        return result

    async def execute(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        operation,
        payload=None,
        target_id=None,
        expected_snapshot=None,
    ):
        require_candidate_session(db)
        effects = PostCommitEffects()
        invalidate_username = operation == "user.update" and "user_name" in getattr(
            payload, "model_fields_set", ()
        )
        try:
            result = await system_authorization_candidate.execute(
                db,
                tenant=tenant,
                actor_user_version=actor_user_version,
                operation=operation,
                payload=payload,
                target_id=target_id,
                expected_snapshot=expected_snapshot,
            )
            # This is only an audit-display cache; authorization always reads DB.
            if invalidate_username:
                key = tenant_cache_key(
                    tenant, REDIS_USER_NAME_PREFIX.rstrip(":"), target_id
                )
                effects.cache(key, partial(redis_client.delete, key))
        except BaseException as exc:
            try:
                await rollback_authorization_session(db, effects)
            except Exception:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise
            raise
        # Unknown ACK must propagate directly: no retry or second rollback owner.
        await commit_authorization_session(db, effects)
        return result


system_candidate_owner = SystemCandidateOwner()
