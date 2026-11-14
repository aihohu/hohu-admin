"""Discover a complete context set before any context row lock is acquired."""

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.delivery_external_sources import digest
from app.modules.ai.service.delivery_file_parts import part_keys
from app.modules.ai.service.delivery_source_domains import validate_receipt
from app.modules.system.models.file import File
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch

_TABLES = (
    (K.CONVERSATION, AiConversation, "conversation_id"),
    (K.SOURCE_MESSAGE, AiMessage, "message_id"),
    (K.PREPARED_ACTION, AiPreparedAction, "action_id"),
    (K.OPERATION_LOG, AiOperationLog, "log_id"),
)


def _stale():
    return BusinessException(
        409, "AI 事务依赖已变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
    )


@dataclass(frozen=True, slots=True)
class ContextInventory:
    tenant_id: int
    conversations: tuple[int, ...]
    keys: tuple[ResourceKey, ...]
    bindings: tuple[tuple, ...]


class AIContextCandidate:
    async def discover(self, db, tx, *, tenant_id, conversation_ids):
        tx.assert_session(db)
        if type(conversation_ids) is not tuple or any(
            type(n) is not int or n <= 0 for n in conversation_ids
        ):
            raise _order_error()
        ids = tuple(sorted(set(conversation_ids)))
        if len(ids) > 16384:
            raise BusinessException(
                422,
                "AI context 超出预算",
                error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
            )
        keys, bindings, related = [], [], set()
        for kind, model, key_name in _TABLES:
            # Explicit paging bounds SQL parameters; the overall set is never truncated.
            for offset in range(0, len(ids), 256):
                rows = (
                    (
                        await db.execute(
                            select(model.__table__)
                            .where(
                                model.tenant_id == tenant_id,
                                model.conversation_id.in_(ids[offset : offset + 256]),
                            )
                            .order_by(getattr(model, key_name))
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    for name in ("field_authority", "result_field_authority"):
                        proof = row.get(name)
                        if validate_receipt(proof) is not None:
                            bindings.append(
                                (
                                    "source-digest",
                                    model.__tablename__,
                                    row[key_name],
                                    name,
                                    proof["digest"],
                                )
                            )
                            for group in proof["source_domains"]:
                                if group.get("kind") == "file":
                                    related.add((K.FILE, int(group["file_id"])))
                                elif group.get("kind") == "artifact":
                                    related.add(
                                        (
                                            K.EXPORT_TASK
                                            if group["resource_kind"] == "export"
                                            else K.IMPORT_BATCH,
                                            group["resource_id"],
                                        )
                                    )
                    if model is AiMessage and row.get("parts"):
                        bindings.append(
                            ("parts-digest", row[key_name], digest(row["parts"]))
                        )
                        try:
                            related.update(
                                (K.FILE, key.identity[0])
                                for key in part_keys(
                                    row["parts"], SimpleNamespace(tenant_id=tenant_id)
                                )
                            )
                        except BusinessException:
                            pass  # Unknown historical parts are never delivered.
                    actual_kind = (
                        K.ASSISTANT_MESSAGE
                        if model is AiMessage and row["role"] != "user"
                        else kind
                    )
                    keys.append(ResourceKey(actual_kind, tenant_id, row[key_name]))
                    # Only immutable routing bindings determine the lock set. Live
                    # state is freshly validated after locks by the phase service.
                    fields = (
                        "tenant_id",
                        key_name,
                        "conversation_id",
                        "source_user_message_id",
                        "user_id",
                        "role",
                        "trace_id",
                        "execute_tool_call_id",
                        "tool_call_id",
                    )
                    bindings.append(
                        (actual_kind.value, *(row.get(name) for name in fields))
                    )
                    if len(keys) > 16384:
                        raise BusinessException(
                            422,
                            "AI context 超出预算",
                            error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
                        )
        for kind, model, anchor in (
            (K.FILE, File, "file_id"),
            (K.IMPORT_BATCH, UserImportBatch, "batch_id"),
            (K.EXPORT_TASK, UserExportTask, "export_id"),
        ):
            values = sorted(v for k, v in related if k == kind)
            for start in range(0, len(values), 256):
                actual = await db.scalars(
                    select(getattr(model, anchor))
                    .where(
                        model.tenant_id == tenant_id,
                        getattr(model, anchor).in_(values[start : start + 256]),
                    )
                    .order_by(getattr(model, anchor))
                )
                keys.extend(ResourceKey(kind, tenant_id, value) for value in actual)
        if len(keys) > 16384:
            raise _order_error()
        return ContextInventory(tenant_id, ids, tuple(keys), tuple(bindings))

    async def recheck(self, db, tx, inventory):
        tx.assert_session(db)
        tx.assert_held(*inventory.keys)
        current = await self.discover(
            db,
            tx,
            tenant_id=inventory.tenant_id,
            conversation_ids=inventory.conversations,
        )
        if current != inventory:
            raise _stale()


ai_context_candidate = AIContextCandidate()
