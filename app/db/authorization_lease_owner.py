"""The narrow lease CAS exception: one fixed UPDATE and no domain capability."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, text, update

from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    ClaimBinding,
    is_protocol_owner,
)


class LeaseOwner:
    def __init__(self, factory):
        self.factory = factory

    async def renew(self, claim, *, seconds=90):
        if (
            type(claim) is not ClaimBinding
            or not is_protocol_owner(claim.execution_owner, claim.execute_tool_name)
            or type(seconds) is not int
            or not 1 <= seconds <= 120
        ):
            raise _order_error()
        async with self.factory() as db:
            require_candidate_session(db)
            journal = db._authorization_journal
            journal.phase = "preparing"
            journal.deadline = asyncio.get_running_loop().time() + 2
            # Infrastructure-only fixed SQL. No proof handle or callback escapes.
            permit = object()
            journal.permit = permit
            try:
                async with asyncio.timeout(2):
                    await db.execute(
                        text(
                            "SELECT set_config('lock_timeout', '1s', true), set_config('statement_timeout', '2s', true)"
                        ).execution_options(_authorization_plan_permit=permit)
                    )
                    table = AiPreparedAction.__table__
                    statement = (
                        update(table)
                        .where(
                            table.c.tenant_id == claim.tenant_id,
                            table.c.user_id == claim.actor_user_id,
                            table.c.action_id == claim.action_id,
                            table.c.status == "running",
                            table.c.row_version == claim.row_version,
                            table.c.execution_owner == claim.execution_owner,
                            table.c.execution_lease_expires_at > func.clock_timestamp(),
                        )
                        .values(
                            execution_lease_expires_at=func.greatest(
                                table.c.execution_lease_expires_at,
                                func.clock_timestamp() + timedelta(seconds=seconds),
                            )
                        )
                        .returning(table.c.action_id)
                    )
                    renewed = (
                        await db.execute(
                            statement.execution_options(
                                _authorization_plan_permit=permit
                            )
                        )
                    ).scalar_one_or_none() is not None
                    journal.check_time()
                    journal.phase = "closed"
            except BaseException:
                journal.fail()
                journal.permit = None
                await rollback_authorization_session(db)
                raise
            finally:
                journal.permit = None
            await commit_authorization_session(db)
        return renewed
