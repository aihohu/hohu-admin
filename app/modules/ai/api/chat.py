"""AI 对话流式接口（Vercel AI SDK 兼容 + 自定义事件）

spec §17.2 重写 + §8.1 流式协议：
  - Vercel AI SDK 原生 text-delta（`0: "..."`）保留
  - 自定义事件（tool_call_started / tool_call_result / confirmation_required / ai_error / done）
    走 `data: {...}\n\n` 格式，由 ChatDeps.signal_event 注入
  - ChatDeps.signal_event 是 asyncio.Queue.put 的封装

spec §4-§5（Task 11）: 顶层生成 trace_id + safety 短路统一写 routing_log +
Supervisor 路由块插入（safety 后、attach_trace 前）.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import uuid
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

from app.core.config import settings
from app.core.redis import redis_client
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


def _format_sse_chunk(event: AiStreamEvent) -> str:
    """把 AiStreamEvent 序列化为 SSE 帧：`data: {...}\n\n`

    spec §3.2 v1.5+: ConfirmationRequiredEvent 在 AI_SSE_RESUME_ENABLED=True 时
    自动附带 `id: <confirmation_id>` 字段（SSE 协议标准），客户端断流重连时
    浏览器/SDK 自动通过 Last-Event-ID 头携带此 id 到 /ai/chat/resume 端点。
    """
    data_line = f"data: {event_to_sse_data(event)}"
    # spec §3.2: 仅 confirmation_required 事件需要 id: 字段（其它事件 sequence 无意义）
    event_id: str | None = None
    if settings.AI_SSE_RESUME_ENABLED and isinstance(event, ConfirmationRequiredEvent):
        event_id = event.confirmation_id
    id_line = f"\nid: {event_id}" if event_id else ""
    return f"{data_line}{id_line}\n\n"


def _collect_text_delta(sse_frame: str, collected: list[str]) -> None:
    """从 SSE 帧提取 text-delta 累积到 collected（spec §8.1: Vercel UI Protocol v4）

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
    """spec §13 决策 14: safety 短路统一写 routing_log + emit AiErrorEvent.

    用于 keyword / topic / url 三个硬短路（injection 不是短路，单独处理）.
    所有 safety 短路必须由此 helper 处理，保证审计日志不断裂（spec §13 决策 14）.
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
        yield _format_sse_chunk(DoneEvent())

    return StreamingResponse(_stream(), media_type=accept)


router = APIRouter()


@router.post("", summary="流式对话（SSE）")
async def chat(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """Vercel AI SDK 兼容的流式对话接口

    spec §17.2 + §8.1：构造完整 ChatDeps（含 data_scope / perms / agent /
    trace_id / conversation_id / signal_event），合并 Vercel 原生 text-delta
    与自定义事件（tool_call_started / tool_call_result / confirmation_required）。

    spec §4-§5（Task 11）: 顶层生成 trace_id，safety 短路统一写 routing_log，
    safety 通过后调 Supervisor 路由（如启用），最后才持久化 user 消息（避免孤儿）.
    """
    # spec §13 决策 14: trace_id 在所有 audit log（routing_log / operation_log）共用，
    # 提前生成给 safety 短路 / build_chat_deps / attach_trace_to_conversation 复用.
    trace_id = f"tr_{uuid.uuid4().hex[:16]}"

    # 读取原始 body（只能读一次）
    raw_body = await request.body()

    # 解析 JSON
    body = json.loads(raw_body) if raw_body else {}
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

    # v1.5+ SR-25: 前端可能注入了 file_id 到最后一条 user message 末尾（chat 上传文件场景）。
    # display_content 是用户原始输入（不含注入），用于持久化 + UI 显示；
    # messages 里的注入版仅给 LLM 看。display_parts 是 display_content + 图片 parts。
    display_content = body.get("displayContent")
    display_parts: list[dict] | None = None
    if display_content is not None:
        display_parts = []
        if display_content:
            display_parts.append({"type": "text", "text": display_content})
        if user_parts:
            display_parts.extend(
                p
                for p in user_parts
                if p.get("type") == "file"
                and str(p.get("mediaType", "")).startswith("image/")
            )
        if not display_parts:
            display_parts = None

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

    # v1.5+: 前端传 agentCode 切换助手（默认 user_mgmt）
    # 提前解析：build_chat_deps 内部 stickiness + 后续 save_user_message 都要用
    agent_code = body.get("agentCode") or body.get("agent_code")

    # spec §13 决策 13: user 消息持久化推迟到路由块成功后（避免 safety / clarification
    # 路径产生孤儿消息）；早 save_user_message 块已移除.

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

    # 构造完整 ChatDeps（spec §4.6 + §17.2 + §5.3 粘滞）
    # spec §13 决策 15: agent_code 找不到 → 不让 ValueError 透到 FastAPI 默认 500，
    # 改为 emit AI_ROUTING_FAILED（spec §8）+ 写 routing_log.
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
            yield _format_sse_chunk(DoneEvent())

        return StreamingResponse(_agent_load_failed_stream(), media_type=accept)

    deps.conversation_id = conversation_id
    # §11.4: 注入 client_ip 给 executor（用于鉴权拒绝时的 IP 级拉黑计数）
    deps.client_ip = request.client.host if request.client else None

    # §11.4 IP 级拉黑短路：单 IP 1h 内 AI 鉴权拒绝 ≥ 50 → 拉黑 2h
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
                yield _format_sse_chunk(DoneEvent())

            return StreamingResponse(_ip_blocked_stream(), media_type=SSE_CONTENT_TYPE)

    # §11.4 用户级自动禁用短路：被禁用时 emit ai_error + done，流结束
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
            yield _format_sse_chunk(DoneEvent())

        return StreamingResponse(_disabled_stream(), media_type=SSE_CONTENT_TYPE)

    # §11.2 keyword_blocklist：用户输入命中项目自定义敏感词 → 整条消息拦截
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
            # spec §6.3 / §11.2 metric：关键词命中事件计数
            from app.modules.ai.metrics import record_security_event  # noqa: PLC0415

            record_security_event("keyword")
            # spec §13 决策 14: safety 短路统一写 routing_log（reason='safety_blocked'）
            return await _emit_safety_blocked(
                db,
                trace_id=trace_id,
                user_id=_current_user.user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                error_code="AI_KEYWORD_BLOCKED",
                error_msg="消息含敏感词，已被管理员配置拦截，请修改后再试",
            )

    # §11.2 v1.5+ SR-23 forbidden_topics：主题级黑名单（政治 / 宗教 / 竞品对比等）
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

    # §11.2 v1.5+ SR-23 forbidden_urls：URL 域名黑名单（竞品 / 恶意网站）
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

    # §11.1 prompt injection 检测（修订 S-16：跨轮持久化到 conversation 级）
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
    # spec §5: Supervisor 路由块（仅在 safety 通过后）
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
    # spec §7.2: llm_choice 是 LLM 解析后的 agent_code（仅 supervisor 成功路径有值），
    # 区别于 final_agent（粘滞 / 手动 / 路由三条路径都可能产生 final_agent）.
    llm_choice: str | None = None
    clarification_payload: dict | None = None
    routing_failed = False
    routing_latency_ms = 0

    if stick_decision and stick_decision.run_supervisor:
        if not supervisor_enabled:
            # spec §15.3: supervisor 关闭 → 用 DEFAULT_AGENT_CODE 旧行为
            route_reason = "supervisor_disabled"
            final_agent_code = DEFAULT_AGENT_CODE
        elif deps.injection_hit:
            # spec §13 决策 7: injection 命中 → 不调 supervisor LLM（防跨 LLM 污染）
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
                    }
                else:
                    # spec §9: increment-before-call 防并发逃配额.
                    # 权衡：LLM 抖动 / 网络超时也会扣用户配额（不 refund）.
                    # 因为 refund 会引入 race（攻击者故意触发 timeout 反复退额），
                    # 选择"宁错杀不放过"。运维监控 routing_log.reason='llm_call_failed'
                    # 比例异常时调高 sys_config.ai:supervisor_daily_limit.
                    await increment_daily_count(redis_client, _current_user.user_id)
                    start = time.monotonic()
                    result = await agent_router.route(db, user_message, candidates)
                    routing_latency_ms = int((time.monotonic() - start) * 1000)

                    if result.failed:
                        routing_failed = True
                        route_reason = result.reason
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
                        }
                        route_reason = result.reason
                    else:
                        final_agent_code = result.agent_code
                        llm_choice = result.agent_code  # spec §7.2 LLM 解析结果
                        route_reason = result.reason

    # 写 audit log（spec §13 决策 14: 覆盖所有路径）
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
                    error_code="AI_ROUTING_FAILED",
                    message="路由失败，请重试或手动选择 Agent",
                )
            )
            yield _format_sse_chunk(DoneEvent())

        return StreamingResponse(_routing_failed_stream(), media_type=accept)

    # clarification → emit + 结束（spec §13 决策 11: user 消息不落库）
    if clarification_payload is not None:
        await db.commit()

        async def _clarification_stream():
            yield _format_sse_chunk(ClarificationRequiredEvent(**clarification_payload))
            yield _format_sse_chunk(DoneEvent())

        return StreamingResponse(_clarification_stream(), media_type=accept)

    # 路由成功 / 粘滞 / 手动 → 注入 agent 到 deps（如还是 None）
    if deps.agent is None and final_agent_code:
        await chat_service.attach_agent_to_deps(deps, final_agent_code)

    # 现在才持久化 user 消息（spec §13 决策 13: 避免 safety / clarification 孤儿消息）
    if conversation_id and (user_message or user_parts):
        persist_content = (
            display_content if display_content is not None else user_message
        )
        persist_parts = display_parts if display_parts is not None else user_parts
        await chat_service.save_user_message(
            db,
            conversation_id,
            _current_user.user_id,
            persist_content,
            parts=persist_parts,
            agent_code=deps.agent.code,
        )

    # 把 trace_id + agent_code 写到 ai_conversation（spec §4.5）
    await chat_service.attach_trace_to_conversation(
        db, conversation_id, deps.agent.code, deps.trace_id
    )
    await db.commit()

    # 创建 Agent（按 user_perms + agent_code 过滤 tool，spec §5.4）
    agent = await chat_service.create_agent(
        db, model_name, user_perms=deps.perms, agent_code=deps.agent.code
    )

    # 流式响应：自定义事件队列 + PydanticAI stream 并发合并（spec §8.1）
    # spec §11: usage_limits 兜底防 agent 无限循环（tool_calls_limit=5 / request_limit=10）
    # 注：accept 已在路由块前定义（spec §13 决策 14：safety / 路由短路复用）
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
        # 修订 BUG-FE-18: 收集 tool_call 事件，save_assistant_message 时存到
        # ai_message.tool_calls JSON，前端重连会话时还原 streamEvents
        collected_tool_calls: list[dict] = []
        started_events: dict[str, dict] = {}  # tool_call_id → started event dict

        def _record_tool_event(ev):
            """拦截 ToolCallStarted/Result 事件，配对后存 collected_tool_calls

            args / result 在写入前调 stringify_large_ints：Snowflake ID 是 int64，
            超 JS Number.MAX_SAFE_INTEGER，DB JSON 列直接存 int 会让前端 reload
            会话时还原 streamEvents 丢精度（CLAUDE.md 跨项目硬规则 #3）。
            """
            from app.modules.ai.agents.hitl.events import (  # noqa: PLC0415
                ToolCallResultEvent,
                ToolCallStartedEvent,
                _ui_to_dict,
                stringify_large_ints,
            )

            if isinstance(ev, ToolCallStartedEvent):
                # 按 toolCallId 缓存 started；等 result 配对后入列
                started_events[ev.tool_call_id] = {
                    "tool": ev.tool,
                    "tool_call_id": ev.tool_call_id,
                    "summary": ev.summary,
                    "args": stringify_large_ints(ev.args),
                    "risk": ev.risk,
                    "trace_id": ev.trace_id,
                    "chip_target": ev.chip_target,
                }
            elif isinstance(ev, ToolCallResultEvent):
                started = started_events.pop(ev.tool_call_id, {})
                collected_tool_calls.append(
                    {
                        **started,
                        "ok": ev.ok,
                        "result": stringify_large_ints(ev.result),
                        "affected_rows": ev.affected_rows,
                        "error_code": ev.error_code,
                        "error_msg": ev.error_msg,
                        "duration_ms": ev.duration_ms,
                        "ui": _ui_to_dict(ev.ui) if ev.ui else None,
                    }
                )

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
                _record_tool_event(ev)
                await unified_queue.put(_format_sse_chunk(ev))

        async def produce_pydantic():
            try:
                async for chunk in adapter.encode_stream(event_stream):
                    # 提取 text-delta 收集（spec §8.1: Vercel UI Protocol v4
                    # `data: {"type":"text-delta","delta":"..."}\n\n`）
                    _collect_text_delta(chunk, collected)
                    await unified_queue.put(chunk)
            except UsageLimitExceeded as e:
                # spec §11.6: agent loop 超限（tool_calls_limit=5 / request_limit=10）
                # 显式 emit AiErrorEvent(AI_USAGE_LIMIT_EXCEEDED)，前端弹 $message.error
                logger.warning(
                    "PydanticAI usage limit exceeded",
                    extra={"trace_id": deps.trace_id, "error": str(e)},
                )
                await unified_queue.put(
                    _format_sse_chunk(
                        AiErrorEvent(
                            error_code="AI_USAGE_LIMIT_EXCEEDED",
                            message="AI 调用次数超限，请换种方式问或拆分任务",
                        )
                    )
                )
                await unified_queue.put(None)
            except Exception:
                # 其它未预期异常：log + sentinel，前端靠 SSE done 兜底
                logger.exception("PydanticAI stream error")
                await unified_queue.put(None)
            else:
                await unified_queue.put(None)  # sentinel

        drain_task = asyncio.create_task(drain_custom_events())
        pydantic_task = asyncio.create_task(produce_pydantic())

        # 主循环：消费 unified_queue
        try:
            while True:
                chunk = await unified_queue.get()
                if chunk is None:  # sentinel: PydanticAI stream 结束
                    break
                yield chunk
        finally:
            if not pydantic_task.done():
                pydantic_task.cancel()
                try:
                    await pydantic_task
                except (asyncio.CancelledError, Exception):
                    pass
            # 通知 drain_task 退出 + 等它处理完残留事件
            await custom_event_queue.put(None)
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass

        # 流结束 emit done（spec §8.1）
        yield _format_sse_chunk(DoneEvent())

        # 保存 AI 响应消息
        collected_text = "".join(collected)
        if saved_conversation_id and collected_text:
            try:
                await chat_service.save_assistant_message(
                    saved_db,
                    saved_conversation_id,
                    content=collected_text,
                    tool_calls=collected_tool_calls if collected_tool_calls else None,
                    agent_code=deps.agent.code if deps.agent else None,
                )
                await saved_db.commit()
            except Exception:
                logger.exception("save_assistant_message failed")

    return StreamingResponse(sse_with_save(), media_type=accept)
