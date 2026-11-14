"""Independent read scopes surround bounded file IO and every byte delivery."""

from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import TenantLock, TransactionBudget
from app.core.config import settings
from app.db.authorization_owner import (
    authorization_session,
    rollback_authorization_session,
)
from app.modules.system.field_policy.file_sources import read_file_content
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.file_read_service import file_read_service


class FileReadOwner:
    def __init__(self, factory):
        self.factory = factory

    async def _run(self, operation, **request):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(
                    db,
                    AuthorizationReadBudget(
                        max_statements=512, max_rows=10000, max_bytes=16 * 1024 * 1024
                    ),
                ):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(request["tenant"].tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        return await getattr(file_read_service, operation)(
                            db, tx, **request
                        )
            finally:
                await rollback_authorization_session(db)

    async def metadata(self, **request):
        return await self._run("metadata", **request)

    async def download(self, **request):
        snapshot = await self._run("download", **request)
        content = await file_transform_worker.run(
            read_file_content,
            snapshot,
            str(settings.UPLOAD_DIR),
            str(settings.PRIVATE_UPLOAD_DIR),
        )
        await self._run("download", expected=snapshot, **request)
        return content
