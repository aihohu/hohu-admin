"""Signed AI downloads use real JWTs and recheck grants under planned locks."""

import asyncio

import pytest
from sqlalchemy import event, func, insert, select, update

from app.core.file_storage import LocalFileStorage
from app.db import authorization_http, authorization_system_owner
from app.db.base import role_menus
from app.modules.ai.api import download
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority
from app.modules.system.ai_tools import user_transfer  # noqa: F401
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_authorization_transfer_http import (
    transfer_http as transfer_http,
)


@pytest.fixture
async def signed_export(transfer_http, import_sessions):
    client, plain = transfer_http
    tenant = import_sessions[2]
    client._transport.app.include_router(download.router, prefix="/ai/download")
    async with plain() as db:
        for mid, permission in enumerate(
            ("ai:chat:use", "system:user:export", "system:user:reset-password"), 801
        ):
            await db.execute(
                insert(Menu).values(
                    tenant_id=0,
                    menu_id=mid,
                    menu_name=permission,
                    permission=permission,
                    menu_type="F",
                    status="1",
                )
            )
            await db.execute(
                insert(role_menus).values(tenant_id=0, role_id=201, menu_id=mid)
            )
        await db.execute(
            insert(AiAgent).values(
                agent_id=901,
                code="user_mgmt",
                name="Users",
                description="Users",
                enabled=True,
            )
        )
        await db.execute(
            insert(RoleAiAgent).values(
                tenant_id=0, role_id=201, agent_id=901, enabled=True
            )
        )
        await db.commit()
    exported = await client.post(
        "/user/export", json={"reason": "signed download test"}
    )
    assert exported.status_code == 200, exported.text
    async with plain() as db:
        identifier = await db.scalar(select(UserExportTask.export_id))
        actor = await load_live_user_authority(db, tenant=tenant)
        lineage = result_projection_service.freeze_lineage(
            tenant=tenant,
            agent_code="user_mgmt",
            tool_codes=["user.export"],
            subject_refs=[{"type": "user_export_task", "id": identifier}],
            data_scope_hash=await result_projection_service.compute_data_scope_hash(
                db, actor
            ),
        )
        token = await result_projection_service.issue_download_token(
            db,
            actor,
            tenant=tenant,
            resource_type="user_export",
            resource_id=identifier,
            lineage=lineage,
        )
        assert token
    return client, plain, identifier, token, exported.content


async def test_signed_download_has_g_first_and_no_commits(signed_export):
    client, plain, identifier, token, data = signed_export
    engine = plain.kw["bind"].sync_engine
    locks, commits = [], []

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    def commit(connection):
        commits.append(connection)

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        response = await client.get(
            f"/ai/download/user-export/{identifier}", params={"token": token}
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    assert response.status_code == 200, response.text
    assert response.content == data
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert not commits


@pytest.mark.parametrize(
    "change", ["invalid_token", "other_resource", "revoke_after_handoff"]
)
async def test_signed_download_rejects_bad_binding_or_late_revocation(
    signed_export, monkeypatch, change
):
    client, plain, identifier, token, _ = signed_export
    if change == "invalid_token":
        token = "invalid"
    elif change == "other_resource":
        identifier = "not-the-signed-resource"
    else:
        original = authorization_http.release_request_reads

        async def release(db):
            await original(db)
            async with plain() as writer:
                await writer.execute(
                    update(User).where(User.user_id == 101).values(auth_version=2)
                )
                await writer.commit()

        monkeypatch.setattr(authorization_http, "release_request_reads", release)
    response = await client.get(
        f"/ai/download/user-export/{identifier}", params={"token": token}
    )
    assert response.status_code == (401 if change == "revoke_after_handoff" else 404), (
        response.text
    )


@pytest.mark.parametrize("first", ["download", "revoke"])
async def test_signed_download_and_rest_revoke_serialize_both_orders(
    signed_export, monkeypatch, first
):
    client, plain, identifier, token, data = signed_export
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = []
    read = LocalFileStorage.read_artifact
    commit = authorization_system_owner.commit_authorization_session

    async def held_read(self, *args, **kwargs):
        reads.append(True)
        if first == "download":
            entered.set()
            await release.wait()
        return await read(self, *args, **kwargs)

    async def held_commit(*args, **kwargs):
        if first == "revoke":
            entered.set()
            await release.wait()
        return await commit(*args, **kwargs)

    monkeypatch.setattr(LocalFileStorage, "read_artifact", held_read)
    monkeypatch.setattr(
        authorization_system_owner, "commit_authorization_session", held_commit
    )
    second = None
    pids = {}

    def sql(_connection, _cursor, statement, _params, _context, _many):
        task = asyncio.current_task()
        if task is first_task or (second is not None and task is second):
            pids["first" if task is first_task else "second"] = (
                _connection.connection.driver_connection.get_server_pid()
            )
        if (
            asyncio.current_task() is second
            and "sys_user_field_policy_state" in statement
            and ("FOR UPDATE" in statement or "FOR SHARE" in statement)
        ):
            waiting.set()

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", sql)

    async def download():
        return await client.get(
            f"/ai/download/user-export/{identifier}", params={"token": token}
        )

    async def revoke():
        return await client.put("/user/101/revoke-sessions")

    first_task = asyncio.create_task(download() if first == "download" else revoke())
    try:
        await asyncio.wait_for(entered.wait(), 4)
        second = asyncio.create_task(revoke() if first == "download" else download())
        await asyncio.wait_for(waiting.wait(), 2)
        async with asyncio.timeout(1), plain() as observer:
            while True:
                blockers = await observer.scalar(
                    select(func.pg_blocking_pids(pids["second"]))
                )
                if pids["first"] in blockers:
                    break
                await asyncio.sleep(0)
        assert not second.done()
        if first == "revoke":
            assert reads == []
        release.set()
        first_response, second_response = await asyncio.wait_for(
            asyncio.gather(first_task, second), 4
        )
        assert first_response.status_code == 200, first_response.text[:200]
        assert second_response.status_code == (200 if first == "download" else 401), (
            second_response.text[:200]
        )
        if first == "download":
            assert first_response.content == data
        assert len(reads) == int(first == "download")
    finally:
        release.set()
        await asyncio.gather(
            first_task,
            *([second] if second is not None else []),
            return_exceptions=True,
        )
        event.remove(engine, "before_cursor_execute", sql)
