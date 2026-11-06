"""One bounded transaction owns read execution and its durable confirmation."""

from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.config import settings
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.service.authorization_file_parse import parse_uploaded_file
from app.modules.ai.service.authorization_read_confirmation import (
    FileReadPreparation,
    read_confirmation_candidate,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class ReadConfirmationOwner:
    def __init__(self, factory):
        self.factory = factory

    async def confirm(self, confirmation_id, **request):
        if "file_preparation" in request or "file_result" in request:
            raise _order_error()
        if request.get("decision") == "approve":
            preparation = await self._run(
                confirmation_id, project=False, **{**request, "decision": "prepare"}
            )
            if type(preparation) is FileReadPreparation:
                result = await file_transform_worker.run(
                    parse_uploaded_file,
                    preparation.snapshot,
                    str(settings.UPLOAD_DIR),
                    str(settings.PRIVATE_UPLOAD_DIR),
                )
                request = {
                    **request,
                    "file_preparation": preparation,
                    "file_result": result,
                }
        return await self._run(confirmation_id, project=False, **request)

    async def project(self, confirmation_id, **request):
        return await self._run(confirmation_id, project=True, **request)

    async def _run(self, confirmation_id, *, project, **request):
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
                        if project:
                            result = await read_confirmation_candidate.project(
                                db, tx, confirmation_id, **request
                            )
                        else:
                            result = await read_confirmation_candidate.execute(
                                db, tx, confirmation_id, **request
                            )
                            if (
                                result is not None
                                and type(result) is not FileReadPreparation
                            ):
                                db.expunge(result)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if project or request.get("decision") in {"read", "prepare"}:
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
