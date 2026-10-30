"""Single-use initial-install permits; these never attest stopped processes."""

import asyncio
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.authorization_maintenance import _required

_ISSUED = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class FreshDatabaseEvidence:
    database_oid: int
    build_sha: str
    manifest_digest: str
    observed_at: datetime


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class BootstrapFence:
    evidence: FreshDatabaseEvidence


def issue_bootstrap(evidence, engine):
    """Only the fixed offline database observer calls this issuance boundary."""
    if type(evidence) is not FreshDatabaseEvidence or evidence.database_oid <= 0:
        raise _required()
    permit = BootstrapFence(evidence)
    _ISSUED[permit] = (asyncio.current_task(), engine, False)
    return permit


def require_bootstrap(permit, action, *, engine=None, consume=False):
    if type(permit) is not BootstrapFence or action != "seed.system":
        raise _required()
    issued = _ISSUED.get(permit)
    if (
        issued is None
        or issued[0] is not asyncio.current_task()
        or issued[2]
        or (engine is not None and issued[1] is not engine)
        or not 0
        <= (datetime.now(UTC) - permit.evidence.observed_at).total_seconds()
        < 30
    ):
        raise _required()
    if consume:
        _ISSUED[permit] = (*issued[:2], True)
    return permit.evidence
