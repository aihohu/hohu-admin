from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.modules.ai.agents.gateway.redact import redact_secrets
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.conversation import ConversationCreate, ConversationUpdate
from app.modules.ai.schemas.message import MessageOut, MessageTombstoneOut
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.ai.service.result_projection_service import (
    ProjectionLineage,
    result_projection_service,
)
from app.modules.system.models.user import User
from app.utils.pagination import build_filters, paginate


class ConversationService:
    """AI 会话管理服务"""

    async def get_list(self, db: AsyncSession, query, user_id: int):
        field_mapping = {
            "title": ("title", "contains"),
            "status": "status",
        }
        filters = build_filters(
            AiConversation, field_mapping, **query.model_dump(exclude_unset=True)
        )
        # 只看自己的会话
        filters.append(AiConversation.user_id == user_id)
        return await paginate(
            db=db,
            model=AiConversation,
            query_params=query,
            filters=filters,
            order_by=AiConversation.update_time.desc(),
        )

    async def get_by_id(
        self, db: AsyncSession, conversation_id: int, user_id: int | None = None
    ) -> AiConversation:
        obj = await db.get(AiConversation, conversation_id)
        if not obj:
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
        tenant_id: int,
    ) -> AiConversation:
        selected = await model_authorization_service.authorize_chat_model(
            db,
            data.model_name,
            tenant_id=tenant_id,
        )
        obj = AiConversation(
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
        tenant_id: int,
    ) -> AiConversation:
        obj = await self.get_by_id(db, conversation_id, user_id)
        update_data = data.model_dump(exclude_unset=True)
        if "model_name" in update_data:
            selected = await model_authorization_service.authorize_chat_model(
                db,
                update_data["model_name"],
                tenant_id=tenant_id,
            )
            update_data["model_name"] = str(selected.model.model_id)
        for field, value in update_data.items():
            setattr(obj, field, value)
        return obj

    async def delete(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> None:
        obj = await self.get_by_id(db, conversation_id, user_id)
        await db.delete(obj)

    async def lock_for_delete(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> AiConversation:
        """Lock the conversation before checking durable action blockers."""
        obj = (
            await db.execute(
                select(AiConversation)
                .where(
                    AiConversation.conversation_id == conversation_id,
                    AiConversation.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if obj is None:
            raise NotFoundException(
                resource_type="AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
            )
        return obj

    async def get_messages(
        self, db: AsyncSession, conversation_id: int, user_id: int
    ) -> list[AiMessage]:
        """获取会话的所有历史消息

        加载时再次脱敏，防止历史未脱敏数据回灌模型上下文。
        """
        await self.get_by_id(db, conversation_id, user_id)  # 验证权限
        stmt = (
            select(AiMessage)
            .where(
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
        tenant_id: int | None = None,
        lineage: ProjectionLineage | None = None,
        projection_dependency_message_ids: list[int] | tuple[int, ...] = (),
    ) -> AiMessage:
        """保存一条消息

        agent_code 透传到 ai_message.agent_code。
        （按消息粒度记录处理 Agent，让历史会话也能还原）.
        用户输入保存前先执行 redact_secrets，防止敏感信息回灌 LLM 上下文。
        修订 BUG-FE-18: assistant 消息含 tool_calls 时存 JSON，前端重连还原卡片
        """
        if role == "user" and content:
            content = redact_secrets(content)

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
            tenant_id=lineage.tenant_id if lineage else tenant_id,
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
        messages = await self.get_messages(db, conversation_id, current_user.user_id)
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
            if allowed:
                output = MessageOut.model_validate(message)
                lineage = result_projection_service.lineage_from_record(message)
                if lineage is not None and output.tool_calls:
                    output.tool_calls = (
                        await result_projection_service.refresh_download_urls(
                            db,
                            current_user,
                            lineage=lineage,
                            value=output.tool_calls,
                        )
                    )
                projected.append(output)
            else:
                projected.append(
                    MessageTombstoneOut(
                        messageId=message.message_id,
                        role=message.role,
                    )
                )
        return projected


conversation_service = ConversationService()
