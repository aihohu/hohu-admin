"""Upload preparation is lock-free; preview publication owns one complete plan."""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.core import file_storage
from app.core.authorization_file import FileBudget
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.file_storage import ArtifactExecution
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_transfer_cache import cache_preview
from app.modules.ai.service.atomic_completion_service import new_execution_owner
from app.modules.ai.service.authorization_preview_candidate import preview_candidate
from app.modules.system.ai_tools.user_transfer import _load_file_bytes
from app.modules.system.models.file import File
from app.modules.system.service.authorization_ai_user_input import context
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.authorization_transfer_candidate import (
    transfer_candidate,
)


class PreviewOwner:
    def __init__(self, factory):
        self.factory = factory

    async def preview(
        self,
        *,
        file_id,
        reason,
        requested_outcome,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        on_conflict="skip",
        sync_mode="CREATE_ONLY",
    ):
        storage = file_storage.get_file_storage()
        async with self.factory() as db:
            try:
                with (
                    bounded_authorization_reads(db),
                    FileBudget(
                        max_reads=1, max_total_bytes=10 * 1024 * 1024
                    ).activate(),
                ):
                    async with asyncio.timeout(5):
                        ctx = await context(db, tenant, "user.import_preview")
                        data, filename, _ = await _load_file_bytes(ctx, file_id)
                        snapshot = dict(
                            (
                                await db.execute(
                                    select(File.__table__).where(
                                        File.tenant_id == tenant.tenant_id,
                                        File.file_id == int(file_id),
                                    )
                                )
                            )
                            .mappings()
                            .one()
                        )
            finally:
                await rollback_authorization_session(db)
            command = transfer_candidate.prepare_preview(
                tenant=tenant,
                actor_user_version=actor_user_version,
                file_bytes=data,
                filename=filename,
                reason=reason,
                on_conflict=on_conflict,
                execution=ArtifactExecution(
                    tenant.tenant_id,
                    "tc_" + uuid4().hex,
                    new_execution_owner("user.import_preview"),
                ),
            )
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await preview_candidate.execute(
                            db,
                            tx,
                            command,
                            file_snapshot=snapshot,
                            file_id=file_id,
                            requested_outcome=requested_outcome,
                            sync_mode=sync_mode,
                            conversation_id=conversation_id,
                            source_id=source_id,
                            trace_id=trace_id,
                            storage=storage,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
            await cache_preview(
                tenant.tenant_id, command.values[6], command.resource_id
            )
            return result
