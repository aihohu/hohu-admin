"""Explicit owners for transfer candidates; no publication retry after ACK loss."""

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import or_, select, tuple_

from app.core import redis as redis_module
from app.core.authorization_file import FileBudget
from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import (
    LockMode,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessRuleException, NotFoundException
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.db.authorization_transfer_cache import cache_preview, retire_preview_hint
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.authorization_transfer_candidate import (
    transfer_candidate,
)
from app.modules.system.service.authorization_transfer_cleanup_candidate import (
    transfer_cleanup_candidate,
)

transfer_parser = file_transform_worker
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupPage:
    items: tuple
    next_cursor: tuple | None


class TransferOwner:
    def __init__(self, factory, storage):
        self.factory, self.storage = factory, storage

    async def cancel(self, *, tenant, actor_user_version, identifier, reason):
        async with self.factory() as db:
            try:
                with (
                    bounded_authorization_reads(db),
                    FileBudget(
                        max_reads=2,
                        max_total_bytes=20 * 1024 * 1024,
                        deadline=time.monotonic() + 5,
                    ).activate(),
                ):
                    async with asyncio.timeout(5):
                        spec = resource_spec(ResourceKind.IMPORT_BATCH)
                        present = await db.scalar(
                            select(spec.table.c.batch_id).where(
                                spec.table.c.tenant_id == tenant.tenant_id,
                                spec.table.c.batch_id == identifier,
                            )
                        )
                        if present is None:
                            raise NotFoundException(
                                "用户导入批次", error_code="AI_IMPORT_BATCH_NOT_FOUND"
                            )
                        try:
                            command = await transfer_cleanup_candidate.prepare(
                                db,
                                tenant_id=tenant.tenant_id,
                                kind="import",
                                identifier=identifier,
                                storage=self.storage,
                            )
                        except (BusinessRuleException, FileNotFoundError) as exc:
                            raise BusinessRuleException(
                                "批次关联操作或文件归属待核对，暂不能取消",
                                error_code="AI_EXECUTION_OUTCOME_UNKNOWN",
                            ) from exc
            finally:
                await rollback_authorization_session(db)
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await transfer_cleanup_candidate.cancel(
                            db,
                            tx,
                            command,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            reason=reason,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
        if result.status == "RUNNING":
            async with asyncio.timeout(2):
                await redis_module.redis_client.setex(
                    f"user_import:cancel:{identifier}", 3600, "1"
                )
        else:
            try:
                await self._cleanup_phase(
                    tenant_id=tenant.tenant_id,
                    kind="import",
                    identifier=identifier,
                    retire=True,
                )
            except Exception:
                logger.warning("Committed cancellation retained its private artifact")
        return result

    async def export(self, *, tenant, actor_user_version, args):
        command = transfer_candidate.prepare_export(
            tenant=tenant, actor_user_version=actor_user_version, args=args
        )
        return await self._publish(command)

    async def preview(self, *, tenant, actor_user_version, **request):
        command = await transfer_parser.run(
            transfer_candidate.prepare_preview,
            tenant=tenant,
            actor_user_version=actor_user_version,
            **request,
        )
        return await self._publish(command)

    async def _publish(self, command):
        tenant = command.tenant
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        work = await transfer_candidate.plan(db, tx, command)
                        await tx.lock(work.locks)
                        result = await transfer_candidate.apply(
                            db, tx, work, storage=self.storage
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
            if command.kind == "preview":
                await cache_preview(
                    tenant.tenant_id, result.preview_token, result.resource_id
                )
            return result

    async def download(
        self,
        *,
        tenant,
        actor_user_version,
        kind,
        identifier,
        token=None,
        administrative=False,
        signed=False,
    ):
        if (
            type(administrative) is not bool
            or type(signed) is not bool
            or ((administrative or signed) and kind != "export")
            or (administrative and signed)
        ):
            raise _order_error()
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        execute = (
                            transfer_candidate.rest_download
                            if administrative or signed
                            else transfer_candidate.download
                        )
                        extra = (
                            {"signed": True, "token": token}
                            if signed
                            else {}
                            if administrative
                            else {"kind": kind, "token": token}
                        )
                        result = await execute(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            identifier=identifier,
                            storage=self.storage,
                            **extra,
                        )
            finally:
                await rollback_authorization_session(db)
        return result

    async def _cleanup_phase(self, *, tenant_id, kind, identifier, retire):
        async with self.factory() as db:
            try:
                with (
                    bounded_authorization_reads(db),
                    FileBudget(
                        max_reads=2,
                        max_total_bytes=20 * 1024 * 1024,
                        deadline=time.monotonic() + 5,
                    ).activate(),
                ):
                    async with asyncio.timeout(5):
                        command = await transfer_cleanup_candidate.prepare(
                            db,
                            tenant_id=tenant_id,
                            kind=kind,
                            identifier=identifier,
                            storage=self.storage,
                            inspect_artifacts=retire,
                        )
            finally:
                await rollback_authorization_session(db)
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(
                                tenant_id,
                                LockMode.EXCLUSIVE
                                if kind == "import" and not retire
                                else LockMode.SHARED,
                            ),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        result = await transfer_cleanup_candidate.execute(
                            db, tx, command, storage=self.storage, retire=retire
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
            if result == "retired" and kind == "import":
                await retire_preview_hint(tenant_id, command.snapshot["preview_token"])
            return result

    async def cleanup_page(self, *, kind, after=None, limit=256, category=None):
        if (
            kind not in {"export", "import"}
            or category not in {None, "preview", "terminal"}
            or (kind == "export" and category == "preview")
            or type(limit) is not int
            or not 1 <= limit <= 256
            or (
                after is not None
                and (
                    type(after) is not tuple
                    or len(after) != 2
                    or type(after[0]) is not int
                    or after[0] < 0
                    or type(after[1]) is not str
                    or not 1 <= len(after[1]) <= 64
                )
            )
        ):
            raise _order_error()
        spec = resource_spec(
            ResourceKind.EXPORT_TASK if kind == "export" else ResourceKind.IMPORT_BATCH
        )
        table = spec.table
        identifier = table.c[spec.key_columns[0]]
        query = (
            select(table.c.tenant_id, identifier)
            .where(
                table.c.status.in_(
                    ("SUCCESS", "FAILED", "EXPIRED")
                    if kind == "export"
                    else ("PREVIEW_DONE", "EXPIRED")
                    if category == "preview"
                    else (
                        "SUCCESS",
                        "PARTIAL_SUCCESS",
                        "FAILED",
                        "CANCELLED",
                        "EXPIRED",
                    )
                    if category == "terminal"
                    else (
                        "PREVIEW_DONE",
                        "SUCCESS",
                        "PARTIAL_SUCCESS",
                        "FAILED",
                        "CANCELLED",
                        "EXPIRED",
                    )
                ),
                or_(
                    table.c.file_storage_key.is_not(None),
                    table.c.failed_rows_file.is_not(None),
                )
                if kind == "import"
                else table.c.file_storage_key.is_not(None),
            )
            .order_by(table.c.tenant_id, identifier)
            .limit(limit)
        )
        if after is not None:
            query = query.where(tuple_(table.c.tenant_id, identifier) > after)
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with asyncio.timeout(5):
                        rows = (await db.execute(query)).all()
            finally:
                await rollback_authorization_session(db)
        items = []
        deadline = time.monotonic() + 30
        for tid, rid in rows:
            if items and time.monotonic() >= deadline:
                break
            try:
                status = await self._cleanup_phase(
                    tenant_id=tid, kind=kind, identifier=rid, retire=False
                )
                if status == "expired":
                    status = await self._cleanup_phase(
                        tenant_id=tid, kind=kind, identifier=rid, retire=True
                    )
            except Exception:
                status = "retained"
            items.append((tid, rid, status))
        return CleanupPage(tuple(items), items[-1][:2] if items else None)
