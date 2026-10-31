"""JWT resume reaches fixed recovery for real DB and FILE completions."""

from dataclasses import replace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import setup_exception_handlers
from app.core.security import create_access_token
from app.db import session
from app.db.request_session import RequestSession
from app.db.session import engine
from app.modules.ai.agents.gateway import atomic_execution, executor
from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
from app.modules.ai.api import confirm, resume
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.auth import service
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from tests.fixture_session import FixtureSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
    count_real_handler,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import preview_case as preview_case


async def resume_http(case, monkeypatch):
    monkeypatch.setattr(resume.settings, "AI_SSE_RESUME_ENABLED", True)
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    monkeypatch.setattr(
        session,
        "AsyncSessionLocal",
        async_sessionmaker(engine, class_=RequestSession, expire_on_commit=False),
    )
    async with FixtureSessionLocal() as db:
        version = await db.scalar(
            select(User.auth_version).where(User.user_id == case.tenant.actor_user_id)
        )
    token = create_access_token(
        subject=str(case.tenant.actor_user_id),
        tenant_id=case.tenant.tenant_id,
        tenant_version=case.tenant.tenant_version,
        user_version=version,
    )
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(resume.router, prefix="/ai/chat")
    locks = []
    previous = set(atomic_execution._QUARANTINED_SESSIONS)

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if "FOR SHARE" in sql or "FOR UPDATE" in sql:
            locks.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/ai/chat/resume",
                params={"confirmation_id": case.confirmation_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        assert '"type": "tool_call_result"' in response.text
        assert '"ok": true' in response.text
        assert locks and "sys_user_field_policy_global_guard" in locks[0]
        assert set(atomic_execution._QUARANTINED_SESSIONS) == previous
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)


async def test_jwt_resume_observes_db_completion_once(completion_case, monkeypatch):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    await resume_http(case, monkeypatch)
    assert len(case.calls) == 1


async def test_jwt_resume_observes_file_completion_once(preview_case, monkeypatch):
    preview = preview_case
    case = preview.case
    confirmations, exports, calls = [], [], []
    original = system_ai_tool_adapter.apply

    async def counted(db, tx, command, work):
        calls.append(True)
        result = await original(db, tx, command, work)
        if result.ok:
            exports.append(result.data["exportId"])
        return result

    async def confirm_export(event):
        if isinstance(event, ConfirmationRequiredEvent):
            confirmations.append(event.confirmation_id)
            async with FixtureSessionLocal() as db:
                actor = await authenticated_actor(case, db)
                response = await confirm.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=event.confirmation_id, action="approve"
                    ),
                    db=db,
                    current_user=actor,
                )
                assert response.data.status == "succeeded"

    monkeypatch.setattr(system_ai_tool_adapter, "apply", counted)
    preview.deps.signal_event = confirm_export
    try:
        result = await executor.execute_tool(
            "user.export", {"reason": "resume regression"}, preview.deps
        )
        assert result.ok and len(confirmations) == len(exports) == len(calls) == 1
        await resume_http(replace(case, confirmation_id=confirmations[0]), monkeypatch)
        assert len(calls) == 1
    finally:
        async with FixtureSessionLocal() as db, db.begin():
            await db.execute(
                delete(UserExportTask).where(UserExportTask.export_id.in_(exports))
            )
