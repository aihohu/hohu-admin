from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationException, NotFoundException
from app.core.tenant import TenantContext, get_bound_tenant_context
from app.modules.ai.agents.gateway.redact import redact_secrets
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.conversation import ConversationCreate, ConversationUpdate
from app.modules.ai.schemas.message import MessageOut, MessageTombstoneOut
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.ai.service.reconciliation_annotation import reconciliation_notice
from app.modules.ai.service.result_projection_service import (
    ProjectionLineage,
    result_projection_service,
)
from app.modules.system.models.user import User
from app.utils.pagination import build_filters, paginate


class ConversationService:
    """AI 会话管理服务"""

    async def get_list(
        self,
        db: AsyncSession,
        query,
        user_id: int,
        *,
        tenant: TenantContext,
    ):
        field_mapping = {
            "title": ("title", "contains"),
            "status": "status",
        }
        filters = build_filters(
            AiConversation, field_mapping, **query.model_dump(exclude_unset=True)
        )
        # 只看自己的会话
        filters.append(AiConversation.tenant_id == tenant.tenant_id)
        filters.append(AiConversation.user_id == user_id)
        filters.append(AiConversation.deleted_at.is_(None))
        return await paginate(
            db=db,
            model=AiConversation,
            query_params=query,
            filters=filters,
            order_by=AiConversation.update_time.desc(),
        )

    async def get_by_id(
        self,
        db: AsyncSession,
        conversation_id: int,
        user_id: int | None = None,
        *,
        tenant: TenantContext,
    ) -> AiConversation:
        obj = (
            await db.execute(
                select(AiConversation).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if not obj or obj.deleted_at is not None:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        if user_id and obj.user_id != user_id:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        return obj

    async def create(
        self,
        db: AsyncSession,
        data: ConversationCreate,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> AiConversation:
        self._require_actor(tenant, user_id)
        selected = await model_authorization_service.authorize_chat_model(
            db,
            data.model_name,
            tenant=tenant,
        )
        obj = AiConversation(
            tenant_id=tenant.tenant_id,
            user_id=user_id,
            title=data.title or "新对话",
            system_prompt=data.system_prompt,
            model_name=str(selected.model.model_id),
        )
        db.add(obj)
        return obj

    async def update(
        self,
        db: AsyncSession,
        conversation_id: int,
        data: ConversationUpdate,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> AiConversation:
        self._require_actor(tenant, user_id)
        obj = await self.get_by_id(db, conversation_id, user_id, tenant=tenant)
        update_data = data.model_dump(exclude_unset=True)
        if "model_name" in update_data:
            selected = await model_authorization_service.authorize_chat_model(
                db,
                update_data["model_name"],
                tenant=tenant,
            )
            update_data["model_name"] = str(selected.model.model_id)
        for field, value in update_data.items():
            setattr(obj, field, value)
        return obj

    async def delete(
        self,
        db: AsyncSession,
        conversation_id: int,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> None:
        self._require_actor(tenant, user_id)
        obj = await self.get_by_id(db, conversation_id, user_id, tenant=tenant)
        obj.deleted_at = datetime.now(UTC)

    async def lock_for_delete(
        self,
        db: AsyncSession,
        conversation_id: int,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> AiConversation:
        """Lock the conversation before checking durable action blockers."""
        obj = (
            await db.execute(
                select(AiConversation)
                .where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id == conversation_id,
                    AiConversation.user_id == user_id,
                    AiConversation.deleted_at.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if obj is None:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        return obj

    async def get_messages(
        self,
        db: AsyncSession,
        conversation_id: int,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> list[AiMessage]:
        """获取会话的所有历史消息

        加载时再次脱敏，防止历史未脱敏数据回灌模型上下文。
        """
        await self.get_by_id(db, conversation_id, user_id, tenant=tenant)  # 验证权限
        stmt = (
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.conversation_id == conversation_id,
                AiMessage.is_active.is_(True),
            )
            .order_by(AiMessage.create_time.asc(), AiMessage.message_id.asc())
        )
        result = await db.execute(stmt)
        messages = list(result.scalars().all())
        # 历史消息加载时再次脱敏，防止敏感信息回灌。
        for msg in messages:
            if msg.content:
                msg.content = redact_secrets(msg.content)
        return messages

    async def get_routing_history(
        self,
        db: AsyncSession,
        conversation_id: int,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> list[str]:
        """Read bounded user intent for routing without exposing historic results."""
        await self.get_by_id(db, conversation_id, user_id, tenant=tenant)
        contents = list(
            await db.scalars(
                select(AiMessage.content)
                .where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id == conversation_id,
                    AiMessage.is_active.is_(True),
                    AiMessage.role == "user",
                    AiMessage.content.is_not(None),
                    AiMessage.content != "",
                )
                .order_by(AiMessage.message_id.desc())
                .limit(6)
            )
        )
        # Select scalar strings so sanitizing cannot modify persisted messages.
        return [redact_secrets(content)[:1000] for content in reversed(contents)]

    async def save_message(
        self,
        db: AsyncSession,
        conversation_id: int,
        role: str,
        content: str,
        message_type: str = "text",
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        parts: list[dict] | None = None,
        tool_calls: list[dict] | None = None,
        agent_code: str | None = None,
        parent_message_id: int | None = None,
        trace_id: str | None = None,
        is_active: bool = True,
        supersedes_message_id: int | None = None,
        lineage: ProjectionLineage | None = None,
        projection_dependency_message_ids: list[int] | tuple[int, ...] = (),
        *,
        tenant: TenantContext,
    ) -> AiMessage:
        """保存一条消息

        agent_code 透传到 ai_message.agent_code。
        （按消息粒度记录处理 Agent，让历史会话也能还原）.
        用户输入保存前先执行 redact_secrets，防止敏感信息回灌 LLM 上下文。
        修订 BUG-FE-18: assistant 消息含 tool_calls 时存 JSON，前端重连还原卡片
        """
        if role == "user" and content:
            content = redact_secrets(content)

        await self.get_by_id(db, conversation_id, tenant=tenant)
        if lineage is not None and lineage.tenant_id != tenant.tenant_id:
            raise AuthenticationException(
                "租户上下文无效", error_code="TENANT_CONTEXT_INVALID"
            )

        msg = AiMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            message_type=message_type,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            parts=parts,
            tool_calls=tool_calls,
            agent_code=agent_code,
            parent_message_id=parent_message_id,
            trace_id=trace_id,
            is_active=is_active,
            supersedes_message_id=supersedes_message_id,
            tenant_id=tenant.tenant_id,
            tool_codes=list(lineage.tool_codes) if lineage else None,
            subject_refs=list(lineage.subject_refs) if lineage else None,
            subject_refs_hash=lineage.subject_refs_hash if lineage else None,
            data_scope_hash=lineage.data_scope_hash if lineage else None,
            resolver_version=lineage.resolver_version if lineage else None,
            projection_dependency_message_ids=[
                str(value) for value in projection_dependency_message_ids
            ],
        )
        db.add(msg)
        return msg

    async def project_messages(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        current_user: User,
    ) -> list[MessageOut | MessageTombstoneOut]:
        """Project each sensitive message through the current authorization policy."""
        tenant = get_bound_tenant_context(current_user)
        messages = await self.get_messages(
            db,
            conversation_id,
            current_user.user_id,
            tenant=tenant,
        )
        # Completion services depend on conversation/result projection services.
        from app.modules.ai.service.completion_read_state_service import (  # noqa: PLC0415
            completion_read_state_service,
        )

        completion_states = await completion_read_state_service.read(
            tenant=tenant,
            tool_call_ids=(
                call["tool_call_id"]
                for message in messages
                for call in message.tool_calls or ()
                if isinstance(call, dict) and isinstance(call.get("tool_call_id"), str)
            ),
        )
        return await self.project_locked_messages(
            db,
            current_user=current_user,
            messages=messages,
            completion_states=completion_states,
        )

    async def project_locked_messages(
        self, db, *, current_user, messages, completion_states
    ):
        """Shared DTO projection; the caller supplies its coherent completion snapshot."""
        tenant = get_bound_tenant_context(current_user)
        from app.modules.ai.service.completion_read_state_service import (  # noqa: PLC0415
            effective_call_state,
            project_verified_call,
            project_verified_content,
        )

        projected: list[MessageOut | MessageTombstoneOut] = []
        for message in messages:
            if message.role == "user":
                projected.append(MessageOut.model_validate(message))
                continue
            allowed = await result_projection_service.authorize_message_projection(
                db,
                current_user,
                owner_user_id=current_user.user_id,
                message=message,
            )
            unknown_ids = {
                call["tool_call_id"]
                for call in message.tool_calls or ()
                if isinstance(call, dict)
                and isinstance(call.get("tool_call_id"), str)
                and (state := effective_call_state(call, completion_states))
                and state.unknown
            }
            if unknown_ids:
                output = MessageOut.model_validate(message)
                # Work on DTO copies: reading history never repairs persisted cards.
                output.tool_calls = [
                    {key: call[key] for key in ("tool", "tool_call_id") if key in call}
                    if call.get("tool_call_id") in unknown_ids or not allowed
                    else project_verified_call(call, completion_states)
                    for call in output.tool_calls or ()
                ]
                output.parts = None
                output.completed_tool_call_ids = []
                content = (output.content or "") if allowed else ""
                if any(
                    completion_states[key].error_code
                    == "AI_EXECUTION_RECONCILIATION_REQUIRED"
                    for key in unknown_ids
                    if key in completion_states
                ):
                    # Historic success/failure prose is not an authoritative outcome.
                    content = ""
                output.content = reconciliation_notice(
                    project_verified_content(
                        content, output.tool_calls, completion_states
                    ),
                    unknown_ids,
                )
                projected.append(output)
                continue
            if allowed:
                output = MessageOut.model_validate(message)
                output.content = project_verified_content(
                    output.content, output.tool_calls, completion_states
                )
                if output.tool_calls:
                    output.tool_calls = [
                        project_verified_call(call, completion_states)
                        for call in output.tool_calls
                    ]
                lineage = result_projection_service.lineage_from_record(message)
                if lineage is not None and output.tool_calls:
                    output.tool_calls = (
                        await result_projection_service.refresh_download_urls(
                            db,
                            current_user,
                            tenant=tenant,
                            lineage=lineage,
                            value=output.tool_calls,
                        )
                    )
                projected.append(output)
            else:
                receipt = await result_projection_service.deletion_receipt(
                    db,
                    current_user,
                    owner_user_id=current_user.user_id,
                    message=message,
                    completion_states=completion_states,
                )
                if receipt is not None:
                    projected.append(
                        MessageOut(
                            message_id=message.message_id,
                            conversation_id=message.conversation_id,
                            parent_message_id=message.parent_message_id,
                            role="assistant",
                            message_type="text",
                            content=receipt.content,
                            completed_tool_call_ids=list(
                                receipt.completed_tool_call_ids
                            ),
                            parts=None,
                            tokens_input=None,
                            tokens_output=None,
                            tool_calls=None,
                            trace_id=message.trace_id,
                            is_active=message.is_active,
                            supersedes_message_id=message.supersedes_message_id,
                            create_time=message.create_time,
                        )
                    )
                    continue
                projected.append(
                    MessageTombstoneOut(
                        messageId=message.message_id,
                        role=message.role,
                    )
                )
        return projected

    @staticmethod
    def _require_actor(tenant: TenantContext, user_id: int) -> None:
        if tenant.actor_user_id != user_id:
            raise AuthenticationException(
                "租户上下文无效", error_code="TENANT_CONTEXT_INVALID"
            )


conversation_service = ConversationService()
