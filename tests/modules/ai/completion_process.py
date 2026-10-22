"""Cold-process acceptance worker. Only explicit test IDs cross the boundary."""

import asyncio
import json
import os
import sys
from functools import wraps
from pathlib import Path

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.core import redis as redis_module
from app.core.tenant import TenantContext
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.gateway import atomic_execution
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.auth.service import load_live_user_authority
from app.modules.system.models.user import User


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


async def main(request):
    assert os.environ["ENV"] == "test"
    tenant = TenantContext(
        tenant_id=request["tenant_id"],
        tenant_code="default",
        actor_user_id=request["actor_id"],
        tenant_version=1,
        source="access_token",
    )
    emit({"event": "started", "pid": os.getpid(), "mode": request["mode"]})
    load_builtin_tools()
    registered = ToolRegistry.get().find("user.create")
    real_handler = registered.fn

    @wraps(real_handler)
    async def counted(ctx, **args):
        emit(
            {
                "event": "handler_started",
                "pid": await ctx.db.scalar(select(func.pg_backend_pid())),
                "txid": await ctx.db.scalar(select(func.txid_current())),
            }
        )
        result = await real_handler(ctx, **args)
        assert result.ok
        user_id = await ctx.db.scalar(
            select(User.user_id).where(
                User.tenant_id == tenant.tenant_id,
                User.user_name == request["username"],
            )
        )
        emit({"event": "handler_written", "user_id": user_id})
        if request.get("reject_result"):
            return ToolResult.failure("TEST_HANDLER_FAILURE", "Rollback actual write")
        return result

    registered.fn = counted
    armed = False

    def locate(session):
        nonlocal armed
        if armed or not request.get("stage"):
            return
        action = next(
            (
                row
                for row in session.identity_map.values()
                if isinstance(row, AiPreparedAction)
                and row.action_id == request["action_id"]
            ),
            None,
        )
        if action is not None and action.status == request["stage"]:
            armed = True
            emit({"event": "arm", "pid": session.scalar(select(func.pg_backend_pid()))})
            assert sys.stdin.readline().strip() == "armed"

    event.listen(Session, "before_commit", locate)
    try:
        async with AsyncSessionLocal() as db:
            actor = await load_live_user_authority(db, tenant=tenant)
            await ToolRegistry.get().validate_on_startup(db)
            await db.rollback()
        if request["mode"] == "recover":
            # No prior owner, row version, result or in-memory claim supplied.
            result = await atomic_execution.reconcile_prepared_completion(
                request["confirmation_id"], tenant=tenant
            )
            emit({"event": "result", "disposition": result.disposition})
        else:
            async with AsyncSessionLocal() as db:
                actor = await load_live_user_authority(db, tenant=tenant)
                result = await confirm_module.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=request["confirmation_id"], action="approve"
                    ),
                    db=db,
                    current_user=actor,
                )
            emit({"event": "result", "disposition": result.data.status})
    finally:
        event.remove(Session, "before_commit", locate)
        await engine.dispose()
        await redis_module.redis_client.aclose()


if __name__ == "__main__":
    request_data = json.loads(Path(sys.argv[1]).read_text(encoding="utf8"))
    asyncio.run(main(request_data))
