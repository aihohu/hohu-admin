"""HTTP and Redis use current contracts and carry the original signed notice."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import scheduler
from app.core.exceptions import BusinessException
from app.modules.job.api import job
from tests.modules.job.test_job_dispatch import issue
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_http_requires_form_and_forwards_the_owner_dispatch(
    http_system, monkeypatch
):
    client, _ = http_system
    client._transport.app.include_router(job.router, prefix="/job")
    publish = AsyncMock()
    monkeypatch.setattr(job, "notify_job_changed", AsyncMock())
    monkeypatch.setattr(job, "notify_manual_trigger", publish)
    body = {"jobKey": "test_task", "jobName": "Safe", "cronExpression": "0 * * * *"}
    denied = await client.post("/job/add", json=body)
    assert denied.status_code == 409, denied.text
    form = await client.get("/job/form", params={"mode": "create"})
    assert form.status_code == 200, form.text
    added = await client.post(
        "/job/add", json=body | {"formReceipt": form.json()["data"]["formReceipt"]}
    )
    assert added.status_code == 200, added.text
    listed = await client.get("/job/list")
    assert listed.status_code == 200, listed.text
    row = listed.json()["data"]["records"][0]
    assert "fieldPolicy" in row and "createBy" not in row
    registered = await client.get("/job/registered")
    assert registered.status_code == 200, registered.text
    assert [item["key"] for item in registered.json()["data"]] == ["test_task"]
    result = await client.post("/job/run/" + row["jobId"])
    assert result.status_code == 200, result.text
    (token,) = publish.await_args.args
    assert isinstance(token, str) and token.count(".") == 1


async def test_redis_publishes_only_verified_bounded_notice():
    client = AsyncMock()
    token = issue(now=None)
    await scheduler.notify_manual_trigger(token, redis=client)
    client.publish.assert_awaited_once_with(scheduler.CHANNEL_MANUAL_TRIGGER, token)
    with pytest.raises(BusinessException):
        await scheduler.notify_manual_trigger('{"tenantId":0,"jobId":1}', redis=client)
    assert client.publish.await_count == 1


def test_scheduler_carries_notice_instead_of_bare_identifiers():
    manager = scheduler.SchedulerManager()
    manager._scheduler = MagicMock()
    token = issue(now=None)
    manager.run_now(token)
    assert manager._scheduler.add_job.call_args.kwargs["args"] == [token]
    with pytest.raises(BusinessException):
        manager.run_now('{"tenantId":0,"jobId":1}')
    assert manager._scheduler.add_job.call_count == 1
