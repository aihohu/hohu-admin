"""Owned transactions for chat facts. No LLM/network call runs under G/B."""

import asyncio

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.schemas.conversation import ConversationCreate, ConversationUpdate
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.authorization_chat_model import select_model
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class ChatOwner:
    def __init__(self, factory):
        self.factory = factory

    async def conversation(
        self, *, tenant, actor_user_version, data, conversation_id=None
    ):
        if type(data) is not dict or set(data) - {
            "title",
            "model_name",
            "system_prompt",
            "status",
        }:
            raise _order_error()
        schema = (
            ConversationCreate if conversation_id is None else ConversationUpdate
        ).model_validate(data)
        values = schema.model_dump(exclude_unset=conversation_id is not None)
        if (
            values.get("title", "") is None
            or values.get("status", 0) not in {0, 1}
            or len(str(values.get("system_prompt") or "").encode()) > 256 * 1024
        ):
            raise _order_error()
        selection = None
        if conversation_id is None or "model_name" in values:
            async with self.factory() as db:
                try:
                    with bounded_authorization_reads(db):
                        async with asyncio.timeout(5):
                            selection = await select_model(
                                db, tenant, values.get("model_name")
                            )
                finally:
                    await rollback_authorization_session(db)
            async with asyncio.timeout(5):
                egress = dict(selection.egress)
                if not await provider_egress.is_model_allowed(
                    egress.pop("provider_code"), egress.pop("base_url"), **egress
                ):
                    raise _order_error()
        return await self._run(
            "conversation",
            {
                "tenant": tenant,
                "actor_user_version": actor_user_version,
                "data": values,
                "selection": selection,
                "conversation_id": conversation_id,
            },
        )

    async def source(self, **request):
        return await self._run("source", request)

    async def finalize(self, **request):
        return await self._run("finalize", request)

    async def recover(self, *, tenant, confirmation_id):
        return await self._run(
            "recover", {"tenant": tenant, "confirmation_id": confirmation_id}
        )

    async def messages(self, **request):
        return await self._run("messages", request)

    async def recover_orphan(self, *, tenant, confirmation_id):
        return await self._run(
            "recover_orphan", {"tenant": tenant, "confirmation_id": confirmation_id}
        )

    async def invalidate(self, **request):
        return await self._run("invalidate", request)

    async def _run(self, operation, request):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(
                                request["tenant"].tenant_id,
                                LockMode.EXCLUSIVE
                                if operation == "conversation"
                                else LockMode.SHARED,
                            ),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        if operation == "conversation":
                            result = await chat_candidate.conversation(
                                db, tx, **request
                            )
                        elif operation == "source":
                            result = await chat_candidate.source(db, tx, **request)
                        elif operation == "finalize":
                            result = await chat_candidate.finalize(db, tx, **request)
                        elif operation == "recover":
                            result = await chat_candidate.recover(db, tx, **request)
                        elif operation == "recover_orphan":
                            result = await chat_candidate.recover_orphan(
                                db, tx, **request
                            )
                        elif operation == "invalidate":
                            result = await chat_candidate.invalidate(db, tx, **request)
                        else:
                            result = await chat_candidate.messages(db, tx, **request)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if operation == "messages":
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
