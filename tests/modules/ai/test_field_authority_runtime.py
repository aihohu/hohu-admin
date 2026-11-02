"""Database authority must be read under the actual tenant barrier."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.exceptions import AuthorizationException
from app.modules.ai.service.field_authority_runtime import AIFieldAuthority
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.user_fields import CATALOG_REVISION


def session(*, mode="legacy", epoch=3):
    state = SimpleNamespace(
        mode=mode,
        ever_enforced=mode == "enforcing",
        epoch=epoch,
        active_manifest_digest=None,
        catalog_revision=CATALOG_REVISION,
        catalog_digest=catalog_digest(),
    )
    return SimpleNamespace(scalar=AsyncMock(side_effect=[state, 2]))


@pytest.mark.asyncio
async def test_current_requires_barrier_before_database_read():
    tx = Mock(spec=["assert_session", "assert_barrier"])
    tx.assert_barrier.side_effect = RuntimeError("no barrier")
    db = session()
    with pytest.raises(RuntimeError, match="no barrier"):
        await AIFieldAuthority().current(
            db, tx, tenant=SimpleNamespace(tenant_id=7, actor_user_id=11)
        )
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_epoch_stops_existing_run():
    service, tx = AIFieldAuthority(), Mock(spec=["assert_session", "assert_barrier"])
    tenant = SimpleNamespace(tenant_id=7, actor_user_id=11)
    frozen = await service.current(session(), tx, tenant=tenant)
    with pytest.raises(AuthorizationException) as caught:
        await service.require(session(epoch=4), tx, tenant=tenant, frozen=frozen)
    assert caught.value.error_code == "FIELD_AUTHORIZATION_CHANGED"


@pytest.mark.asyncio
async def test_legacy_evidence_cannot_become_enforcing_proof():
    with pytest.raises(AuthorizationException) as caught:
        await AIFieldAuthority().current(
            session(mode="enforcing"),
            Mock(spec=["assert_session", "assert_barrier"]),
            tenant=SimpleNamespace(tenant_id=7, actor_user_id=11),
        )
    assert caught.value.error_code == "FIELD_POLICY_UNAVAILABLE"
