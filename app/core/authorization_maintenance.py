"""Offline writer-stop attestations required before dormant maintenance writers.

There is deliberately no production observer or default 'stopped' answer here.
The deployment owner supplies its trusted observer outside every DB transaction.
"""

import asyncio
import re
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.exceptions import BusinessException

_ACTIONS = frozenset(
    {
        "principal.create",
        "principal.permissions",
        "seed.system",
        "seed.agents",
        "seed.settings",
        "migration",
    }
)
_REQUIRED = frozenset({"api", "worker", "scheduler", "web-write-ingress"})
_FENCES = weakref.WeakKeyDictionary()


def _required():
    return BusinessException(
        503,
        "需要当前部署的停写与版本屏障证明",
        error_code="AUTHORIZATION_MAINTENANCE_FENCE_REQUIRED",
    )


@dataclass(frozen=True, slots=True)
class StoppedWritersEvidence:
    deployment_id: str
    component_version: str
    build_sha: str
    manifest_digest: str
    observed_at: datetime
    stopped_surfaces: frozenset[str]
    active_writer_count: int
    ingress_fenced: bool


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class MaintenanceFence:
    action: str
    evidence: StoppedWritersEvidence


async def acquire_maintenance_fence(observer, *, action, expected_build_sha, db=None):
    """Ask a configured deployment observer; caller must have no open DB work."""
    if (
        (db is not None and db.in_transaction())
        or action not in _ACTIONS
        or not callable(observer)
        or type(expected_build_sha) is not str
        or re.fullmatch(r"[0-9a-f]{40}", expected_build_sha) is None
    ):
        raise _required()
    async with asyncio.timeout(3):
        evidence = await observer()
    now = datetime.now(UTC)
    if (
        type(evidence) is not StoppedWritersEvidence
        or type(evidence.deployment_id) is not str
        or not 1 <= len(evidence.deployment_id) <= 128
        or evidence.component_version != "0.2.0"
        or evidence.build_sha != expected_build_sha
        or type(evidence.manifest_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", evidence.manifest_digest) is None
        or type(evidence.observed_at) is not datetime
        or evidence.observed_at.tzinfo is None
        or not 0 <= (now - evidence.observed_at).total_seconds() <= 30
        or type(evidence.stopped_surfaces) is not frozenset
        or not _REQUIRED <= evidence.stopped_surfaces
        or type(evidence.active_writer_count) is not int
        or evidence.active_writer_count != 0
        or evidence.ingress_fenced is not True
    ):
        raise _required()
    fence = MaintenanceFence(action, evidence)
    remaining = 30 - (now - evidence.observed_at).total_seconds()
    _FENCES[fence] = (
        asyncio.current_task(),
        asyncio.get_running_loop().time() + remaining,
    )
    return fence


def require_maintenance(fence, action):
    """Check a server-issued, current-task permit; a request dict is never proof."""
    if (
        type(fence) is not MaintenanceFence
        or action not in _ACTIONS
        or fence.action != action
    ):
        raise _required()
    issued = _FENCES.get(fence)
    if (
        issued is None
        or issued[0] is not asyncio.current_task()
        or asyncio.get_running_loop().time() >= issued[1]
        or not 0
        <= (datetime.now(UTC) - fence.evidence.observed_at).total_seconds()
        < 30
    ):
        raise _required()
    return fence.evidence
