from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use
from app.core.base_response import PageResult, ResponseModel
from app.db import authorization_http
from app.db.session import get_db
from app.modules.ai.schemas.conversation import (
    ConversationCreate,
    ConversationOut,
    ConversationQuery,
    ConversationUpdate,
)
from app.modules.auth.service import access_identity
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
    identity = access_identity(_current_user)
    await authorization_http.release_request_reads(db)
    page = await authorization_http.get_authorization_runtime().chat.list_conversations(
        tenant=identity.tenant, actor_user_version=identity.user_version, query=query
    )
    return ResponseModel.success(data=page)


@router.get("/{conversation_id}", summary="获取会话详情 + 历史消息")
async def get_conversation_detail(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    identity = access_identity(_current_user)
    await authorization_http.release_request_reads(db)
    result = await authorization_http.get_authorization_runtime().chat.messages(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        conversation_id=conversation_id,
        detail=True,
    )
    return ResponseModel.success(data=result)


@router.post("", summary="创建会话")
async def create_conversation(
    data: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    identity = access_identity(_current_user)
    await authorization_http.release_request_reads(db)
    result = await authorization_http.get_authorization_runtime().chat.conversation(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        data=data.model_dump(),
        http_result=True,
    )
    return ResponseModel.success(data=result)


@router.put("/{conversation_id}", summary="更新会话")
async def update_conversation(
    conversation_id: int,
    data: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    identity = access_identity(_current_user)
    await authorization_http.release_request_reads(db)
    await authorization_http.get_authorization_runtime().chat.conversation(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        data=data.model_dump(exclude_unset=True),
        conversation_id=conversation_id,
    )
    return ResponseModel.success(msg="更新成功")


@router.delete("/{conversation_id}", summary="删除会话")
async def delete_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    identity = access_identity(_current_user)
    await authorization_http.release_request_reads(db)
    await authorization_http.get_authorization_runtime().chat.invalidate(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        conversation_id=conversation_id,
    )
    return ResponseModel.success(msg="删除成功")
