"""Maintenance gates require current, independently observed stopped writers."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.authorization_maintenance import (
    MaintenanceFence,
    StoppedWritersEvidence,
    acquire_maintenance_fence,
    require_maintenance,
)
from app.core.exceptions import BusinessException


def evidence(**changes):
    values = {
        "deployment_id": "isolated-test",
        "component_version": "0.2.0",
        "build_sha": "a" * 40,
        "manifest_digest": "b" * 64,
        "observed_at": datetime.now(UTC),
        "stopped_surfaces": frozenset(
            {"api", "worker", "scheduler", "web-write-ingress"}
        ),
        "active_writer_count": 0,
        "ingress_fenced": True,
    }
    values.update(changes)
    return StoppedWritersEvidence(**values)


async def test_cannot_observe_external_runtime_while_a_transaction_is_open():
    called = False

    async def observe():
        nonlocal called
        called = True
        return evidence()

    with pytest.raises(BusinessException):
        await acquire_maintenance_fence(
            observe,
            action="principal.create",
            expected_build_sha="a" * 40,
            db=SimpleNamespace(in_transaction=lambda: True),
        )
    assert called is False


@pytest.mark.parametrize(
    "changes",
    [
        {"active_writer_count": 1},
        {"ingress_fenced": False},
        {"component_version": "0.1.6"},
        {"build_sha": "c" * 40},
        {"stopped_surfaces": frozenset({"api"})},
    ],
)
async def test_incomplete_evidence_fails_closed(changes):
    async def observe():
        return evidence(**changes)

    with pytest.raises(BusinessException):
        await acquire_maintenance_fence(
            observe, action="principal.create", expected_build_sha="a" * 40
        )


async def test_only_issued_matching_action_is_a_fence():
    async def observe():
        return evidence()

    fence = await acquire_maintenance_fence(
        observe, action="principal.create", expected_build_sha="a" * 40
    )
    assert require_maintenance(fence, "principal.create") == fence.evidence
    with pytest.raises(BusinessException):
        require_maintenance(fence, "principal.permissions")
    with pytest.raises(BusinessException):
        require_maintenance(
            MaintenanceFence("principal.create", fence.evidence), "principal.create"
        )


async def test_old_observation_does_not_receive_a_fresh_thirty_seconds(monkeypatch):
    async def observe():
        return evidence(observed_at=datetime.now(UTC) - timedelta(seconds=29))

    fence = await acquire_maintenance_fence(
        observe, action="principal.create", expected_build_sha="a" * 40
    )
    loop = asyncio.get_running_loop()
    later = loop.time() + 2
    with monkeypatch.context() as patch:
        patch.setattr(loop, "time", lambda: later)
        with pytest.raises(BusinessException) as caught:
            require_maintenance(fence, "principal.create")
    assert caught.value.error_code == "AUTHORIZATION_MAINTENANCE_FENCE_REQUIRED"


async def test_fence_cannot_be_transferred_to_another_task():
    async def observe():
        return evidence()

    fence = await acquire_maintenance_fence(
        observe, action="principal.create", expected_build_sha="a" * 40
    )

    async def use():
        with pytest.raises(BusinessException):
            require_maintenance(fence, "principal.create")

    await asyncio.create_task(use())
