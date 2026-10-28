"""Upload preparation is lock-free; preview publication owns one complete plan."""

from uuid import uuid4

from app.core import file_storage
from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.config import settings
from app.core.file_storage import ArtifactExecution
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.db.authorization_transfer_cache import cache_preview
from app.modules.ai.service.atomic_completion_service import new_execution_owner
from app.modules.ai.service.authorization_preview_candidate import (
    GatewayPreviewBinding,
    preview_candidate,
    read_preview_input,
)
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

    async def _read(self, operation, **request):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(request["tenant"].tenant_id, LockMode.SHARED),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        candidate = {
                            "receipt": preview_candidate.receipt,
                            "input": preview_candidate.input_file,
                            "admit": preview_candidate.admit_gateway,
                        }[operation]
                        return await candidate(db, tx, **request)
            finally:
                await rollback_authorization_session(db)

    async def receipt(self, **request):
        return await self._read("receipt", **request)

    async def gateway_preview(self, *, gateway, **request):
        if type(gateway) is not GatewayPreviewBinding:
            raise _order_error()
        args = gateway.arguments()
        if args is None:
            raise _order_error()
        await self._read("admit", gateway=gateway, **request)
        return await self.preview(
            **request,
            **args,
            requested_outcome="preview_only",
            gateway=gateway,
        )

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
        gateway=None,
    ):
        storage = file_storage.get_file_storage()
        request = {
            "tenant": tenant,
            "actor_user_version": actor_user_version,
            "file_id": file_id,
        }
        snapshot = await self._read("input", **request)
        data, filename = await file_transform_worker.run(
            read_preview_input, snapshot, str(settings.PRIVATE_UPLOAD_DIR)
        )
        await self._read("input", expected=snapshot, **request)
        command = await file_transform_worker.run(
            transfer_candidate.prepare_preview,
            tenant=tenant,
            actor_user_version=actor_user_version,
            file_bytes=data,
            filename=filename,
            reason=reason,
            on_conflict=on_conflict,
            execution=ArtifactExecution(
                tenant.tenant_id,
                gateway.tool_call_id if gateway else "tc_" + uuid4().hex,
                f"preview:{gateway.log_id}"
                if gateway
                else new_execution_owner("user.import_preview"),
            ),
        )
        async with authorization_session(self.factory) as db:
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
                            file_snapshot=dict(snapshot.facts),
                            file_policy=snapshot.policy,
                            file_id=file_id,
                            requested_outcome=requested_outcome,
                            sync_mode=sync_mode,
                            conversation_id=conversation_id,
                            source_id=source_id,
                            trace_id=trace_id,
                            storage=storage,
                            gateway=gateway,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
            await cache_preview(
                tenant.tenant_id, command.values[6], command.resource_id
            )
            return result
