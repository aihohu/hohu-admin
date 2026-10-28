"""Real readonly Gateway calls retain the original JWT and release DB before IO."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_service import chat_service
from app.modules.auth.service import access_identity
from tests.modules.ai.test_atomic_prepared_completion import authenticated_actor
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


@pytest.fixture
async def read_case(completion_case):
    case = completion_case
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        action = await db.get(AiPreparedAction, case.action_id)
        deps = await chat_service.build_chat_deps(
            db,
            actor,
            agent_code="user_mgmt",
            conversation_id=case.conversation_id,
            trace_id=action.trace_id,
        )
        deps.conversation_id = case.conversation_id
        deps.source_user_message_id = action.source_user_message_id
        db.expunge_all()
    return deps


async def invoke(deps, monkeypatch):
    monkeypatch.setattr(executor, "_safe_write_query_cache", lambda *_a, **_k: None)
    return await executor._invoke_tool_fn(
        ToolRegistry.get().find("user.count"), {}, deps, "a" * 64
    )


@pytest.mark.parametrize(
    "tool,args",
    [
        ("user.count", {}),
        ("user.list", {}),
        ("user.lookup", {"user_name": "admin"}),
        ("user.stats", {"group_by": "status"}),
        ("user.distinct", {"field": "status"}),
        ("user.role_lookup", {"query": "admin"}),
        ("user.dept_lookup", {"query": "test"}),
        ("role.count", {}),
        ("role.list", {}),
        ("role.lookup", {"query": "admin"}),
        ("role.menu_lookup", {"role_id": 1, "query": "user"}),
        ("role.agent_lookup", {"role_id": 1, "query": "user"}),
        ("dept.count", {}),
        ("dept.list", {}),
        ("dept.lookup", {"query": "test"}),
    ],
)
async def test_fixed_read_tools(read_case, tool, args):
    deps = read_case
    identity = access_identity(deps.user)
    args = dict(args)
    if "role_id" in args:
        args["role_id"] = deps.user.roles[0].role_id
    result = await AuthorizationRuntime(engine).chat.read_tool(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        conversation_id=deps.conversation_id,
        source_id=deps.source_user_message_id,
        trace_id=deps.trace_id,
        agent_code=ToolRegistry.get().find(tool).meta.agent,
        scope_hash=deps.data_scope_hash,
        tool=tool,
        args=args,
    )
    assert result.ok


async def test_readonly_original_version_is_not_upgraded(read_case, monkeypatch):
    identity = access_identity(read_case.user)
    read_case.user.__dict__["_verified_access_identity"] = replace(
        identity, user_version=identity.user_version + 1
    )
    monkeypatch.setattr(executor, "clear_failures", AsyncMock())
    result = await invoke(read_case, monkeypatch)
    assert not result.ok


async def test_readonly_redis_runs_after_rollback_without_commit(
    read_case, monkeypatch
):
    active, commits, observations = set(), [], []

    def begin(conn):
        active.add(id(conn))

    def rollback(conn):
        active.discard(id(conn))

    def commit(conn):
        active.discard(id(conn))
        commits.append(True)

    async def clear(*_args, **_kwargs):
        observations.append(len(active))

    monkeypatch.setattr(executor, "clear_failures", clear)
    hooks = (("begin", begin), ("rollback", rollback), ("commit", commit))
    for name, hook in hooks:
        event.listen(engine.sync_engine, name, hook)
    try:
        result = await invoke(read_case, monkeypatch)
    finally:
        for name, hook in hooks:
            event.remove(engine.sync_engine, name, hook)
    assert result.ok
    assert observations == [0]
    assert commits == []


@pytest.mark.parametrize("change", ["scope", "source", "tenant", "agent"])
async def test_readonly_revalidates_bound_context(read_case, monkeypatch, change):
    deps = read_case
    monkeypatch.setattr(executor, "clear_failures", AsyncMock())
    if change == "scope":
        deps.data_scope_hash = "a" * 64
    elif change == "tenant":
        deps.tenant = replace(deps.tenant, tenant_id=999)
    elif change == "agent":
        deps.agent = None
    else:
        async with AsyncSessionLocal() as db, db.begin():
            source = await db.get(AiMessage, deps.source_user_message_id)
            source.is_active = False
    result = await invoke(deps, monkeypatch)
    assert not result.ok
    executor.clear_failures.assert_not_awaited()


async def test_readonly_auxiliary_redis_timeout_keeps_result(read_case, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(executor, "clear_failures", blocked)
    task = asyncio.create_task(invoke(read_case, monkeypatch))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        done, _ = await asyncio.wait({task}, timeout=3)
        assert task in done
        assert task.result().ok
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_actual_preflight_never_commits(read_case):
    commits = []

    def committed(_conn):
        commits.append(True)

    event.listen(engine.sync_engine, "commit", committed)
    try:
        outcome = await executor._run_dry_run(
            ToolRegistry.get().find("user.create"), {"user_name": "admin"}, read_case
        )
    finally:
        event.remove(engine.sync_engine, "commit", committed)
    assert outcome.failure.error_code == "AI_PREVIEW_REJECTED"
    assert commits == []


async def test_preflight_original_version_is_not_upgraded(read_case):
    identity = access_identity(read_case.user)
    read_case.user.__dict__["_verified_access_identity"] = replace(
        identity, user_version=identity.user_version + 1
    )
    outcome = await executor._run_dry_run(
        ToolRegistry.get().find("user.create"), {"user_name": "admin"}, read_case
    )
    assert outcome.failure is not None
    assert outcome.failure.error_code != "AI_PREVIEW_REJECTED"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("user.create", {"user_name": "admin"}),
        ("user.update", {"user_id": 1, "nickname": "preview only"}),
        ("user.reset_password", {"user_id": 1}),
        ("user.batch_delete", {"user_ids": [1]}),
        ("user.update_dept", {"user_id": 1, "dept_assignments": []}),
        ("user.update_roles", {"user_id": 1, "role_ids": []}),
        (
            "role.create",
            {
                "role_name": "Preview",
                "role_code": "preview_candidate",
                "data_scope": "1",
                "status": "1",
            },
        ),
        ("role.update", {"role_id": 1, "role_name": "Preview"}),
        ("role.update_menus", {"role_id": 1, "menu_ids": []}),
        ("role.update_agents", {"role_id": 1, "agent_ids": []}),
        ("dept.create", {"parent_id": None, "dept_name": "Preview"}),
        ("dept.update", {"dept_id": 1, "dept_name": "Preview"}),
        ("dept.move", {"dept_id": 1, "new_parent_id": None}),
        ("user.export", {"reason": "Preview", "user_name": "admin"}),
    ],
)
async def test_fixed_preflight_adapters(read_case, completion_case, tool, args):
    deps, case = read_case, completion_case
    args = dict(args)
    for field, value in (
        ("user_id", deps.user.user_id),
        ("role_id", case.default_role_id),
        ("dept_id", case.department_id),
    ):
        if field in args:
            args[field] = value
    if "user_ids" in args:
        args["user_ids"] = [deps.user.user_id]
    registered = ToolRegistry.get().find(tool)
    deps.agent = SimpleNamespace(code=registered.meta.agent)
    outcome = await executor._run_dry_run(registered, args, deps)
    if outcome.failure:
        assert outcome.failure.error_code not in {
            "AI_INTERNAL_ERROR",
            "FIELD_POLICY_LOCK_ORDER",
            "AI_TOOL_PERM_DENIED",
        }, outcome.failure
    else:
        assert outcome.count is not None
