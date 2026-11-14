from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessException
from app.modules.ai.service.stream_delivery import StreamDelivery


async def test_stream_probes_every_batch_and_bounds_epoch_renewals():

    checker = AsyncMock(
        return_value={
            "authority": {"schema": 2, "tenant_epoch": 1},
            "statements": 10,
            "decoded_bytes": 100,
        }
    )
    probe = AsyncMock(
        return_value={"unchanged": False, "statements": 3, "decoded_bytes": 100}
    )
    sender = StreamDelivery(checker, probe)
    with pytest.raises(BusinessException):
        await sender.send("data: x\n\n")
    assert checker.await_count <= 3
    assert probe.await_count <= 3


async def test_stream_budget_refuses_over_limit_before_next_delivery():

    checker = AsyncMock(
        return_value={
            "authority": {"schema": 2, "tenant_epoch": 1},
            "statements": 10,
            "decoded_bytes": 100,
        }
    )
    probe = AsyncMock(
        return_value={"unchanged": True, "statements": 3, "decoded_bytes": 100}
    )
    sender = StreamDelivery(checker, probe)
    for _ in range(512):
        await sender.send("x")
    with pytest.raises(BusinessException):
        await sender.send("x")
    assert probe.await_count == 512


async def test_stream_rechecks_internal_event_proof_and_counts_full_read_cost():

    checker = AsyncMock(
        return_value={
            "authority": {"schema": 2, "tenant_epoch": 1},
            "statements": 4097,
            "decoded_bytes": 100,
        }
    )
    sender = StreamDelivery(checker, AsyncMock())
    with pytest.raises(BusinessException):
        await sender.send("data: x\n\n", force_full=True)
