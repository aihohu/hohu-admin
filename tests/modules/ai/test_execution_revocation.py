"""Live authorization at the write transaction's final boundary."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.core.exceptions import AuthorizationException
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.service.execution_authorization_service import (
    ensure_current_write_authority,
)
from app.modules.system.models.user import User
from tests.tenant_helpers import tenant_context


@pytest.mark.parametrize("failure", ["tool", "ai", "scope", "session"])
async def test_revocation_after_execution_is_rejected(failure):
    tenant = tenant_context()
    current = SimpleNamespace(
        user_id=1, user_name="actor", auth_version=2 if failure == "session" else 1
    )
    deps = SimpleNamespace(
        tenant=tenant,
        user=SimpleNamespace(user_id=1, auth_version=1),
        data_scope_hash="before",
    )
    meta = AiToolMeta(
        name="user.update",
        summary="Update a user",
        agent="user_mgmt",
        required_perms=("system:user:edit",),
        risk="high",
        readonly=False,
    )
    with (
        patch(
            "app.modules.ai.service.execution_authorization_service.load_live_user_authority",
            AsyncMock(return_value=current),
        ),
        patch(
            "app.modules.ai.service.execution_authorization_service.ensure_ai_chat_use",
            side_effect=AuthorizationException(error_code="AI_CHAT_PERMISSION_DENIED")
            if failure == "ai"
            else None,
        ),
        patch(
            "app.modules.ai.service.execution_authorization_service.agent_authorization_service.tool_permissions",
            return_value=set() if failure == "tool" else {"system:user:edit"},
        ),
        patch(
            "app.modules.ai.service.execution_authorization_service.agent_authorization_service.authorize_agent_access",
            AsyncMock(),
        ),
        patch(
            "app.modules.ai.service.execution_authorization_service.result_projection_service.compute_data_scope_hash",
            AsyncMock(return_value="changed" if failure == "scope" else "before"),
        ),
    ):
        with pytest.raises(AuthorizationException):
            await ensure_current_write_authority(AsyncMock(), deps=deps, meta=meta)


async def test_revocation_rolls_back_flushed_business_write(db_session, monkeypatch):
    """A denial must undo the actual SQL write, not merely hide its response."""
    actor = User(
        tenant_id=0,
        user_name="test_commit_revocation",
        nickname="before-revocation",
        hashed_password="unused-test-placeholder",
        status="1",
        auth_version=1,
    )
    db_session.add(actor)
    await db_session.flush()
    actor_id, original = actor.user_id, actor.nickname
    deps = SimpleNamespace(user=actor, tenant=tenant_context(), trace_id=None)
    meta = AiToolMeta(
        name="test.revoke_write",
        summary="Test write",
        agent="user_mgmt",
        required_perms=("system:user:edit",),
        risk="high",
        readonly=False,
    )

    class TransactionSession:
        def begin(self):
            return db_session.begin_nested()

        def __getattr__(self, name):
            return getattr(db_session, name)

    @asynccontextmanager
    async def session_factory():
        yield TransactionSession()

    async def write_user(ctx):
        await ctx.db.execute(
            update(User)
            .where(User.user_id == actor_id)
            .values(nickname="uncommitted-revocation-test")
        )
        return ToolResult.success(data={"updated": True})

    async def revoked(db, **_kwargs):
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == actor_id))
            == "uncommitted-revocation-test"
        )
        raise AuthorizationException("权限已撤回", error_code="AI_TOOL_PERM_DENIED")

    async def no_timeout(awaitable, **_kwargs):
        return await awaitable

    monkeypatch.setattr(executor, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        executor,
        "build_tool_context",
        lambda _deps, db, _meta, **_kw: SimpleNamespace(db=db),
    )
    monkeypatch.setattr(executor, "with_l3_timeout", no_timeout)
    monkeypatch.setattr(executor, "ensure_current_write_authority", revoked)
    for name in ("clear_failures", "decr_quota", "_record_perm_denied_for_ip"):
        monkeypatch.setattr(executor, name, AsyncMock())
    result = await executor._invoke_tool_fn(
        SimpleNamespace(meta=meta, fn=write_user), {}, deps, "test-revocation"
    )
    assert result.ok is False
    assert result.error_code == "AI_TOOL_PERM_DENIED"
    assert (
        await db_session.scalar(select(User.nickname).where(User.user_id == actor_id))
        == original
    )
