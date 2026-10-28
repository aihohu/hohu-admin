"""External preparation must not retain request or authorization transactions."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.supervisor import quota, router
from app.modules.ai.api import chat as chat_module
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.model import AiModel
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_authorization_chat_ingress import (
    ingress as ingress,
)
from tests.modules.ai.test_authorization_chat_ingress import (
    ingress_facts,
)
from tests.modules.ai.test_authorization_stream_finalize import run_chat


async def test_actual_chat_dns_has_no_database_transaction(
    completion_case, monkeypatch
):
    active = set()
    observations = []

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    async def dns(*_args, **_kwargs):
        observations.append(len(active))
        return True

    listeners = (("begin", begin), ("commit", end), ("rollback", end))
    for name, callback in listeners:
        event.listen(engine.sync_engine, name, callback)
    try:
        await run_chat(completion_case, monkeypatch, egress_check=dns)
    finally:
        for name, callback in listeners:
            event.remove(engine.sync_engine, name, callback)
    assert observations and not any(observations), observations


async def test_model_selection_original_version_is_rechecked_before_dns(
    ingress, monkeypatch
):
    calls = []

    async def dns(*_args, **_kwargs):
        calls.append(True)
        return True

    monkeypatch.setattr(provider_egress, "is_model_allowed", dns)
    with pytest.raises(BusinessException):
        await AuthorizationRuntime(engine).chat.select_model(
            tenant=ingress["tenant"],
            actor_user_version=ingress["actor_user_version"] + 1,
            model_ref=str(ingress["authority"].model.model_id),
        )
    assert calls == []


async def test_dns_result_cannot_upgrade_a_changed_model_snapshot(ingress, monkeypatch):
    model_id = ingress["authority"].model.model_id

    async def dns(*_args, **_kwargs):
        async with AsyncSessionLocal() as db, db.begin():
            model = await db.get(AiModel, model_id)
            model.is_enabled = False
        return True

    monkeypatch.setattr(provider_egress, "is_model_allowed", dns)
    try:
        choice = await AuthorizationRuntime(engine).chat.select_model(
            tenant=ingress["tenant"],
            actor_user_version=ingress["actor_user_version"],
            model_ref=str(model_id),
        )
        request = dict(
            ingress, authority=replace(ingress["authority"], model=choice.selection)
        )
        with pytest.raises(BusinessException):
            await AuthorizationRuntime(engine).chat.start_run(**request)
        assert await ingress_facts(ingress) == ([], [])
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            model = await db.get(AiModel, model_id)
            model.is_enabled = True


async def test_model_dns_has_cumulative_deadline_and_no_commit(ingress, monkeypatch):
    commits = []

    def commit(_connection):
        commits.append(True)

    async def dns(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(provider_egress, "is_model_allowed", dns)
    event.listen(engine.sync_engine, "commit", commit)
    try:
        async with asyncio.timeout(6):
            with pytest.raises(TimeoutError):
                await AuthorizationRuntime(engine).chat.select_model(
                    tenant=ingress["tenant"],
                    actor_user_version=ingress["actor_user_version"],
                    model_ref=str(ingress["authority"].model.model_id),
                )
    finally:
        event.remove(engine.sync_engine, "commit", commit)
    assert commits == []


async def test_actual_chat_redis_and_guard_have_no_database_transaction(
    completion_case, monkeypatch
):
    active = set()
    observations = []

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    async def redis_check(*_args, **_kwargs):
        observations.append(("redis", len(active)))
        return False

    acquire = chat_module.chat_run_guard.acquire

    async def guard(*args, **kwargs):
        observations.append(("guard", len(active)))
        return await acquire(*args, **kwargs)

    for name in (
        "is_ip_blacklisted",
        "check_user_disabled",
        "is_injection_hit_conversation",
    ):
        monkeypatch.setattr(chat_module, name, redis_check)
    monkeypatch.setattr(chat_module.chat_run_guard, "acquire", guard)
    listeners = (("begin", begin), ("commit", end), ("rollback", end))
    for name, callback in listeners:
        event.listen(engine.sync_engine, name, callback)
    try:
        await run_chat(completion_case, monkeypatch)
    finally:
        for name, callback in listeners:
            event.remove(engine.sync_engine, name, callback)
    assert {name for name, _ in observations} == {"redis", "guard"}
    assert not any(count for _, count in observations), observations


async def test_chat_redis_preparation_deadline_prevents_start(
    completion_case, monkeypatch
):
    release = asyncio.Event()
    entered = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(chat_module, "is_ip_blacklisted", blocked)
    task = asyncio.create_task(run_chat(completion_case, monkeypatch))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        completed, _ = await asyncio.wait({task}, timeout=3)
        assert task in completed
        with pytest.raises(BusinessException) as exc:
            task.result()
        assert exc.value.error_code == "AI_PREPARATION_TIMEOUT"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_actual_supervisor_and_quota_have_no_database_transaction(
    completion_case, monkeypatch
):
    active = set()
    observations = []

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    async def count(*_args, **_kwargs):
        observations.append(("quota", len(active)))
        return 0

    async def increment(*_args, **_kwargs):
        observations.append(("increment", len(active)))
        return 1

    async def llm(*_args, **_kwargs):
        observations.append(("supervisor", len(active)))
        return '{"agent_code": "user_mgmt"}'

    monkeypatch.setattr(quota, "get_daily_count", count)
    monkeypatch.setattr(quota, "increment_daily_count", increment)
    monkeypatch.setattr(router, "call_llm_text", llm)
    monkeypatch.setattr(
        chat_module.model_authorization_service,
        "create_model_instance",
        Mock(return_value=object()),
    )
    listeners = (("begin", begin), ("commit", end), ("rollback", end))
    for name, callback in listeners:
        event.listen(engine.sync_engine, name, callback)
    try:
        await run_chat(completion_case, monkeypatch, agent_code="auto")
    finally:
        for name, callback in listeners:
            event.remove(engine.sync_engine, name, callback)
    assert {name for name, _ in observations} == {"quota", "increment", "supervisor"}
    assert not any(count for _, count in observations), observations
