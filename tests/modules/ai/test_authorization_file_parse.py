"""Real uploaded-file parsing releases DB work and rechecks current authority."""

import pytest
from sqlalchemy import event, select, update

from app.core.authorization_io import file_transform_worker
from app.core.exceptions import BusinessException
from app.db import authorization_read_confirmation_owner as owner_module
from app.db.session import engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
from app.modules.ai.agents.tools import file_access
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.system.models.file import File
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor
from tests.modules.ai.test_preview_atomic_completion import preview_case as preview_case


@pytest.fixture
async def parse_case(preview_case):
    case = preview_case
    async with AsyncSessionLocal() as db, db.begin():
        case.deps.agent = await db.scalar(
            select(AiAgent).where(AiAgent.code == "shared")
        )
        db.expunge(case.deps.agent)
        setting = await db.scalar(
            select(SystemSetting).where(
                SystemSetting.tenant_id == case.deps.tenant.tenant_id,
                SystemSetting.setting_key == "ai:enabled_tools",
            )
        )
        assert setting is not None
        before = setting.setting_value
        setting.setting_value = '["file.parse"]'
    try:
        yield case
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(SystemSetting)
                .where(
                    SystemSetting.tenant_id == case.deps.tenant.tenant_id,
                    SystemSetting.setting_key == "ai:enabled_tools",
                )
                .values(setting_value=before)
            )


async def invoke(case):
    return await executor._read_tool(
        ToolRegistry.get().find("file.parse"), {"file_id": str(case.file_id)}, case.deps
    )


async def test_file_parse_worker_has_no_active_database_transaction(
    parse_case, monkeypatch
):
    active, observed = set(), []
    original = file_access.read_local_bounded

    def read(*args):
        observed.append(len(active))
        return original(*args)

    monkeypatch.setattr(file_access, "read_local_bounded", read)

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    for name, hook in (("begin", begin), ("rollback", end), ("commit", end)):
        event.listen(engine.sync_engine, name, hook)
    try:
        result = await invoke(parse_case)
    finally:
        for name, hook in (("begin", begin), ("rollback", end), ("commit", end)):
            event.remove(engine.sync_engine, name, hook)
    assert result.ok and result.data["rows"] == 1
    assert result.projection.subject_refs == (
        {"type": "file", "id": str(parse_case.file_id)},
    )
    assert observed == [0]


@pytest.mark.parametrize("mutation", ["path", "deleted", "version", "source"])
async def test_file_parse_rechecks_after_worker(parse_case, monkeypatch, mutation):
    original = file_transform_worker.run
    calls = []

    async def mutate(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        calls.append(function.__name__)
        async with AsyncSessionLocal() as db, db.begin():
            if mutation in {"path", "deleted"}:
                await db.execute(
                    update(File)
                    .where(File.file_id == parse_case.file_id)
                    .values(
                        **(
                            {"file_path": "changed.csv"}
                            if mutation == "path"
                            else {"del_flag": "2"}
                        )
                    )
                )
            elif mutation == "version":
                await db.execute(
                    update(User)
                    .where(User.user_id == parse_case.deps.user.user_id)
                    .values(auth_version=User.auth_version + 1)
                )
            else:
                await db.execute(
                    update(AiMessage)
                    .where(
                        AiMessage.message_id == parse_case.deps.source_user_message_id
                    )
                    .values(is_active=False)
                )
        return result

    monkeypatch.setattr(file_transform_worker, "run", mutate)
    with pytest.raises(BusinessException):
        await invoke(parse_case)
    assert len(calls) == 1


async def test_forced_file_parse_confirmation_uses_worker_outside_plan(
    parse_case, monkeypatch
):
    deps = parse_case.deps
    deps.injection_hit = True
    parsed, statuses, writes, active, observed = [], [], [], set(), []
    original = file_transform_worker.run

    async def worker(function, *args, **kwargs):
        parsed.append(function.__name__)
        observed.append(len(active))
        return await original(function, *args, **kwargs)

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(
                context.execution_options.get("_authorization_plan_permit") is not None
            )

    async def approve(event):
        if not isinstance(event, ConfirmationRequiredEvent):
            return
        for _ in range(2):
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=deps.tenant)
                response = await confirm_module.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=event.confirmation_id, action="approve"
                    ),
                    db=db,
                    current_user=actor,
                )
            assert response.data.status == "succeeded"
            statuses.append(response.data.status)
        raise RuntimeError("stop after fixed file read confirmation")

    monkeypatch.setattr(file_transform_worker, "run", worker)
    deps.signal_event = approve
    hooks = (
        ("begin", begin),
        ("rollback", end),
        ("commit", end),
        ("before_cursor_execute", capture),
    )
    for name, hook in hooks:
        event.listen(engine.sync_engine, name, hook)
    try:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await executor.execute_tool(
                "file.parse", {"file_id": str(parse_case.file_id)}, deps
            )
    finally:
        for name, hook in hooks:
            event.remove(engine.sync_engine, name, hook)
    assert parsed == ["parse_uploaded_file"]
    assert statuses == ["succeeded", "succeeded"]
    assert observed == [0]
    assert writes and all(writes)


@pytest.mark.parametrize("committed", [False, True])
async def test_file_confirmation_unknown_commit_does_not_repeat_parse(
    parse_case, monkeypatch, committed
):
    deps = parse_case.deps
    deps.injection_hit = True

    async def stop(event):
        if isinstance(event, ConfirmationRequiredEvent):
            raise RuntimeError("leave file confirmation pending")

    deps.signal_event = stop
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor.execute_tool(
            "file.parse", {"file_id": str(parse_case.file_id)}, deps
        )
    async with AsyncSessionLocal() as db:
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.conversation_id == deps.conversation_id,
                AiPreparedAction.execute_tool_name == "file.parse",
            )
        )
    calls = []
    original_worker, original_commit = (
        file_transform_worker.run,
        owner_module.commit_authorization_session,
    )

    async def worker(function, *args, **kwargs):
        calls.append(function.__name__)
        return await original_worker(function, *args, **kwargs)

    async def fault(db):
        if committed:
            await original_commit(db)
        raise BusinessException(
            503, "injected uncertainty", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(file_transform_worker, "run", worker)
    monkeypatch.setattr(owner_module, "commit_authorization_session", fault)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=deps.tenant)
        response = await confirm_module.confirm_tool(
            ConfirmRequest(confirmation_id=action.confirmation_id, action="approve"),
            db=db,
            current_user=actor,
        )
    assert response.data.status == "running"
    async with AsyncSessionLocal() as db:
        current = await db.get(AiPreparedAction, action.action_id)
        assert current.status == ("succeeded" if committed else "pending_confirmation")
    if committed:
        monkeypatch.setattr(
            owner_module, "commit_authorization_session", original_commit
        )
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(db, tenant=deps.tenant)
            response = await confirm_module.confirm_tool(
                ConfirmRequest(
                    confirmation_id=action.confirmation_id, action="approve"
                ),
                db=db,
                current_user=actor,
            )
        assert response.data.status == "succeeded"
    assert calls == ["parse_uploaded_file"]
