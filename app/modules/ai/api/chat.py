import json
from http import HTTPStatus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from pydantic_ai.ui import SSE_CONTENT_TYPE
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.core.config import ChatDeps
from app.modules.ai.schemas.chat import ChatRequest
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()


@router.post("", summary="流式对话（SSE）")
async def chat(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """Vercel AI SDK 兼容的流式对话接口"""
    # 读取原始 body（只能读一次）
    raw_body = await request.body()

    # 解析前端请求
    try:
        run_input = VercelAIAdapter.build_run_input(raw_body)
    except ValidationError as e:
        return Response(
            content=json.dumps(e.json()),
            media_type="application/json",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    # 从已读的 body 解析 JSON 获取会话信息
    body = json.loads(raw_body) if raw_body else {}
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    if conversation_id is not None:
        conversation_id = int(conversation_id)

    # 提取用户消息文本
    user_message = ""
    messages = body.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if last_msg.get("role") == "user":
            # 兼容 content 和 parts 两种格式
            user_message = last_msg.get("content", "")
            if not user_message:
                parts = last_msg.get("parts", [])
                user_message = "".join(
                    p.get("text", "") for p in parts if p.get("type") == "text"
                )

    # 保存用户消息
    if conversation_id and user_message:
        await chat_service.save_user_message(
            db, conversation_id, _current_user.user_id, user_message
        )
        await db.commit()

    # 解析模型选择
    conv = None
    if conversation_id:
        conv = await conversation_service.get_by_id(
            db, int(conversation_id), _current_user.user_id
        )

    model_name = body.get("modelId") or None

    # 回退到会话绑定的模型
    if not model_name and conv:
        model_name = conv.model_name

    # 将使用的模型更新到会话
    if conv and model_name and conv.model_name != model_name:
        conv.model_name = model_name

    # 创建 Agent
    agent = await chat_service.create_agent(db, model_name)

    # 流式响应
    accept = request.headers.get("accept", SSE_CONTENT_TYPE)
    adapter = VercelAIAdapter(agent=agent, run_input=run_input, accept=accept)
    event_stream = adapter.run_stream(
        deps=ChatDeps(user_id=_current_user.user_id, db=db)
    )

    # 包装 SSE 流：收集 AI 回复文本，流结束后保存到数据库
    saved_conversation_id = conversation_id
    saved_db = db

    async def sse_with_save():
        collected_text = ""
        async for chunk in adapter.encode_stream(event_stream):
            # 尝试从 SSE data 中提取 text-delta
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    event = json.loads(chunk[6:])
                    if event.get("type") == "text-delta" and event.get("delta"):
                        collected_text += event["delta"]
                except (json.JSONDecodeError, KeyError):
                    pass
            yield chunk

        # 流结束，保存 AI 响应
        if saved_conversation_id and collected_text:
            await chat_service.save_assistant_message(
                saved_db, saved_conversation_id, content=collected_text
            )
            await saved_db.commit()

    return StreamingResponse(sse_with_save(), media_type=accept)


@router.post("/sync", summary="非流式对话")
async def chat_sync(
    data: ChatRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """非流式对话接口，返回完整响应"""
    # 保存用户消息
    await chat_service.save_user_message(
        db, data.conversation_id, _current_user.user_id, data.message
    )

    # 获取会话绑定的模型
    conv = await conversation_service.get_by_id(
        db, data.conversation_id, _current_user.user_id
    )

    # 创建 Agent 并运行
    agent = await chat_service.create_agent(db, conv.model_name)
    result = await agent.run(
        data.message, deps=ChatDeps(user_id=_current_user.user_id, db=db)
    )

    # 保存 AI 响应
    await chat_service.save_assistant_message(
        db,
        data.conversation_id,
        content=result.output,
        tokens_input=result.usage().request_tokens if result.usage() else None,
        tokens_output=result.usage().response_tokens if result.usage() else None,
    )
    await db.commit()

    return ResponseModel.success(data={"content": result.output})
