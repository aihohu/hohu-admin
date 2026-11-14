"""Owned transactions for chat facts. No LLM/network call runs under G/B."""

import asyncio

from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.config import settings
from app.core.exceptions import BusinessException, BusinessRuleException
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.schemas.conversation import (
    ConversationCreate,
    ConversationUpdate,
)
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.authorization_chat_ingress_candidate import (
    chat_ingress_candidate,
)
from app.modules.ai.service.authorization_chat_model import select_model
from app.modules.ai.service.authorization_feedback_candidate import feedback_candidate
from app.modules.ai.service.authorization_file_parse import (
    file_parse_failure,
    parse_uploaded_file,
)
from app.modules.ai.service.authorization_gateway_audit_candidate import (
    gateway_audit_candidate,
)
from app.modules.ai.service.authorization_gateway_read_candidate import (
    gateway_read_candidate,
)
from app.modules.ai.service.conversation_delivery import project_conversation
from app.modules.ai.service.delivery_probe import delivery_probe
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class ChatOwner:
    def __init__(self, factory):
        self.factory = factory

    async def conversation(
        self,
        *,
        tenant,
        actor_user_version,
        data,
        conversation_id=None,
        http_result=False,
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
        if conversation_id is None:
            values["title"] = values.get("title") or "新对话"
        if type(http_result) is not bool:
            raise _order_error()
        if (
            values.get("title", "") is None
            or values.get("status", 0) not in {0, 1}
            or len(str(values.get("system_prompt") or "").encode()) > 256 * 1024
        ):
            raise _order_error()
        selection = None
        if conversation_id is None or "model_name" in values:
            async with authorization_session(self.factory) as db:
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
                    raise BusinessRuleException(
                        "所选 AI 模型当前不可用", error_code="AI_MODEL_NOT_AVAILABLE"
                    )
        return await self._run(
            "conversation",
            {
                "tenant": tenant,
                "actor_user_version": actor_user_version,
                "data": values,
                "selection": selection,
                "conversation_id": conversation_id,
            },
            http_result=http_result,
        )

    async def source(self, **request):
        return await self._run("source", request)

    async def input_parts(self, **request):
        return await self._run("input_parts", request)

    async def routing_context(self, **request):
        return await self._run("routing_context", request)

    async def delivery_check(self, **request):
        return await self._run("delivery_check", request)

    async def delivery_probe(self, **request):
        return await self._run("delivery_probe", request)

    async def start_operation(self, **request):
        return await self._run("start_operation", request)

    async def finish_operation(self, **request):
        return await self._run("finish_operation", request)

    async def read_tool(self, **request):
        snapshot = await self._run("read_tool", request)
        if request.get("tool") != "file.parse":
            return snapshot
        error = None
        try:
            result = await file_transform_worker.run(
                parse_uploaded_file,
                snapshot,
                str(settings.UPLOAD_DIR),
                str(settings.PRIVATE_UPLOAD_DIR),
            )
        except BusinessException as exc:
            error = exc
        if await self._run("read_tool", request) != snapshot:
            raise _order_error()
        if error is not None:
            return file_parse_failure(snapshot, error)
        return result

    async def preflight(self, **request):
        return await self._run("preflight", request)

    async def start_run(self, **request):
        return await self._run("start_run", request)

    async def check_run(self, **request):
        # Only this read can restart after discovery races with a new tool log.
        # Each attempt exits _run (including rollback) before rebuilding its plan.
        async with asyncio.timeout(8):
            for attempt in range(3):
                try:
                    return await self._run("check_run", request)
                except BusinessException as exc:
                    if exc.error_code != "AUTHORIZATION_SNAPSHOT_STALE" or attempt == 2:
                        raise

    async def query_cache(self, **request):
        return await self._run("query_cache", request)

    async def model_authority(self, **request):
        return await self._run("model_authority", request)

    async def action_projection(self, **request):
        try:
            return await self._run("action_projection", request)
        except BusinessException as exc:
            if 400 <= exc.code < 500:
                return False
            raise

    async def routing_audit(self, **request):
        return await self._run("routing_audit", request)

    async def routing_feedback(self, **request):
        return await self._run("routing_feedback", request)

    async def select_model(self, **request):
        choices = await self._run("model_candidates", request)
        semaphore = asyncio.Semaphore(10)

        async def allowed(choice):
            async with semaphore:
                egress = dict(choice.selection.egress)
                return await provider_egress.is_model_allowed(
                    egress.pop("provider_code"),
                    egress.pop("base_url"),
                    **egress,
                )

        async with asyncio.timeout(5):
            checks = await asyncio.gather(*(allowed(choice) for choice in choices))
        for choice, permitted in zip(choices, checks, strict=True):
            if permitted:
                return choice
        raise BusinessRuleException(
            "所选 AI 模型当前不可用", error_code="AI_MODEL_NOT_AVAILABLE"
        )

    async def finalize(self, **request):
        return await self._run("finalize", request)

    async def recover(self, *, tenant, confirmation_id):
        return await self._run(
            "recover", {"tenant": tenant, "confirmation_id": confirmation_id}
        )

    async def messages(self, **request):
        return await self._run("messages", request)

    async def list_conversations(self, **request):
        return await self._run("list_conversations", request)

    async def recover_orphan(self, *, tenant, confirmation_id):
        return await self._run(
            "recover_orphan", {"tenant": tenant, "confirmation_id": confirmation_id}
        )

    async def invalidate(self, **request):
        return await self._run("invalidate", request)

    async def _run(self, operation, request, *, http_result=False):
        async with authorization_session(self.factory) as db:
            try:
                read_budget = AuthorizationReadBudget(
                    max_statements=16 if operation == "delivery_probe" else 4096,
                    max_rows=1024 if operation == "delivery_probe" else 100000,
                    max_bytes=2 * 1024 * 1024
                    if operation == "delivery_probe"
                    else 128 * 1024 * 1024,
                )
                with bounded_authorization_reads(db, read_budget):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(
                                request["tenant"].tenant_id,
                                LockMode.EXCLUSIVE
                                if operation
                                in {
                                    "conversation",
                                    "start_run",
                                    "routing_audit",
                                    "routing_feedback",
                                }
                                else LockMode.SHARED,
                            ),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        if operation == "conversation":
                            result = await chat_candidate.conversation(
                                db, tx, **request
                            )
                            if http_result:
                                result = await project_conversation(
                                    db,
                                    tx,
                                    tenant=request["tenant"],
                                    version=request["actor_user_version"],
                                    identifier=result,
                                )
                        elif operation == "model_candidates":
                            result = await chat_ingress_candidate.model_candidates(
                                db, tx, **request
                            )
                        elif operation == "start_run":
                            result = await chat_ingress_candidate.start_run(
                                db, tx, **request
                            )
                        elif operation == "routing_audit":
                            result = await chat_ingress_candidate.routing_audit(
                                db, tx, **request
                            )
                        elif operation == "routing_feedback":
                            result = await feedback_candidate.submit(db, tx, **request)
                        elif operation == "start_operation":
                            result = await gateway_audit_candidate.start_operation(
                                db, tx, **request
                            )
                        elif operation == "finish_operation":
                            result = await gateway_audit_candidate.finish_operation(
                                db, tx, **request
                            )
                        elif operation == "read_tool":
                            result = await gateway_read_candidate.read_tool(
                                db, tx, **request
                            )
                        elif operation == "preflight":
                            result = await gateway_read_candidate.preflight(
                                db, tx, **request
                            )
                        elif operation == "check_run":
                            result = await chat_candidate.check_run(db, tx, **request)
                        elif operation == "query_cache":
                            result = await chat_candidate.query_cache(db, tx, **request)
                        elif operation == "model_authority":
                            result = await chat_candidate.model_authority(
                                db, tx, **request
                            )
                        elif operation == "action_projection":
                            result = await chat_candidate.action_projection(
                                db, tx, **request
                            )
                        elif operation == "source":
                            result = await chat_candidate.source(db, tx, **request)
                        elif operation == "input_parts":
                            result = await chat_candidate.input_parts(db, tx, **request)
                        elif operation == "routing_context":
                            result = await chat_candidate.routing_context(
                                db, tx, **request
                            )
                        elif operation == "delivery_probe":
                            result = await delivery_probe(db, tx, **request)
                        elif operation == "delivery_check":
                            result = await (
                                chat_candidate.check_run(db, tx, **request)
                                if request.get("source_id")
                                else chat_candidate.model_authority(db, tx, **request)
                            )
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
                        elif operation == "list_conversations":
                            result = await chat_candidate.list_conversations(
                                db, tx, **request
                            )
                        else:
                            result = await chat_candidate.messages(db, tx, **request)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if operation in {
                "check_run",
                "query_cache",
                "model_authority",
                "action_projection",
                "messages",
                "list_conversations",
                "model_candidates",
                "read_tool",
                "preflight",
                "input_parts",
                "routing_context",
                "delivery_check",
                "delivery_probe",
            } or (operation == "finish_operation" and not result):
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            if operation in {"delivery_check", "delivery_probe"}:
                return {
                    "authority"
                    if operation == "delivery_check"
                    else "unchanged": result,
                    "statements": read_budget.statements,
                    "decoded_bytes": read_budget.decoded_bytes,
                }
            return result
