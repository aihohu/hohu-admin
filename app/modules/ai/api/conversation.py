from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import BusinessRuleException
from app.core.tenant import resolve_tenant_id
from app.db.session import get_db
from app.modules.ai.schemas.conversation import (
    ConversationCreate,
    ConversationOut,
    ConversationQuery,
    ConversationUpdate,
)
from app.modules.ai.schemas.message import MessageOut
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/list",
    summary="获取会话列表",
    response_model=ResponseModel[PageResult[ConversationOut]],
)
async def get_conversation_list(
    query: ConversationQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    page_data = await conversation_service.get_list(db, query, _current_user.user_id)
    return ResponseModel.success(data=page_data)


@router.get("/{conversation_id}", summary="获取会话详情 + 历史消息")
async def get_conversation_detail(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    conversation = await conversation_service.get_by_id(
        db, conversation_id, _current_user.user_id
    )
    messages = await conversation_service.get_messages(
        db, conversation_id, _current_user.user_id
    )
    conv_out = ConversationOut.model_validate(conversation)
    msg_outs = [MessageOut.model_validate(m) for m in messages]
    actions = await prepared_action_service.list_pending_for_conversation(
        db,
        conversation_id=conversation_id,
        user_id=_current_user.user_id,
        tenant_id=resolve_tenant_id(_current_user),
    )
    pending_actions = [
        prepared_action_service.to_pending_out(action) for action in actions
    ]
    return ResponseModel.success(
        data={
            "conversation": conv_out,
            "messages": msg_outs,
            "pendingActions": pending_actions,
        }
    )


@router.post("", summary="创建会话")
async def create_conversation(
    data: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    conversation = await conversation_service.create(
        db,
        data,
        _current_user.user_id,
        tenant_id=resolve_tenant_id(_current_user),
    )
    await db.commit()
    await db.refresh(conversation)
    return ResponseModel.success(data=ConversationOut.model_validate(conversation))


@router.put("/{conversation_id}", summary="更新会话")
async def update_conversation(
    conversation_id: int,
    data: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    await conversation_service.update(
        db,
        conversation_id,
        data,
        _current_user.user_id,
        tenant_id=resolve_tenant_id(_current_user),
    )
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete("/{conversation_id}", summary="删除会话")
async def delete_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    await conversation_service.lock_for_delete(
        db, conversation_id, _current_user.user_id
    )
    has_in_progress = await prepared_action_service.has_in_progress_for_conversation(
        db,
        conversation_id=conversation_id,
        user_id=_current_user.user_id,
        tenant_id=resolve_tenant_id(_current_user),
    )
    if has_in_progress:
        error = BusinessRuleException(
            "会话仍有待确认或执行中的操作，完成或拒绝后再删除",
            error_code="AI_CHAT_RUN_IN_PROGRESS",
        )
        error.code = 409
        raise error
    await conversation_service.delete(db, conversation_id, _current_user.user_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")
