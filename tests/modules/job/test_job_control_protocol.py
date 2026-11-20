"""Current HTTP accepts canonical string IDs and rejects unknown form queries."""

import pytest

from app.modules.job.api import job
from app.modules.system.api import config
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def protocol_http(http_system):
    client, _ = http_system
    client._transport.app.include_router(job.router, prefix="/job")
    client._transport.app.include_router(config.router, prefix="/config")
    return client


@pytest.mark.parametrize("surface", ["job", "config"])
async def test_form_query_is_closed_in_the_actual_http_adapter(protocol_http, surface):
    response = await protocol_http.get(
        f"/{surface}/form", params={"mode": "create", "unknown": "ignored-before"}
    )
    assert response.status_code in {400, 422}, response.text


async def test_job_metadata_and_schedule_accept_canonical_json_string_ids(
    protocol_http,
):
    client = protocol_http
    form = (await client.get("/job/form", params={"mode": "create"})).json()["data"]
    added = await client.post(
        "/job/add",
        json={
            "jobKey": "test_task",
            "jobName": "Original",
            "cronExpression": "0 * * * *",
            "formReceipt": form["formReceipt"],
        },
    )
    assert added.status_code == 200, added.text
    identifier = (await client.get("/job/list")).json()["data"]["records"][0]["jobId"]
    assert type(identifier) is str
    form = (
        await client.get("/job/form", params={"mode": "update", "recordId": identifier})
    ).json()["data"]
    changed = await client.put(
        "/job/update",
        json={
            "jobId": identifier,
            "remark": "Sparse",
            "formReceipt": form["formReceipt"],
        },
    )
    assert changed.status_code == 200, changed.text
    scheduled = await client.put(
        "/job/schedule", json={"jobId": identifier, "cronExpression": "5 * * * *"}
    )
    assert scheduled.status_code == 200, scheduled.text
