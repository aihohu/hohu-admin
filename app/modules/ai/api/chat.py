import asyncio
import base64
import ipaddress
import json
import logging
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from pydantic_ai.ui import SSE_CONTENT_TYPE
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.config import settings
from app.db.session import get_db
from app.modules.ai.core.config import ChatDeps
from app.modules.ai.schemas.chat import ChatRequest
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)


def _is_private_url(url: str) -> bool:
    """判断 URL 是否指向内网地址（localhost / 私有 IP）"""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def _convert_local_images_to_data_uri_sync(body: dict) -> dict:
    """将 body 中内网图片 URL 替换为 base64 data URI（同步，在线程池中调用）"""
    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    for msg in body.get("messages", []):
        parts = msg.get("parts", [])
        for part in parts:
            if part.get("type") != "file":
                continue
            url = part.get("url", "")
            if not _is_private_url(url):
                continue
            parsed = urlparse(url)
            file_path = Path(parsed.path.lstrip("/")).resolve()
            if not file_path.is_relative_to(upload_dir):
                logger.warning("Image path outside upload dir: %s", file_path)
                continue
            if not file_path.exists():
                logger.warning("Image file not found: %s", file_path)
                continue
            media_type = part.get("mediaType", "image/jpeg")
            with open(file_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            part["url"] = f"data:{media_type};base64,{b64}"
    return body


async def _convert_local_images_to_data_uri(body: dict) -> dict:
    """将同步图片转换放到线程池，避免阻塞事件循环"""
    return await asyncio.to_thread(_convert_local_images_to_data_uri_sync, body)


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

    # 解析 JSON
    body = json.loads(raw_body) if raw_body else {}
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    if conversation_id is not None:
        conversation_id = int(conversation_id)

    # 提取用户消息文本和结构化 parts（在 base64 转换之前保存原始 parts）
    user_message = ""
    user_parts = None
    messages = body.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if last_msg.get("role") == "user":
            user_message = last_msg.get("content", "")
            raw_parts = last_msg.get("parts", [])
            if not user_message and raw_parts:
                user_message = "".join(
                    p.get("text", "") for p in raw_parts if p.get("type") == "text"
                )
            if raw_parts:
                user_parts = raw_parts

    # 将内网图片 URL 转为 base64 data URI（LLM 提供商无法访问内网）
    body = await _convert_local_images_to_data_uri(body)
    raw_body = json.dumps(body).encode()

    # 解析前端请求
    try:
        run_input = VercelAIAdapter.build_run_input(raw_body)
    except ValidationError as e:
        return Response(
            content=json.dumps(e.json()),
            media_type="application/json",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    # 保存用户消息
    if conversation_id and (user_message or user_parts):
        await chat_service.save_user_message(
            db, conversation_id, _current_user.user_id, user_message, parts=user_parts
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
