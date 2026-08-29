"""AI 对话流式接口（Vercel AI SDK 兼容 + 自定义事件）

聊天流入口与 SSE 协议：
  - Vercel AI SDK 原生 text-delta（`0: "..."`）保留
  - 自定义事件（tool_call_started / tool_call_result / confirmation_required / ai_error / done）
    走 `data: {...}\n\n` 格式，由 ChatDeps.signal_event 注入
  - ChatDeps.signal_event 是 asyncio.Queue.put 的封装

顶层生成 trace_id；安全短路统一写 routing_log，并
Supervisor 路由块插入（safety 后、attach_trace 前）.
"""

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
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.ui import SSE_CONTENT_TYPE
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from pydantic_ai.usage import UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use
from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.redis import redis_client
from app.core.tenant import resolve_tenant_id
from app.db.session import get_db
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    AiStreamEvent,
    ConfirmationRequiredEvent,
    DoneEvent,
    event_to_sse_data,
)
from app.modules.ai.agents.safety.auto_disable import check_user_disabled
from app.modules.ai.agents.safety.forbidden_topics import (
    check_topics,
    load_forbidden_topics,
)
from app.modules.ai.agents.safety.forbidden_urls import (
    check_forbidden_urls,
    load_forbidden_urls,
)
from app.modules.ai.agents.safety.injection_detector import (
    detect_injection,
    is_injection_hit_conversation,
    record_injection_hit_conversation,
)
from app.modules.ai.agents.safety.ip_blacklist import is_ip_blacklisted
from app.modules.ai.agents.safety.keyword_blocklist import (
    check_keywords,
    load_blocklist,
)
from app.modules.ai.core.provider_egress import is_provider_failure
from app.modules.ai.schemas.chat import resolve_chat_trace_id
from app.modules.ai.schemas.model import ModelOption
from app.modules.ai.service.chat_run_service import (
    ToolCallCollector,
    chat_run_finalizer,
    chat_run_guard,
    enforce_grounded_management_write_claim,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.ai.service.result_projection_service import (
    ProjectionLineage,
    result_projection_service,
)
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


def _format_sse_chunk(event: AiStreamEvent) -> str:
    """把 AiStreamEvent 序列化为 SSE 帧：`data: {...}\n\n`

    启用 SSE 续传时，``ConfirmationRequiredEvent``
    自动附带 `id: <confirmation_id>` 字段（SSE 协议标准），客户端断流重连时
    浏览器/SDK 自动通过 Last-Event-ID 头携带此 id 到 /ai/chat/resume 端点。
    """
    data_line = f"data: {event_to_sse_data(event)}"
    # 仅待确认事件需要 id 字段，其他事件没有可续传的序号语义。
    event_id: str | None = None
    if settings.AI_SSE_RESUME_ENABLED and isinstance(event, ConfirmationRequiredEvent):
        event_id = event.confirmation_id
    id_line = f"\nid: {event_id}" if event_id else ""
    return f"{data_line}{id_line}\n\n"


def _collect_text_delta(sse_frame: str, collected: list[str]) -> None:
    """从 Vercel UI Protocol SSE 帧提取 text-delta 并累积到 collected。

    后端 PydanticAI 1.89 的 `VercelAIAdapter.encode_stream` 输出 Vercel UI
    Protocol v4：`data: {"type":"text-delta","delta":"..."}\n\n`。本函数从中提取
    delta 字段累积，给 save_assistant_message 用。其它类型（start / text-start /
    text-end / reasoning-* / tool-call / tool-result / finish / [DONE]）跳过。
    """
    if not (sse_frame.startswith("data: ") and sse_frame.endswith("\n\n")):
        return
    payload = sse_frame[6:-2]
    if not payload.startswith("{"):
        return  # [DONE] 或纯文本（理论不会出现）
    try:
        ev = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return
    if (
        isinstance(ev, dict)
        and ev.get("type") == "text-delta"
        and isinstance(ev.get("delta"), str)
    ):
        collected.append(ev["delta"])


def _run_guard_heartbeat_ttl(*, pending_handoff: bool) -> int:
    """HITL heartbeat must never shrink the lease below its confirmation window."""
    if pending_handoff:
        return (
            settings.AI_HITL_PENDING_TTL_SEC
            + settings.AI_CHAT_RUN_GUARD_PENDING_GRACE_SEC
        )
    return settings.AI_CHAT_RUN_GUARD_TTL_SEC


async def _finalize_stream_turn(
    db: AsyncSession,
    *,
    conversation_id: int,
    trace_id: str,
    source_user_message_id: int,
    content: str,
    tool_calls: list[dict] | None,
    agent_code: str | None,
    stream_error_code: str | None,
    lineage: ProjectionLineage | None = None,
    projection_dependency_message_ids: list[int] | tuple[int, ...] = (),
) -> list[AiStreamEvent]:
    """建立 durability barrier：assistant/terminal commit 完成后才构造 done。"""
    if stream_error_code is not None:
        await db.rollback()
        return [
            DoneEvent(
                trace_id=trace_id,
                persistence="failed",
                projection="updated",
            )
        ]
    content, unverified_write_claim = enforce_grounded_management_write_claim(
        content,
        agent_code=agent_code,
        tool_calls=tool_calls,
    )
    try:
        message = await chat_run_finalizer.finalize_assistant_turn(
            db,
            conversation_id=conversation_id,
            trace_id=trace_id,
            source_user_message_id=source_user_message_id,
            content=content,
            tool_calls=tool_calls,
            agent_code=agent_code,
            lineage=lineage,
            projection_dependency_message_ids=projection_dependency_message_ids,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "finalize_assistant_turn failed",
            extra={"conversation_id": conversation_id, "trace_id": trace_id},
        )
        return [
            AiErrorEvent(
                error_code="AI_MESSAGE_PERSIST_FAILED",
                message="AI 回复持久化失败，请刷新会话确认状态",
            ),
            DoneEvent(
                trace_id=trace_id,
                persistence="failed",
                projection="updated",
            ),
        ]
    events: list[AiStreamEvent] = []
    if unverified_write_claim:
        events.append(
            AiErrorEvent(
                error_code="AI_UNVERIFIED_WRITE_CLAIM",
                message="AI 回复缺少可验证的写工具结果，未确认任何业务变更",
            )
        )
    events.append(
        DoneEvent(
            trace_id=trace_id,
            message_id=message.message_id if message else None,
            persistence="committed",
            projection="updated",
        )
    )
    return events


async def _emit_safety_blocked(
    db: AsyncSession,
    *,
    trace_id: str,
    user_id: int,
    conversation_id: int | None,
    user_message: str,
    error_code: str,
    error_msg: str,
    accept: str = SSE_CONTENT_TYPE,
) -> StreamingResponse:
    """安全检查短路时统一写路由日志并发送 ``AiErrorEvent``。

    用于 keyword / topic / url 三个硬短路（injection 不是短路，单独处理）.
    所有安全短路都必须经过此 helper，保证审计日志连续。
    """
    from app.modules.ai.service.routing_log_service import (  # noqa: PLC0415
        routing_log_service,
    )

    await routing_log_service.write_log(
        db,
        trace_id=trace_id,
        user_id=user_id,
        conversation_id=conversation_id,
        input_message=user_message or "",  # 防 None
        candidates=[],
        llm_choice=None,
        final_agent=None,
        reason="safety_blocked",
        latency_ms=0,
    )
    await db.commit()

    async def _stream():
        yield _format_sse_chunk(AiErrorEvent(error_code=error_code, message=error_msg))
        yield _format_sse_chunk(
            DoneEvent(
                trace_id=trace_id,
                persistence="not_applicable",
                projection="unchanged",
            )
        )

    return StreamingResponse(
        _stream(),
        media_type=accept,
        headers={"X-AI-Trace-ID": trace_id},
    )


router = APIRouter()


@router.get(
    "/models",
    summary="列出当前租户可用于对话的模型",
    response_model=ResponseModel[list[ModelOption]],
)
async def list_chat_models(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_chat_use),
) -> ResponseModel[list[ModelOption]]:
    items = await model_authorization_service.list_model_options(
        db,
        tenant_id=resolve_tenant_id(current_user),
    )
    return ResponseModel.success(data=items)


@router.post("", summary="流式对话（SSE）")
async def chat(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
):
    """Vercel AI SDK 兼容的流式对话接口

    构造完整 ``ChatDeps``，包含 data_scope、权限、agent、
    trace_id / conversation_id / signal_event），合并 Vercel 原生 text-delta
    与自定义事件（tool_call_started / tool_call_result / confirmation_required）。

    顶层生成 trace_id；安全短路统一写 routing_log，
    safety 通过后调 Supervisor 路由（如启用），最后才持久化 user 消息（避免孤儿）.
    """
    # 读取原始 body（只能读一次）
    raw_body = await request.body()

    # 解析 JSON
    body = json.loads(raw_body) if raw_body else {}
    trace_id = resolve_chat_trace_id(body.get("traceId") or body.get("trace_id"))
    command_action = body.get("action", "send")
    if command_action != "send":
        raise BusinessRuleException(
            "消息编辑与重新生成尚未开放",
            error_code="AI_CHAT_COMMAND_INVALID",
        )
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    if conversation_id is not None:
        conversation_id = int(conversation_id)

    # 提取用户消息文本和结构化 parts
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

    # 上传文件时，前端可能把 file_id 追加到最后一条用户消息末尾。
    # display_content 是用户原始输入（不含注入），用于持久化 + UI 显示；
    # messages 里的注入版仅给 LLM 看。display_parts 是 display_content + 所有 file parts
    # （含 image + 非 image 文件如 Excel/CSV，前者渲染 <img>，后者渲染文件 chip）。
    display_content = body.get("displayContent")
    display_parts: list[dict] | None = None
    if display_content is not None:
        display_parts = []
        if display_content:
            display_parts.append({"type": "text", "text": display_content})
        if user_parts:
            display_parts.extend(p for p in user_parts if p.get("type") == "file")
        if not display_parts:
            display_parts = None

    # 将内网图片 URL 转为 base64 data URI（LLM 提供商无法访问内网）
    body = await _convert_local_images_to_data_uri(body)

    # 给 PydanticAI 的请求体移除非 image 文件 part + 剥 fileSize（PydanticAI FileUIPart.url
    # 必须合法 http(s) URL 且不允许 extra 字段；Excel/CSV 等业务文件无预览 URL，通过 file_id
    # 在 request body 其他字段传递，发 LLM 时不应作为 file part 出现）。
    # user_parts（持久化用）已在前面提取，保留完整文件元数据用于 UI chip 渲染。
    body_for_llm = dict(body)
    new_messages = []
    for msg in body.get("messages", []):
        new_msg = dict(msg)
        parts = msg.get("parts")
        if isinstance(parts, list):
            filtered = []
            for p in parts:
                if not isinstance(p, dict):
                    filtered.append(p)
                    continue
                if p.get("type") == "file":
                    if not str(p.get("mediaType", "")).startswith("image/"):
                        continue
                    p = {k: v for k, v in p.items() if k != "fileSize"}
                filtered.append(p)
            if filtered:
                new_msg["parts"] = filtered
            else:
                new_msg.pop("parts", None)
        new_messages.append(new_msg)
    body_for_llm["messages"] = new_messages

    # 解析前端请求
    try:
        run_input = VercelAIAdapter.build_run_input(json.dumps(body_for_llm).encode())
    except ValidationError as e:
        return Response(
            content=json.dumps(e.json()),
            media_type="application/json",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    # 前端通过 agentCode 切换助手，未指定时使用 user_mgmt。
    # 提前解析：build_chat_deps 内部 stickiness + 后续 save_user_message 都要用
    agent_was_supplied = "agentCode" in body or "agent_code" in body
    if "agentCode" in body:
        agent_code = body["agentCode"]
    elif "agent_code" in body:
        agent_code = body["agent_code"]
    else:
        agent_code = None
    if agent_was_supplied and (
        not isinstance(agent_code, str) or not agent_code.strip()
    ):
        raise AuthorizationException(
            "The requested AI Agent is unavailable",
            error_code="AI_AGENT_FORBIDDEN",
        )

    # 用户消息在路由成功后再持久化，避免安全拦截或澄清流程
    # 路径产生孤儿消息）；早 save_user_message 块已移除.

    # 解析模型选择
    conv = None
    projection_dependency_message_ids: list[int] = []
    if conversation_id:
        conv = await conversation_service.get_by_id(
            db, int(conversation_id), _current_user.user_id
        )
        projection_dependency_message_ids = (
            await result_projection_service.collect_message_projection_dependencies(
                db,
                conversation_id=int(conversation_id),
            )
        )
        await chat_service.ensure_trace_available(
            db,
            conversation_id=conversation_id,
            trace_id=trace_id,
        )

    model_was_supplied = "modelId" in body or "model_id" in body
    if "modelId" in body:
        model_name = body["modelId"]
    elif "model_id" in body:
        model_name = body["model_id"]
    else:
        model_name = None

    # 回退到会话绑定的模型
    if model_was_supplied and model_name is None:
        model_name = ""
    if not model_was_supplied and model_name is None and conv:
        model_name = conv.model_name

    # 显式/既有会话模型可以在路由前校验；新会话未指定模型时必须等 Agent
    # 授权完成，再使用其 model_preference，不能提前固化成全局第一项。
    selected_model = None
    if model_name is not None:
        selected_model = await model_authorization_service.authorize_chat_model(
            db,
            model_name,
            tenant_id=resolve_tenant_id(_current_user),
        )
        model_name = str(selected_model.model.model_id)

    # 构造包含数据权限和粘滞路由信息的完整 ChatDeps。
    # agent_code 不存在时转换为稳定的 AI_ROUTING_FAILED 事件并记录路由日志，
    # 不让 ValueError 透传为默认 500。
    try:
        deps = await chat_service.build_chat_deps(
            db,
            _current_user,
            agent_code=agent_code,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
    except ValueError as exc:
        from app.modules.ai.service.routing_log_service import (  # noqa: PLC0415
            routing_log_service,
        )

        # 捕获异常消息到本地变量，避免 generator 函数体内 e 退出 except 后被 deref.
        err_msg = str(exc)
        logger.warning(
            "agent load failed",
            extra={"error": err_msg, "trace_id": trace_id},
        )
        await routing_log_service.write_log(
            db,
            trace_id=trace_id,
            user_id=_current_user.user_id,
            conversation_id=conversation_id,
            input_message=user_message or "",
            candidates=[],
            llm_choice=None,
            final_agent=None,
            reason="agent_load_failed",
            latency_ms=0,
        )
        await db.commit()

        accept = request.headers.get("accept", SSE_CONTENT_TYPE)

        async def _agent_load_failed_stream():
            yield _format_sse_chunk(
                AiErrorEvent(
                    error_code="AI_ROUTING_FAILED",
                    message=f"Agent 加载失败：{err_msg}",
                )
            )
            yield _format_sse_chunk(
                DoneEvent(
                    trace_id=trace_id,
                    persistence="not_applicable",
                    projection="unchanged",
                )
            )

        return StreamingResponse(
            _agent_load_failed_stream(),
            media_type=accept,
            headers={"X-AI-Trace-ID": trace_id},
        )

    deps.conversation_id = conversation_id
    deps.command_action = command_action
    deps.projection_dependency_message_ids = tuple(projection_dependency_message_ids)
    # 注入 client_ip，供执行器统计鉴权拒绝并实施 IP 级封禁。
    deps.client_ip = request.client.host if request.client else None

    # 单个 IP 一小时内鉴权拒绝达到阈值后封禁两小时。
    if deps.client_ip:
        if await is_ip_blacklisted(redis_client, deps.client_ip):
            logger.warning(
                "ip blacklisted blocked chat",
                extra={"ip": deps.client_ip, "user_id": _current_user.user_id},
            )

            async def _ip_blocked_stream():
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_IP_BLOCKED",
                        message="您的 IP 因异常 AI 调用被临时拉黑，请联系管理员",
                    )
                )
                yield _format_sse_chunk(
                    DoneEvent(
                        trace_id=trace_id,
                        persistence="not_applicable",
                        projection="unchanged",
                    )
                )

            return StreamingResponse(
                _ip_blocked_stream(),
                media_type=SSE_CONTENT_TYPE,
                headers={"X-AI-Trace-ID": trace_id},
            )

    # 用户被自动禁用时发送 ai_error 和 done，并结束流。
    if await check_user_disabled(redis_client, _current_user.user_id):
        logger.warning(
            "user auto-disabled blocked chat",
            extra={
                "user_id": _current_user.user_id,
                "user_name": _current_user.user_name,
                "conversation_id": conversation_id,
            },
        )

        async def _disabled_stream():
            yield _format_sse_chunk(
                AiErrorEvent(
                    error_code="AI_USER_AUTO_DISABLED",
                    message="AI 功能已被自动禁用（24h），如非本人操作请联系管理员",
                )
            )
            yield _format_sse_chunk(
                DoneEvent(
                    trace_id=trace_id,
                    persistence="not_applicable",
                    projection="unchanged",
                )
            )

        return StreamingResponse(
            _disabled_stream(),
            media_type=SSE_CONTENT_TYPE,
            headers={"X-AI-Trace-ID": trace_id},
        )

    # 用户输入命中项目自定义敏感词时拦截整条消息。
    blocklist = await load_blocklist(db)
    if user_message:
        hits = check_keywords(user_message, blocklist)
        if hits:
            logger.warning(
                "keyword_blocklist blocked chat",
                extra={
                    "user_id": _current_user.user_id,
                    "conversation_id": conversation_id,
                    "hit_count": len(hits),
                },
            )
            # 记录关键词命中事件指标。
            from app.modules.ai.metrics import record_security_event  # noqa: PLC0415

            record_security_event("keyword")
            # 安全短路统一记录 routing_log，原因为 safety_blocked。
            return await _emit_safety_blocked(
                db,
                trace_id=trace_id,
                user_id=_current_user.user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                error_code="AI_KEYWORD_BLOCKED",
                error_msg="消息含敏感词，已被管理员配置拦截，请修改后再试",
            )

    # 主题级黑名单用于拦截政治、宗教、竞品对比等受限主题。
    topics = await load_forbidden_topics(db)
    if user_message:
        topic_hits = check_topics(user_message, topics)
        if topic_hits:
            logger.warning(
                "forbidden_topics blocked chat",
                extra={
                    "user_id": _current_user.user_id,
                    "conversation_id": conversation_id,
                    "hit_count": len(topic_hits),
                },
            )
            from app.modules.ai.metrics import record_security_event  # noqa: PLC0415

            record_security_event("forbidden_topic")
            return await _emit_safety_blocked(
                db,
                trace_id=trace_id,
                user_id=_current_user.user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                error_code="AI_FORBIDDEN_TOPIC",
                error_msg="消息涉及禁讨论主题，请修改后再试",
            )

    # URL 域名黑名单用于拦截竞品或恶意网站。
    url_blocklist = await load_forbidden_urls(db)
    if user_message:
        url_hits = check_forbidden_urls(user_message, url_blocklist)
        if url_hits:
            logger.warning(
                "forbidden_urls blocked chat",
                extra={
                    "user_id": _current_user.user_id,
                    "conversation_id": conversation_id,
                    "hit_count": len(url_hits),
                },
            )
            from app.modules.ai.metrics import record_security_event  # noqa: PLC0415

            record_security_event("forbidden_url")
            return await _emit_safety_blocked(
                db,
                trace_id=trace_id,
                user_id=_current_user.user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                error_code="AI_FORBIDDEN_URL",
                error_msg="消息含禁访问的链接，请删除后重试",
            )

    # Prompt injection 检测结果持久化到会话级，跨轮次生效。
    # 流程：
    #   1. 本轮 detect_injection（仅扫当前 user message）
    #   2. 命中 → 写 Redis ai:injection_hit:{conversation_id} TTL 1h
    #   3. is_injection_hit_conversation 读历史 → deps.injection_hit = 本轮 OR 历史
    # 这样攻击者拆分注入到多轮（每轮只触发 1 个 pattern）也会被 conversation
    # 级 flag 兜住，后续轮次 tool 调用强制 HITL。
    current_hit = detect_injection(user_message)
    if current_hit and conversation_id is not None:
        await record_injection_hit_conversation(redis_client, conversation_id)
    history_hit = await is_injection_hit_conversation(redis_client, conversation_id)
    deps.injection_hit = current_hit or history_hit

    if deps.injection_hit:
        logger.warning(
            "prompt injection detected",
            extra={
                "user_id": _current_user.user_id,
                "conversation_id": conversation_id,
                "trace_id": deps.trace_id,
                "current_hit": current_hit,
                "history_hit": history_hit,
            },
        )

    # ============================================================
    # Supervisor 路由仅在安全检查通过后执行。
    # ============================================================
    # 不重调 stickiness（build_chat_deps 内已调，挂在 deps.sticky_decision）
    # 路由块必须严格在 attach_trace_to_conversation 之前 —— 否则 deps.agent.code
    # 会 AttributeError（deps.agent 在 run_supervisor=True 时为 None，由本块注入）.
    import time  # noqa: PLC0415

    from app.modules.ai.agents.hitl.events import (  # noqa: PLC0415
        ClarificationRequiredEvent,
    )
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_bool,
    )
    from app.modules.ai.agents.supervisor.quota import (  # noqa: PLC0415
        check_supervisor_quota,
        increment_daily_count,
    )
    from app.modules.ai.agents.supervisor.router import agent_router  # noqa: PLC0415
    from app.modules.ai.constants import DEFAULT_AGENT_CODE  # noqa: PLC0415
    from app.modules.ai.service.agent_visibility import (  # noqa: PLC0415
        list_visible_agents,
    )
    from app.modules.ai.service.routing_log_service import (  # noqa: PLC0415
        routing_log_service,
    )

    accept = request.headers.get("accept", SSE_CONTENT_TYPE)

    stick_decision = deps.sticky_decision
    supervisor_enabled = await get_ai_config_bool(
        db, "ai:supervisor_enabled", default=True
    )

    candidates: list = []
    final_agent_code: str | None = stick_decision.agent_code if stick_decision else None
    route_reason: str = stick_decision.reason if stick_decision else "no_decision"
    # llm_choice 是 LLM 解析出的 agent_code，仅 Supervisor 成功时有值；
    # 区别于 final_agent（粘滞 / 手动 / 路由三条路径都可能产生 final_agent）.
    llm_choice: str | None = None
    clarification_payload: dict | None = None
    routing_failed = False
    routing_error_code = "AI_ROUTING_FAILED"
    routing_latency_ms = 0

    if stick_decision and stick_decision.run_supervisor:
        if not supervisor_enabled:
            # Supervisor 关闭时使用默认 agent_code。
            route_reason = "supervisor_disabled"
            final_agent_code = DEFAULT_AGENT_CODE
        elif deps.injection_hit:
            # 命中注入检测后不调用 Supervisor LLM，避免跨模型污染。
            route_reason = "injection_blocked_from_supervisor"
            final_agent_code = DEFAULT_AGENT_CODE
        elif not user_message or not user_message.strip():
            # 兜底：空消息不进 supervisor（防 LLM 乱选）
            route_reason = "empty_message"
            final_agent_code = DEFAULT_AGENT_CODE
        else:
            candidates = await list_visible_agents(db, _current_user)
            if not candidates:
                routing_failed = True
                route_reason = "no_candidates"
                routing_error_code = "AI_AGENT_NOT_AVAILABLE"
            else:
                quota = await check_supervisor_quota(db, user_id=_current_user.user_id)
                if not quota.allowed:
                    route_reason = "quota_exceeded"
                    clarification_payload = {
                        "candidates": tuple(
                            {
                                "code": c.code,
                                "name": c.name,
                                "description": c.description,
                            }
                            for c in candidates
                        ),
                        "message": "AI 路由配额已用尽，请手动选择 Agent",
                        "reason_code": "quota_exceeded",
                    }
                else:
                    # 调用前先递增计数，防止并发绕过配额。
                    # 权衡：LLM 抖动 / 网络超时也会扣用户配额（不 refund）.
                    # 因为 refund 会引入 race（攻击者故意触发 timeout 反复退额），
                    # 选择"宁错杀不放过"。运维监控 routing_log.reason='llm_call_failed'
                    # 比例异常时调高 sys_config.ai:supervisor_daily_limit.
                    await increment_daily_count(redis_client, _current_user.user_id)
                    start = time.monotonic()
                    result = await agent_router.route(
                        db,
                        user_message,
                        candidates,
                        tenant_id=deps.tenant_id,
                    )
                    routing_latency_ms = int((time.monotonic() - start) * 1000)

                    if result.failed:
                        routing_failed = True
                        route_reason = result.reason
                        if result.reason == "no_candidates":
                            routing_error_code = "AI_AGENT_NOT_AVAILABLE"
                    elif result.clarification:
                        clarification_payload = {
                            "candidates": tuple(
                                {
                                    "code": c.code,
                                    "name": c.name,
                                    "description": c.description,
                                }
                                for c in result.candidates
                            ),
                            "message": "请确认你想咨询哪类问题",
                            "reason_code": "selection_required",
                        }
                        route_reason = result.reason
                    else:
                        final_agent_code = result.agent_code
                        llm_choice = result.agent_code  # LLM 解析出的路由结果。
                        route_reason = result.reason

    # Supervisor 结果和安全/default fallback 也必须再次通过统一 Agent Policy。
    if not routing_failed and clarification_payload is None and final_agent_code:
        if deps.agent is None:
            try:
                await chat_service.attach_agent_to_deps(deps, final_agent_code)
            except AuthorizationException as exc:
                routing_failed = True
                routing_error_code = exc.error_code or "AI_AGENT_NOT_AVAILABLE"
                route_reason = "agent_policy_rejected"

    # 每个真正要进入执行 Agent 的新 LLM run 都在路由日志/会话 commit 前复验。
    # 无显式/会话模型时使用已授权 Agent 的偏好；偏好无效时 fail closed，
    # 绝不静默回退到其它模型。
    if not routing_failed and clarification_payload is None and deps.agent is not None:
        if selected_model is None:
            model_ref = getattr(deps.agent, "model_preference", None)
            selected_model = await model_authorization_service.authorize_chat_model(
                db,
                model_ref,
                tenant_id=deps.tenant_id,
            )
        model_name = str(selected_model.model.model_id)
        if conv is not None and conv.model_name != model_name:
            conv.model_name = model_name
        deps.resolved_model_id = selected_model.model.model_id
        deps.resolved_provider_id = selected_model.provider.provider_id

    # 所有路由路径都写入审计日志。
    # input_message 用 `or ""` 兜底：边界防御统一为空串写入 HMAC hash.
    # 注：success path 在此处立即 commit；clarification / failed 路径在 emit stream 前 commit.
    await routing_log_service.write_log(
        db,
        trace_id=trace_id,
        user_id=_current_user.user_id,
        conversation_id=conversation_id,
        input_message=user_message or "",
        candidates=candidates,
        llm_choice=llm_choice,
        final_agent=final_agent_code,
        reason=route_reason,
        latency_ms=routing_latency_ms,
    )
    # success path 立即提交，避免后续 attach_trace_to_conversation 失败时丢日志
    if not routing_failed and clarification_payload is None:
        await db.commit()

    # AI_ROUTING_FAILED → emit error + 结束（user 消息不落库）
    if routing_failed:
        await db.commit()

        async def _routing_failed_stream():
            yield _format_sse_chunk(
                AiErrorEvent(
                    error_code=routing_error_code,
                    message=(
                        "当前没有可用的 AI Agent，请联系管理员授权"
                        if routing_error_code == "AI_AGENT_NOT_AVAILABLE"
                        else "路由失败，请重试或手动选择 Agent"
                    ),
                )
            )
            yield _format_sse_chunk(
                DoneEvent(
                    trace_id=trace_id,
                    persistence="not_applicable",
                    projection="unchanged",
                )
            )

        return StreamingResponse(
            _routing_failed_stream(),
            media_type=accept,
            headers={"X-AI-Trace-ID": trace_id},
        )

    # 需要澄清时发送事件并结束，用户消息不落库。
    if clarification_payload is not None:
        await db.commit()

        async def _clarification_stream():
            yield _format_sse_chunk(ClarificationRequiredEvent(**clarification_payload))
            yield _format_sse_chunk(
                DoneEvent(
                    trace_id=trace_id,
                    persistence="not_applicable",
                    projection="unchanged",
                )
            )

        return StreamingResponse(
            _clarification_stream(),
            media_type=accept,
            headers={"X-AI-Trace-ID": trace_id},
        )

    # 创建 Agent，并按用户权限和 agent_code 过滤工具。
    agent = await chat_service.create_agent(
        db,
        model_name,
        user_perms=deps.perms,
        agent_code=deps.agent.code,
        tenant_id=deps.tenant_id,
        agent_config=deps.agent,
    )

    # 所有持久化变更前先获取会话级 owner lease。
    # create_agent 放在 guard 前，provider 配置失败不会留下 source/guard 半状态。
    guard_owner_token: str | None = None
    if conversation_id is not None:
        from app.modules.ai.service.prepared_action_service import (  # noqa: PLC0415
            prepared_action_service,
        )

        action_in_progress = (
            await prepared_action_service.has_in_progress_for_conversation(
                db,
                conversation_id=conversation_id,
                user_id=_current_user.user_id,
                tenant_id=deps.tenant_id,
            )
        )
        if action_in_progress:
            exc = BusinessRuleException(
                "该会话仍有待确认或正在执行的操作",
                error_code="AI_CHAT_RUN_IN_PROGRESS",
            )
            exc.code = 409
            raise exc
        guard_owner_token = chat_run_guard.generate_owner_token()
        acquired = await chat_run_guard.acquire(
            redis_client,
            conversation_id=conversation_id,
            owner_token=guard_owner_token,
        )
        if not acquired:
            exc = BusinessRuleException(
                "该会话已有 AI 操作正在执行",
                error_code="AI_CHAT_RUN_IN_PROGRESS",
            )
            exc.code = 409
            raise exc
        deps.guard_owner_token = guard_owner_token
        action_in_progress = (
            await prepared_action_service.has_in_progress_for_conversation(
                db,
                conversation_id=conversation_id,
                user_id=_current_user.user_id,
                tenant_id=deps.tenant_id,
            )
        )
        if action_in_progress:
            await chat_run_guard.release(
                redis_client,
                conversation_id=conversation_id,
                owner_token=guard_owner_token,
            )
            exc = BusinessRuleException(
                "该会话仍有待确认或正在执行的操作",
                error_code="AI_CHAT_RUN_IN_PROGRESS",
            )
            exc.code = 409
            raise exc

    try:
        # 现在才持久化 user 消息（避免 safety / clarification 孤儿消息）。flush 后
        # message_id 立即成为本 run 的 source 因果键，供 Gateway operation 使用。
        if conversation_id and (user_message or user_parts):
            persist_content = (
                display_content if display_content is not None else user_message
            )
            persist_parts = display_parts if display_parts is not None else user_parts
            source_message = await chat_service.save_user_message(
                db,
                conversation_id,
                _current_user.user_id,
                persist_content,
                parts=persist_parts,
                agent_code=deps.agent.code,
                trace_id=deps.trace_id,
                tenant_id=deps.tenant_id,
            )
            deps.source_user_message_id = source_message.message_id

        # 将 trace_id 和 agent_code 写入会话，供追踪和粘滞路由使用。
        await chat_service.attach_trace_to_conversation(
            db, conversation_id, deps.agent.code, deps.trace_id
        )
        await db.commit()
    except Exception:
        await db.rollback()
        if conversation_id is not None and guard_owner_token is not None:
            await chat_run_guard.release(
                redis_client,
                conversation_id=conversation_id,
                owner_token=guard_owner_token,
            )
        raise

    # 并发合并自定义事件队列和 PydanticAI 流。
    # usage_limits 防止 Agent 无限循环（tool_calls_limit=5 / request_limit=10）。
    # accept 在路由前定义，供安全检查和路由短路复用。
    adapter = VercelAIAdapter(agent=agent, run_input=run_input, accept=accept)
    event_stream = adapter.run_stream(
        deps=deps,
        usage_limits=UsageLimits(
            request_limit=10,  # 总 LLM 请求数上限（含初始 + 每个 tool 后续）
            tool_calls_limit=5,  # 单轮 tool 调用上限（防 LLM 失控循环调相同 tool）
        ),
    )

    # 注入 signal_event：execute_tool emit 事件 → push 到 queue
    custom_event_queue: asyncio.Queue[AiStreamEvent] = asyncio.Queue()

    async def _signal_event(event: AiStreamEvent) -> None:
        await custom_event_queue.put(event)

    deps.signal_event = _signal_event

    saved_conversation_id = conversation_id
    saved_db = db

    async def sse_with_save():
        """合并 PydanticAI vercel stream + 自定义事件 queue → 单一 SSE 输出"""
        collected: list[str] = []
        tool_collector = ToolCallCollector()
        stream_error_code: str | None = None

        # 生产者：跑 PydanticAI stream，把 vercel chunk 转发到 unified_queue
        # 修订 BUG: PydanticAI 调 tool fn 时 stream 协程在 tool fn 内 hang（HITL
        # 等 wake 5min），原实现 drain custom_event_queue 绑定在 vercel chunk
        # 之后，stream hang 时不 drain → confirmation_required 事件卡 queue 没到
        # 前端。修法：独立 drain_task 异步消费 custom_event_queue，不依赖 vercel
        # chunk 流转。
        unified_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def drain_custom_events() -> None:
            """独立任务：消费 custom_event_queue（tool_call_started /
            confirmation_required / tool_call_result 等自定义事件）

            与 produce_pydantic 并行运行，事件到达立即转发到 unified_queue。
            收到 None sentinel 退出。
            """
            while True:
                ev = await custom_event_queue.get()
                if ev is None:  # sentinel
                    break
                tool_collector.record(ev)
                await unified_queue.put(_format_sse_chunk(ev))

        async def produce_pydantic():
            nonlocal stream_error_code
            try:
                async for chunk in adapter.encode_stream(event_stream):
                    # 提取并收集 Vercel UI Protocol 的 text-delta。
                    # `data: {"type":"text-delta","delta":"..."}\n\n`）
                    _collect_text_delta(chunk, collected)
                    await unified_queue.put(chunk)
            except UsageLimitExceeded as e:
                # Agent 循环超过工具调用或请求次数限制。
                # 显式 emit AiErrorEvent(AI_USAGE_LIMIT_EXCEEDED)，前端弹 $message.error
                logger.warning(
                    "PydanticAI usage limit exceeded",
                    extra={"trace_id": deps.trace_id, "error": str(e)},
                )
                stream_error_code = "AI_USAGE_LIMIT_EXCEEDED"
                await unified_queue.put(
                    _format_sse_chunk(
                        AiErrorEvent(
                            error_code="AI_USAGE_LIMIT_EXCEEDED",
                            message="AI 调用次数超限，请换种方式问或拆分任务",
                        )
                    )
                )
            except Exception as exc:
                if is_provider_failure(exc):
                    # 不记录上游异常文本，避免 body、URL 或密钥进入日志。
                    logger.warning(
                        "PydanticAI Provider stream failed",
                        extra={"trace_id": deps.trace_id},
                    )
                    stream_error_code = "AI_PROVIDER_UPSTREAM_ERROR"
                    error_message = "Provider 暂时不可用，请稍后重试"
                else:
                    # 其它未预期异常：log + sentinel，前端靠 SSE done 兜底
                    logger.exception("PydanticAI stream error")
                    stream_error_code = "AI_INTERNAL_ERROR"
                    error_message = "AI 流式响应异常，请稍后重试"
                await unified_queue.put(
                    _format_sse_chunk(
                        AiErrorEvent(
                            error_code=stream_error_code,
                            message=error_message,
                        )
                    )
                )
            finally:
                # The model stream may finish while tool events are still queued.
                # Close and fully drain that queue before publishing the terminal
                # sentinel; otherwise the consumer can stop before the final
                # started/result/confirmation card reaches the browser.
                await custom_event_queue.put(None)
                try:
                    await drain_task
                finally:
                    await unified_queue.put(None)

        drain_task = asyncio.create_task(drain_custom_events())
        pydantic_task = asyncio.create_task(produce_pydantic())

        async def heartbeat_guard() -> None:
            nonlocal stream_error_code
            if saved_conversation_id is None or guard_owner_token is None:
                return
            while True:
                await asyncio.sleep(settings.AI_CHAT_RUN_GUARD_HEARTBEAT_SEC)
                try:
                    renewed = await chat_run_guard.renew(
                        redis_client,
                        conversation_id=saved_conversation_id,
                        owner_token=guard_owner_token,
                        ttl_sec=_run_guard_heartbeat_ttl(
                            pending_handoff=deps.guard_handoff
                        ),
                    )
                except Exception:
                    logger.exception(
                        "chat run guard heartbeat failed",
                        extra={"conversation_id": saved_conversation_id},
                    )
                    renewed = False
                if renewed:
                    continue
                stream_error_code = "AI_CHAT_GUARD_LOST"
                await unified_queue.put(
                    _format_sse_chunk(
                        AiErrorEvent(
                            error_code="AI_CHAT_GUARD_LOST",
                            message="会话执行锁已失效，请刷新后重试",
                        )
                    )
                )
                pydantic_task.cancel()
                return

        heartbeat_task = asyncio.create_task(heartbeat_guard())

        # 主循环：消费 unified_queue
        stream_consumed = False
        try:
            while True:
                chunk = await unified_queue.get()
                if chunk is None:  # sentinel: PydanticAI stream 结束
                    break
                yield chunk
            stream_consumed = True
        finally:
            if not pydantic_task.done():
                pydantic_task.cancel()
                try:
                    await pydantic_task
                except (asyncio.CancelledError, Exception):
                    pass
            # produce_pydantic normally owns the drain barrier. Client disconnect
            # may cancel it before startup, so keep this idempotent fallback.
            if not drain_task.done():
                await custom_event_queue.put(None)
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass
            if not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
            # 客户端在 terminal barrier 前断流：普通 run 已被取消，可释放；HITL
            # pending 已把 durable handoff context 写入 payload，必须保留 lease。
            if (
                not stream_consumed
                and saved_conversation_id is not None
                and guard_owner_token is not None
                and not deps.guard_handoff
            ):
                try:
                    await chat_run_guard.release(
                        redis_client,
                        conversation_id=saved_conversation_id,
                        owner_token=guard_owner_token,
                    )
                except Exception:
                    logger.exception(
                        "chat run guard release after disconnect failed",
                        extra={"conversation_id": saved_conversation_id},
                    )

        try:
            collected_text = "".join(collected)
            if saved_conversation_id and deps.source_user_message_id:
                projection_snapshot = tool_collector.snapshot_projection()
                terminal_events = await _finalize_stream_turn(
                    saved_db,
                    conversation_id=saved_conversation_id,
                    trace_id=deps.trace_id,
                    source_user_message_id=deps.source_user_message_id,
                    content=collected_text,
                    tool_calls=tool_collector.snapshot() or None,
                    agent_code=deps.agent.code if deps.agent else None,
                    stream_error_code=stream_error_code,
                    lineage=(
                        result_projection_service.freeze_lineage(
                            tenant_id=deps.tenant_id,
                            agent_code=deps.agent.code,
                            tool_codes=projection_snapshot[0],
                            subject_refs=projection_snapshot[1],
                            data_scope_hash=(
                                deps.data_scope_hash if projection_snapshot[2] else None
                            ),
                            projection_dependency_message_ids=(
                                deps.projection_dependency_message_ids
                            ),
                        )
                        if deps.agent is not None and projection_snapshot[3]
                        else None
                    ),
                    projection_dependency_message_ids=(
                        projection_dependency_message_ids
                    ),
                )
            else:
                terminal_events = [
                    DoneEvent(
                        trace_id=deps.trace_id,
                        persistence="not_applicable",
                        projection="unchanged",
                    )
                ]
            for terminal_event in terminal_events:
                yield _format_sse_chunk(terminal_event)
        finally:
            # pending handoff 的 guard 交给 confirm/resume/TTL 收口；其它路径必须在
            # terminal commit（或失败 rollback）之后由原 owner compare-and-delete。
            if (
                saved_conversation_id is not None
                and guard_owner_token is not None
                and not deps.guard_handoff
            ):
                try:
                    await chat_run_guard.release(
                        redis_client,
                        conversation_id=saved_conversation_id,
                        owner_token=guard_owner_token,
                    )
                except Exception:
                    logger.exception(
                        "chat run guard release failed",
                        extra={"conversation_id": saved_conversation_id},
                    )

    return StreamingResponse(
        sse_with_save(),
        media_type=accept,
        headers={"X-AI-Trace-ID": trace_id},
    )
