"""Maintenance gates require current, independently observed stopped writers."""

from datetime import UTC, datetime
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
