"""A Redis acknowledgement is a notification outcome, never task execution."""

from unittest.mock import AsyncMock

import pytest

from app.core import scheduler
from app.core.exceptions import BusinessException
from app.modules.job.api import job
from tests.modules.job.test_job_manual_dispatch_owner import signed_job as signed_job
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize("subscribers", [0, 1, "timeout"])
async def test_run_response_reports_notification_outcome_without_retry(
    http_system, monkeypatch, subscribers
):
    client, _ = http_system
    client._transport.app.include_router(job.router, prefix="/job")
    redis = AsyncMock()
    redis.publish.return_value = 0 if subscribers == "timeout" else subscribers
    monkeypatch.setattr(scheduler, "redis_client", redis)
    form = await client.get("/job/form", params={"mode": "create"})
    added = await client.post(
        "/job/add",
        json={
            "jobKey": "test_task",
            "jobName": "Safe",
            "cronExpression": "0 * * * *",
            "formReceipt": form.json()["data"]["formReceipt"],
        },
    )
    assert added.status_code == 200, added.text
    row = (await client.get("/job/list")).json()["data"]["records"][0]
    redis.publish.reset_mock()
    if subscribers == "timeout":
        redis.publish.side_effect = TimeoutError("publish not acknowledged")
    response = await client.post("/job/run/" + row["jobId"])
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "notificationStatus": "notified" if subscribers == 1 else "unconfirmed"
    }
    assert redis.publish.await_count == 1


@pytest.mark.parametrize(
    "hint", [{"tenant_id": False}, {"tenant_id": "0"}, {"job_id": True}]
)
async def test_dispatch_routing_hints_reject_noncanonical_identifiers(signed_job, hint):
    runtime, _, _, token = signed_job
    with pytest.raises(BusinessException):
        await runtime.job.claim(dispatch=token, runner_id="strict-hint", **hint)
