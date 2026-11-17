"""A watermark probe can reuse a prior full proof; it never grants a body."""

from hashlib import sha256

from sqlalchemy import and_, func, literal, select

from app.core.authorization_plan import LockPlan
from app.core.config import settings
from app.core.field_policy.contracts import canonical
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.delivery_source_domains import (
    authority_header,
    validate_receipt,
)
from app.modules.job.models.job import SysJob
from app.modules.job.task_registry import tenant_task_keys
from app.modules.system.field_policy.delivery_catalog import delivery_catalog
from app.modules.system.field_policy.file_sources import (
    DELIVERY_CLASSES,
    make_file_manifest,
)
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.file import File
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch


async def delivery_probe(
    db,
    tx,
    *,
    tenant,
    actor_user_version,
    header,
    conversation_id=None,
    source_id=None,
    trace_id=None,
    receipt=None,
):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    await tx.lock(LockPlan())
    if validate_receipt(receipt) is None or authority_header(receipt) != header:
        return False
    generic = header.get("schema") == 2
    if (
        header.get("tenant_id") != str(tenant.tenant_id)
        or header.get("actor_id") != str(tenant.actor_user_id)
        or header.get("build") != (settings.RELEASE_BUILD_SHA or "development")
    ):
        return False
    if generic and header.get("catalog_digest") != delivery_catalog().digest:
        return False
    users, tenants = User.__table__, Tenant.__table__
    guard, state, head = (
        UserFieldPolicyGlobalGuard.__table__,
        UserFieldPolicyState.__table__,
        FieldPolicyHead.__table__,
    )
    clauses = [
        select(users.c.user_id)
        .where(
            users.c.tenant_id == tenant.tenant_id,
            users.c.user_id == tenant.actor_user_id,
            users.c.auth_version == actor_user_version,
            users.c.status == "1",
        )
        .exists(),
        select(tenants.c.tenant_id)
        .where(
            tenants.c.tenant_id == tenant.tenant_id,
            tenants.c.row_version == tenant.tenant_version,
            tenants.c.lifecycle_state == "active",
            tenants.c.status == "1",
        )
        .exists(),
        select(guard.c.guard_id)
        .where(guard.c.guard_id == 1, guard.c.epoch == header.get("global_epoch"))
        .exists(),
        select(state.c.tenant_id)
        .where(
            state.c.tenant_id == tenant.tenant_id,
            state.c.epoch == header.get("tenant_epoch"),
        )
        .exists(),
    ]
    if generic:
        clauses.append(
            select(head.c.tenant_id)
            .where(
                head.c.tenant_id == tenant.tenant_id,
                head.c.active_manifest_digest == header.get("active_digest"),
            )
            .exists()
        )
    else:
        clauses.append(
            ~select(head.c.tenant_id)
            .where(head.c.tenant_id == tenant.tenant_id)
            .exists()
        )
    if conversation_id is not None:
        conversation, source = AiConversation.__table__, AiMessage.__table__
        clauses.extend(
            [
                select(conversation.c.conversation_id)
                .where(
                    conversation.c.tenant_id == tenant.tenant_id,
                    conversation.c.conversation_id == conversation_id,
                    conversation.c.user_id == tenant.actor_user_id,
                    conversation.c.deleted_at.is_(None),
                )
                .exists(),
                select(source.c.message_id)
                .where(
                    source.c.tenant_id == tenant.tenant_id,
                    source.c.message_id == source_id,
                    source.c.conversation_id == conversation_id,
                    source.c.trace_id == trace_id,
                    source.c.role == "user",
                    source.c.is_active.is_(True),
                )
                .exists(),
            ]
        )
    # G/B does not version factual retirement or message invalidation. Check
    # those live structural dependencies in this same bounded SELECT too.
    message_ids, job_ids = set(), set()
    for group in receipt["source_domains"]:
        if group.get("object_id") == "ai.message":
            message_ids.update(int(value) for value in group["records"])
        elif group.get("object_id") == "job.definition":
            job_ids.update(int(value) for value in group["records"])
        elif group.get("kind") == "file":
            files = File.__table__
            manifest = None
            for delivery_class in DELIVERY_CLASSES:
                candidate = make_file_manifest(
                    tenant_id=tenant.tenant_id,
                    owner_id=tenant.actor_user_id,
                    file_id=int(group["file_id"]),
                    delivery_class=delivery_class,
                    digest=group["sha256"],
                )
                if (
                    sha256(canonical(candidate, 4 * 1024 * 1024)).hexdigest()
                    == group["manifest_digest"]
                ):
                    manifest = candidate
                    break
            if manifest is None:
                return False
            clauses.append(
                select(files.c.file_id)
                .where(
                    files.c.tenant_id == tenant.tenant_id,
                    files.c.file_id == int(group["file_id"]),
                    files.c.del_flag == "0",
                    files.c.owner_user_id == tenant.actor_user_id,
                    files.c.content_sha256 == group["sha256"],
                    files.c.delivery_class == manifest["delivery_class"],
                    files.c.source_manifest == manifest,
                )
                .exists()
            )
        elif group.get("kind") == "artifact":
            model, anchor = (
                (UserExportTask, "export_id")
                if group["resource_kind"] == "export"
                else (UserImportBatch, "batch_id")
            )
            clauses.append(
                select(getattr(model, anchor))
                .where(
                    model.tenant_id == tenant.tenant_id,
                    getattr(model, anchor) == group["resource_id"],
                    model.operator_id == tenant.actor_user_id,
                    model.field_lineage[group["source_kind"]].is_not(None),
                )
                .exists()
            )
    if message_ids:
        messages, parent = AiMessage.__table__, AiConversation.__table__
        owned = (
            select(parent.c.conversation_id)
            .where(
                parent.c.tenant_id == tenant.tenant_id,
                parent.c.conversation_id == messages.c.conversation_id,
                parent.c.user_id == tenant.actor_user_id,
                parent.c.deleted_at.is_(None),
            )
            .correlate(messages)
            .exists()
        )
        clauses.append(
            select(func.pg_catalog.count(literal(1)))
            .select_from(messages)
            .where(
                messages.c.tenant_id == tenant.tenant_id,
                messages.c.message_id.in_(message_ids),
                messages.c.is_active.is_(True),
                owned,
            )
            .scalar_subquery()
            == len(message_ids)
        )
    if job_ids:
        clauses.append(
            select(func.pg_catalog.count(literal(1)))
            .select_from(SysJob)
            .where(
                SysJob.tenant_id == tenant.tenant_id,
                SysJob.job_id.in_(job_ids),
                SysJob.job_key.in_(tenant_task_keys()),
            )
            .scalar_subquery()
            == len(job_ids)
        )
    return bool(await db.scalar(select(literal(True)).where(and_(*clauses))))
