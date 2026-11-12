"""Both physical lock orders preserve fresh publication and dispatch authority."""

import asyncio

import pytest

from app.core.exceptions import BusinessException
from app.core.tenant import TenantLocatorContext
from app.db import authorization_parameter_read_owner as reads
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job.service import authorization_job_lifecycle as lifecycle
from app.modules.system.schemas.user import UpdateProfile
from app.modules.system.service.authorization_configuration_candidate import (
    configuration_authorization_candidate as configuration_candidate,
)
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)
from tests.modules.job.test_job_manual_dispatch_owner import signed_job as signed_job
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_parameter_publication import form, grant_publish, write
from tests.modules.system.test_parameter_read_owner import parameters as parameters
from tests.modules.system.test_parameter_read_owner import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_parameter_read_owner import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize("writer_first", [True, False])
async def test_publication_read_and_change_have_both_real_lock_orders(
    parameters, monkeypatch, writer_first
):
    factory, plain = parameters
    runtime = AuthorizationRuntime(plain.kw["bind"])
    await grant_publish(plain)
    current = await form(factory)
    await write(
        factory,
        "config.publish",
        {"confirmed": True, "expected_revision": current["publicationRevision"]},
        301,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    candidate = (
        configuration_candidate.execute if writer_first else reads.public_parameters
    )

    async def paused(*args, **kwargs):
        result = await candidate(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(
        configuration_candidate if writer_first else reads,
        "execute" if writer_first else "public_parameters",
        paused,
    )

    async def writer():
        return await runtime.control(
            tenant=TENANT,
            actor_user_version=1,
            operation="config.update",
            target_id=301,
            payload={"config_value": "changed"},
        )

    async def reader():
        return await runtime.parameter_read(
            tenant=TenantLocatorContext(0, "plan-0", 1),
            actor_user_version=None,
            operation="config.public",
        )

    tasks = []
    try:
        tasks.append(asyncio.create_task(writer() if writer_first else reader()))
        await asyncio.wait_for(entered.wait(), 4)
        tasks.append(asyncio.create_task(reader() if writer_first else writer()))
        await asyncio.sleep(0.1)
        assert not tasks[1].done(), (
            "the opposite owner must wait for the physical tenant barrier"
        )
        release.set()
        first, second = await asyncio.gather(*tasks)
        assert (second if writer_first else first) == (
            {} if writer_first else {"business.301": "value-301"}
        )
        assert await reader() == {}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("writer_first", [True, False])
async def test_dispatch_claim_and_epoch_write_have_both_real_lock_orders(
    signed_job, monkeypatch, writer_first
):
    runtime, _, _, token = signed_job
    entered, release = asyncio.Event(), asyncio.Event()
    target = (
        system_authorization_candidate
        if writer_first
        else lifecycle.job_lifecycle_candidate
    )
    name = "execute" if writer_first else "claim"
    original = getattr(target, name)

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(target, name, paused)

    async def writer():
        return await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.profile",
            payload=UpdateProfile(nickname="new epoch"),
        )

    async def claim():
        return await runtime.job.claim(dispatch=token, runner_id="both-orders")

    tasks = []
    try:
        tasks.append(asyncio.create_task(writer() if writer_first else claim()))
        await asyncio.wait_for(entered.wait(), 4)
        tasks.append(asyncio.create_task(claim() if writer_first else writer()))
        await asyncio.sleep(0.1)
        assert not tasks[1].done()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if writer_first:
            assert (
                isinstance(results[1], BusinessException)
                and results[1].error_code == "JOB_DISPATCH_STALE"
            )
        else:
            assert not isinstance(results[0], BaseException) and results[0] is not None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
