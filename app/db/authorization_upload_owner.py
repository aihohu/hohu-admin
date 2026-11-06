"""Finite upload IO between independent identity/policy and FILE transactions."""

import asyncio
import logging

from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.config import settings
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.modules.system.service.authorization_chat_image_candidate import (
    chat_image_candidate,
    read_image,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.authorization_upload_candidate import (
    UploadBytes,
    limit,
    publish_uploads,
    upload_candidate,
)
from app.modules.system.service.file_service import file_service

logger = logging.getLogger(__name__)


def retire_uploads(paths):
    for path in paths:
        file_service._delete_disk_file(path)


class UploadOwner:
    def __init__(self, factory):
        self.factory = factory

    async def _run(self, operation, **request):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(
                                request["tenant"].tenant_id,
                                LockMode.EXCLUSIVE
                                if operation in {"delete", "insert"}
                                else LockMode.SHARED,
                            ),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        candidate = (
                            chat_image_candidate
                            if operation == "image"
                            else upload_candidate
                        )
                        result = await getattr(candidate, operation)(db, tx, **request)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if operation in {"policies", "image"}:
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result

    async def read_chat_image(self, *, tenant, actor_user_version, file_url):
        request = {
            "tenant": tenant,
            "actor_user_version": actor_user_version,
            "file_url": file_url,
        }
        snapshot = await self._run("image", **request)
        result = await file_transform_worker.run(
            read_image,
            snapshot,
            str(settings.UPLOAD_DIR),
            str(settings.PRIVATE_UPLOAD_DIR),
        )
        await self._run("image", expected=snapshot, **request)
        return result

    async def upload(
        self, *, tenant, actor_user_version, files, business_type=None, business_id=None
    ):
        files = tuple(files)
        if not 1 <= len(files) <= 32 or (
            business_type is not None
            and (not isinstance(business_type, str) or len(business_type) > 50)
        ):
            raise limit()
        names = tuple((item.filename, business_type) for item in files)
        if any(not name or len(name) > 255 for name, _ in names):
            raise limit()
        policies = await self._run(
            "policies",
            tenant=tenant,
            actor_user_version=actor_user_version,
            names=names,
        )
        sources = []
        total = 0
        async with asyncio.timeout(5):
            for item, policy in zip(files, policies, strict=True):
                policy.validate_extension(item.filename)
                content = await item.read(
                    min(policy.max_bytes, 100 * 1024 * 1024 - total) + 1
                )
                policy.validate_size(len(content))
                total += len(content)
                if total > 100 * 1024 * 1024 or (
                    item.content_type and len(item.content_type) > 100
                ):
                    raise limit()
                sources.append(
                    UploadBytes(
                        item.filename,
                        item.content_type,
                        content,
                        business_type,
                        business_id,
                    )
                )
        uploads = await file_transform_worker.run(
            publish_uploads,
            tuple(sources),
            tenant.tenant_id,
            str(settings.UPLOAD_DIR),
            str(settings.PRIVATE_UPLOAD_DIR),
        )
        # No cleanup on exception: a lost database ACK can hide a successful insert.
        return await self._run(
            "insert",
            tenant=tenant,
            actor_user_version=actor_user_version,
            uploads=uploads,
        )

    async def delete(self, *, tenant, actor_user_version, identifiers, batch=False):
        identifiers = tuple(sorted(set(identifiers)))
        if (
            type(batch) is not bool
            or len(identifiers) > 32
            or any(
                type(item) is not int or not 0 < item < 2**63 for item in identifiers
            )
        ):
            raise limit()
        if not identifiers:
            return 0
        paths = await self._run(
            "delete",
            tenant=tenant,
            actor_user_version=actor_user_version,
            identifiers=identifiers,
            batch=batch,
        )
        try:
            await file_transform_worker.run(retire_uploads, paths)
        except Exception:
            # Metadata is gone with a known ACK. Unreachable disk remnants are
            # retained for operator cleanup; an IO timeout never replays deletion.
            logger.warning("Committed file retirement left disk cleanup pending")
        return len(paths)
