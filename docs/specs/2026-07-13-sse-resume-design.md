# SSE 断流续传（HITL 期热接管） — v1.5+

**Status**: ⚠️ Plan v1.5+（待实现）
**Created**: 2026-07-13
**Owner**: hohu core team
**Depends on**: §8.4.1 多 worker HITL（已完成 2026-07-13）/ §8.3 `/ai/confirm` / §8.5 MVP 断流兜底
**Related**: [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 / §8.3 / §8.5 / §14 / §22

---

## 1. Context

### 1.1 问题

当前 SSE 流（`POST /ai/chat`）是单次请求拉起的 in-memory 协程，所有事件通过 `asyncio.Queue` 转发。`spec §8.5 MVP 简化`明确「不做 sequence_id / 心跳定时器」：

- `EventSource.onerror` 触发 → 前端提示"网络中断，操作已取消，请重新发起"
- 后端检测流断 → 把 pending confirmation 标记 `expired`

但生产场景下用户在 HITL 期断流的概率不低：
- 移动端切后台 → 系统杀进程
- 网络抖动 / Wi-Fi 切 4G
- 浏览器刷新 / 误关标签页后浏览器历史回退
- 笔记本休眠

断流即取消 → 用户必须从头发起对话，LLM 已经决策好的 tool 调用要重跑一遍（重复 dry_run / 重复 LLM 决策成本）。

### 1.2 触发

`spec §14 v1.5+ Roadmap` 条目：

> | SSE sequence_id 断点续传 | 网络抖动频繁 | 单调递增 sequence + `Last-Event-ID` 头重连 |

配套 `v1.5+ 多 worker HITL（pub/sub）`（已完成 2026-07-13，commit `0259752`）— 多 worker 是续传的**架构前提**（memory 模式下进程内 dict，新 worker 无法接管）。

### 1.3 范围

**只覆盖 HITL 挂起期**（用户已选）。sequence_id 只在 `confirmation_required` 事件上分配，不缓存 text-delta / reasoning-delta / tool_call_started 等中间事件。

理由：
- HITL 期是最长的"等待窗口"（最长 5min），断流概率最高
- HITL 期续传的收益最大（LLM 决策不丢、dry_run 不重跑、用户填的 reason 不丢）
- 流式生成期（text-delta）断流：用户重新发消息即可，LLM 重跑成本可接受

### 1.4 预期结果

- 客户端在 HITL 期断流后，能用标准 SSE 协议（`Last-Event-ID` 头）自动重连到新端点 `GET /ai/chat/resume`
- 新 worker 接管原 confirmation（hang → wake → execute_tool → emit 结果）
- 并发安全默认到位（Redis owner 锁防双执行）
- 配置驱动：`AI_SSE_RESUME_ENABLED` 可关闭（默认开）

---

## 2. 关键设计决策

### 2.1 用 SSE 标准协议字段（`id:` + `Last-Event-ID` 头），不发明私有 query param

SSE 协议规范（[WHATWG](https://html.spec.whatwg.org/multipage/server-sent-events.html) / [MDN](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)）原生支持：
- 每个事件可设 `id:` 字段
- 客户端断流重连时**自动**通过 `Last-Event-ID` HTTP 头携带最后事件的 id
- 浏览器原生 `EventSource` 类自动重连

服务端实现：

```
data: {"type":"confirmation_required","confirmationId":"abc123",...}
id: abc123

```

新端点 `GET /ai/chat/resume` 读 `Last-Event-ID` 头作为 confirmation_id；同时支持 `?confirmation_id=` query param 作为调试 / 非 EventSource 客户端的后备。

**为什么用标准协议**：
- 开源项目优先用标准协议能力，外部 SDK / 跨语言客户端零成本接入
- 浏览器原生 EventSource 类自动重连，未来某端点改用 EventSource 实现零代码改动
- spec 一眼看懂（不需要解释项目私有约定）
- `hohu-cli` 未来加的 Python / Go 客户端能直接复用 SSE 协议

**反例**：私有 `?confirmation_id=xxx` query param — 需要看项目 spec 才懂，外部贡献者门槛高；非标准客户端（如 iOS SDK）要手工拼接 URL。

**回归**：端点同时支持 query param 作为调试后备（curl 一目了然），但 spec 主推 `Last-Event-ID` 头。

### 2.2 新增 `confirmation_resumed` 事件类型（不重放 confirmation_required）

重放 `confirmation_required` 是**滥用事件语义** — 事件名说"要求确认"，重连后再发一次让读 spec 的人困惑（"用户已经确认过了为什么还要确认？"）。

新事件 `confirmation_resumed` 语义明确："流已重连，等待原 confirmation 结果"。

```typescript
type AiStreamEvent =
  | { type: "text-delta"; delta: string }
  // ... 其它原有事件 ...
  | { type: "confirmation_required";
      confirmationId: string; tool: string; toolCallId: string;
      summary: string; args: Record<string, unknown>;
      dryRun?: { summary: string; affectedCount: number; affectedExamples?: string[] };
      expiresAt: string }
  | { type: "confirmation_resumed";   // ← 新增
      confirmationId: string; tool: string; toolCallId: string;
      summary: string; args: Record<string, unknown>;
      dryRun?: { summary: string; affectedCount: number; affectedExamples?: string[] };
      expiresAt: string;
      resumedAt: string }              // 新字段：ISO 8601 UTC，前端显示"重连于 14:32"
  | { type: "ai_error"; errorCode: string; message: string }
  | { type: "done" };
```

前端处理：

```javascript
case "confirmation_required":
case "confirmation_resumed":
  // 共用渲染逻辑（schema 兼容，前端可统一处理）
  showConfirmationDrawer(event)
  if (event.type === "confirmation_resumed") {
    drawer.setReconnectedBadge()  // 仅 UI 标记（"已重连"chip）
  }
```

**为什么新增事件不重放**：
- 事件 schema 自解释（看到 `confirmation_resumed` 就知道是重连分支）
- 前端可做差异化 UI（重连后抽屉显示"已重连"提示）
- spec / 前端代码 / 后端代码三处一致，无歧义

**反例**：重放 `confirmation_required` → 前端要做"是否已在 pending 状态"去重逻辑（看似简单但语义不纯）；外部贡献者读 spec 困惑。

**回归**：两个事件 schema 兼容（共用字段），前端可统一渲染。

### 2.3 并发安全默认到位（Redis SETNX owner 锁）

**已知风险**：客户端断流后立即重连到 worker B，但 worker A 的 SSE 协程 cancel 还没完成（cancel 是协作式的，要等 `await` 点让出）。极端 race：
1. worker B 抢锁成功 → 开始 hang
2. worker A 的 cancel 终于传播到 hang → A 的 hang 抛 CancelledError
3. 用户点 confirm → wake → publish channel
4. worker B 收到 message → 醒来 → execute_tool
5. 但 worker A 的 execute_tool 在 cancel 到达前**可能已经执行了**（如果 wake 已经先 publish 过，A 的 hang 已经醒了）

结果：tool 执行两次。破坏性操作（如 `user.batch_delete`）可能删两次。

**默认安全实现**：Redis SETNX owner 锁 + Lua 脚本释放（防误删）。

**关键约束**：owner 锁 TTL 必须 **≥ `AI_TOOL_TIMEOUT`**（spec §11，默认 30s）。设 60s 留余量。否则 execute_tool 实际跑了超过锁 TTL 时（如批量删 1000 用户慢操作），锁先过期 → 新 worker B 抢锁成功 → 双执行 race。`AI_HITL_OWNER_LOCK_TTL_SEC` 在 settings 里独立配置，部署文档明确「修改 `AI_TOOL_TIMEOUT` 时同步检查 owner 锁 TTL」。

```python
# worker B 接管前抢锁
lock_key = f"ai:hitl:owner:{confirmation_id}"
worker_token = secrets.token_urlsafe(16)  # 防其它 worker 误删

# NX + EX 60s（≥ AI_TOOL_TIMEOUT 30s + 余量）
ok = await redis.set(lock_key, worker_token, nx=True, ex=60)
if not ok:
    # worker A 还活着 / 另一个续传请求在跑
    raise BusinessRuleException(
        "HITL 续传进行中，请稍候重试",
        error_code="AI_RESUME_IN_PROGRESS"
    )

try:
    action = await hitl_manager.hang(confirmation_id)
    # hang 醒来后 execute_tool（worker_token 透传给 wake 校验，可选 v1.6+）
    result = await execute_tool(...)
finally:
    # Lua 脚本：只删自己的锁（防 token 不匹配误删）
    await redis.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1, lock_key, worker_token
    )
```

worker A 的 cancel 传播到 hang 后，A 的 execute_tool 还没开始的话会抛 CancelledError（不执行）；如果已经在 execute_tool 中，A 释放锁（finally） → B 抢锁成功。

worker A 的 cancel 还没传播时，A 持有锁 → B 抢锁失败 → 端点返回 409 → 前端 2s 后重试。

**为什么默认到位**：
- 架构正确性不应是"v1.6+ 优化"
- 开源项目留已知并发风险等于邀请用户踩坑
- Redis SETNX 是行业标准做法（与 `redis.lock` 等价但更轻量）

**反例**：(1) 不加锁 → 极端 race 下破坏性操作执行两次。(2) 进程内 `threading.Lock` → 多 worker 失效。(3) `wake` 端做 owner 校验 → 改动面太大，涉及 `/ai/confirm` 现有契约。

**回归**：每次续传抢 30s TTL 锁；execute_tool 完成 / hang 抛错时释放；token 匹配防误删；worker A 死透后锁自然释放（cancel 传播 → finally → release）。

### 2.4 可配置开关（`AI_SSE_RESUME_ENABLED`，默认开）

不是所有用户都需要续传（内网部署 90% 不需要移动端续传）。**功能默认开启但可关闭**：

```env
AI_SSE_RESUME_ENABLED=true   # 默认开
```

关闭时：
- 服务端 `confirmation_required` 事件**不发** `id:` 字段
- `GET /ai/chat/resume` 端点返回 410 Gone + `AI_RESUME_DISABLED`
- 前端 `EventSource.onerror` 维持 MVP 行为（提示"网络中断，请重新发起"）

**为什么默认开**：SSE `id:` 字段几乎零成本（每事件多几字节），是协议标准能力。关闭是给极端环境（如 Redis 内存紧张）的逃生口。

**反例**：默认关 → 大部分用户感受不到这个功能的存在，违反"开源默认正确"原则。

### 2.5 模式限制：仅 `redis_pubsub` 模式支持续传

`memory` 模式下进程内 `dict[confirmation_id, _PendingEntry]`，新 worker 重 hang 找不到 entry（`_pending.get(confirmation_id) is None`）→ 抛 `BusinessRuleException`。续传强制要求 `redis_pubsub` 模式。

| 模式 | 续传支持 | 端点行为 |
|---|---|---|
| `memory` | ❌ | 返回 410 + `stream_gone`（与 §8.3 wake 失败一致） |
| `redis_pubsub` | ✅ | 完整支持（需 `AI_SSE_RESUME_ENABLED=true`） |

部署文档明确：「要支持移动端 / 不稳定网络续传，必须用 `redis_pubsub` 模式 + 多 worker」。

### 2.6 TTL 处理：不重置 5min 全程，剩余 < 60s 拒绝续传

**不重置 TTL**：5min 从原始 `create_pending` 开始计时。续传不延长。

**剩余 < 60s 拒绝**：续传时 GET pending → 计算 `expires_at - now`：
- 剩余 ≥ 60s → 允许续传
- 剩余 < 60s → 返回 422 + `AI_RESUME_TTL_TOO_SHORT`（前端提示"确认窗口已临近超时，请重新发起"）

**为什么不重置**：
- 防滥用：攻击者反复续传保活 confirmation 无限延期
- 简化语义：5min 是契约，续传是"恢复"，不是"延期"

**反例**：(1) 续传时重置 5min → 攻击者可保活 confirmation 直到 Redis OOM。(2) 续传时 EXPIRE 改为剩余 50% → 复杂度高且语义模糊。

**回归**：剩余 < 60s 拒绝续传（用户体验稍紧但安全）。

---

## 3. 端点契约

### 3.1 `GET /ai/chat/resume`

```
GET /ai/chat/resume
Header:
  Authorization: Bearer <jwt>
  Last-Event-ID: <confirmation_id>          # SSE 标准协议
  Accept: text/event-stream
Query (optional, 调试后备):
  confirmation_id=<id>
```

**confirmation_id 解析优先级**：`Last-Event-ID` 头 > `?confirmation_id=` query param。两者都缺失 → 400 + `AI_RESUME_MISSING_ID`。两者都存在但值不同 → 以头为准（标准协议优先），不报错（向后兼容）。

**鉴权**：`Depends(get_current_user)`（与 `/ai/chat` 一致）。

**响应**（成功）：

```
HTTP/1.1 200 OK
Content-Type: text/event-stream

data: {"type":"confirmation_resumed","confirmationId":"abc","tool":"user.update_dept",...}
id: abc

data: {"type":"tool_call_result","toolCallId":"tc_xxx","ok":true,...}

data: {"type":"text-delta","delta":"已"}

data: {"type":"text-delta","delta":"将张三..."}

data: {"type":"done"}

```

**响应**（错误）：

| HTTP | errorCode | 触发条件 | 前端处理 |
|---|---|---|---|
| 400 | `AI_RESUME_MISSING_ID` | 既无 `Last-Event-ID` 头也无 query param | 提示"续传参数缺失"，停止重试 |
| 401 | (无) | 未登录 / token 过期 | 跳登录页 |
| 403 | `AI_RESUME_FORBIDDEN` | confirmation_id 不属于当前用户 | 提示"无权续传此对话"，停止重试 |
| 404 | `AI_RESUME_NOT_FOUND` | confirmation_id 不在 Redis（已 expired / 从未存在） | 提示"操作已超时，请重新发起" |
| 409 | `AI_RESUME_IN_PROGRESS` | 已有 worker 接管（锁竞争失败） | 静默 2s 后重试（最多 3 次） |
| 410 | `AI_RESUME_DISABLED` | 功能关闭 / `memory` 模式 | 退化为 MVP 行为（提示重新发起） |
| 410 | `AI_RESUME_ALREADY_RESOLVED` | pending.wake_action 已设（断流期间已被 wake） | 启动 §9.3 30s 轮询兜底（`GET /ai/operation-log?tool_call_id=...`）拿最终结果 |
| 422 | `AI_RESUME_TTL_TOO_SHORT` | 剩余 TTL < 60s | 提示"确认窗口已临近超时，请重新发起" |

### 3.2 `confirmation_required` 事件 schema 变更（向后兼容）

仅追加 SSE `id:` 字段（SSE 协议标准），事件 payload 不变：

```
data: {"type":"confirmation_required",...}    # payload 不变
id: <confirmation_id>                          # 新增：SSE 协议标准字段
```

前端 SSE 帧解析规则补一条：解析 `id:` 行 → 缓存到 `lastEventId`，断流重连时加到 `Last-Event-ID` 请求头。

---

## 4. 服务端实现要点

### 4.1 `app/modules/ai/agents/hitl/constants.py`

```python
AI_HITL_OWNER_LOCK_PREFIX = "ai:hitl:owner"
# owner 锁 TTL 必须 ≥ AI_TOOL_TIMEOUT（spec §11），否则 execute_tool 慢时
# 锁先过期 → 新 worker B 抢锁成功 → 双执行。设 60s 留余量（AI_TOOL_TIMEOUT
# 默认 30s + 网络抖动缓冲）。详见 spec §2.3 / §7.2 race 分析。
AI_HITL_OWNER_LOCK_TTL_SEC = 60
```

### 4.2 `app/modules/ai/api/chat.py` 改造（confirmation_required 加 id:）

`_format_sse_chunk` 扩展为接受可选 `event_id`：

```python
def _format_sse_chunk(event: AiStreamEvent, event_id: str | None = None) -> str:
    """把 AiStreamEvent 序列化为 SSE 帧，可选附带 id: 字段（SSE 协议标准）"""
    data_line = f"data: {event_to_sse_data(event)}"
    id_line = f"\nid: {event_id}" if event_id else ""
    return f"{data_line}{id_line}\n\n"
```

`ToolCallStartedEvent` / `ConfirmationRequiredEvent` emit 时透传 confirmation_id（前者已有 `tool_call_id` 字段，后者已有 `confirmation_id` 字段，复用）。

仅在 `AI_SSE_RESUME_ENABLED=true` 时加 `id:`。

### 4.3 新增 `app/modules/ai/api/resume.py`

```python
"""SSE 续传端点 — spec §3 v1.5+

GET /ai/chat/resume
- 读 Last-Event-ID 头作为 confirmation_id（SSE 标准协议）
- 校验 owner / TTL / 模式
- 抢 Redis owner 锁防双执行
- hang → wake → execute_tool → emit 结果
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query, Request
from pydantic_ai.ui import SSE_CONTENT_TYPE
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException, NotFoundException
from app.core.redis import redis_client
from app.db.session import get_db
from app.modules.ai.agents.gateway.executor import execute_tool
from app.modules.ai.agents.hitl.constants import (
    AI_HITL_OWNER_LOCK_PREFIX,
    AI_HITL_OWNER_LOCK_TTL_SEC,
    ConfirmAction,
)
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    ConfirmationResumedEvent,  # 新增
    DoneEvent,
    ToolCallResultEvent,
    _format_sse_chunk,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.service.chat_service import chat_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/ai/chat/resume", summary="SSE 流断流续传（HITL 期热接管）")
async def resume_chat(
    request: Request,
    confirmation_id_query: str | None = Query(
        default=None, alias="confirmation_id", description="调试后备（主推 Last-Event-ID 头）"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. 模式 / 功能开关
    # 错误码 → HTTP code 映射遵循 spec §9.6 模式：用 BusinessRuleException
    # 手动改 exc.code（默认 400）。AI_RESUME_DISABLED / AI_RESUME_ALREADY_RESOLVED
    # 是 410；AI_RESUME_TTL_TOO_SHORT 是 422；AI_RESUME_IN_PROGRESS 是 409；
    # AI_RESUME_MISSING_ID 是 400（默认）。
    if not settings.AI_SSE_RESUME_ENABLED:
        exc = BusinessRuleException(
            "SSE 续传功能未启用", error_code="AI_RESUME_DISABLED"
        )
        exc.code = 410
        raise exc

    if settings.AI_HITL_MODE != "redis_pubsub":
        exc = BusinessRuleException(
            "续传要求 redis_pubsub 模式", error_code="AI_RESUME_DISABLED"
        )
        exc.code = 410
        raise exc

    # 2. 取 confirmation_id（标准协议头优先）
    confirmation_id = request.headers.get("last-event-id") or confirmation_id_query
    if not confirmation_id:
        raise BusinessRuleException(
            "缺少 confirmation_id（Last-Event-ID 头或 query param）",
            error_code="AI_RESUME_MISSING_ID",
        )

    # 3. 取 Redis pending → 校验 owner + TTL
    pending = await hitl_manager.get_pending(redis_client, confirmation_id)
    if pending is None:
        raise NotFoundException(
            "HITL confirmation", error_code="AI_RESUME_NOT_FOUND"
        )
    if pending.user_id != current_user.user_id:
        raise AuthorizationException(
            error_code="AI_RESUME_FORBIDDEN"
        )
    if pending.wake_action is not None:
        # 已被 wake（用户在断流期间已点过确认）→ 返回 410 + AI_RESUME_ALREADY_RESOLVED
        # 前端启动 §9.3 30s 轮询兜底拿最终结果
        exc = BusinessRuleException(
            "HITL 已被处理（断流期间用户已确认/拒绝）",
            error_code="AI_RESUME_ALREADY_RESOLVED",
        )
        exc.code = 410
        raise exc

    # TTL 剩余校验
    ttl_sec = await redis_client.ttl(hitl_manager._redis_key(confirmation_id))
    if ttl_sec < 60:
        exc = BusinessRuleException(
            f"HITL 确认窗口剩余 {ttl_sec}s，已不足 60s",
            error_code="AI_RESUME_TTL_TOO_SHORT",
        )
        exc.code = 422
        raise exc

    # 4. 抢 owner 锁（防 worker A cancel 慢导致 worker B 双执行）
    import secrets  # noqa: PLC0415
    worker_token = secrets.token_urlsafe(16)
    lock_key = f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
    lock_ok = await redis_client.set(lock_key, worker_token, nx=True, ex=AI_HITL_OWNER_LOCK_TTL_SEC)
    if not lock_ok:
        exc = BusinessRuleException(
            "已有 worker 接管此 confirmation，请稍后重试",
            error_code="AI_RESUME_IN_PROGRESS",
        )
        exc.code = 409
        raise exc

    # 5. 构造 SSE 流（参考 chat.py 的 produce_pydantic 模式）
    async def resume_stream():
        try:
            # emit confirmation_resumed（前端重建抽屉）
            # 注意：confirmation_id 不在 PendingPayload 内（它是 Redis key 后缀），
            # 用 caller 传入的 confirmation_id 变量。
            resumed_event = ConfirmationResumedEvent(
                confirmation_id=confirmation_id,  # ← caller 传入的变量
                tool=pending.tool_name,
                tool_call_id=pending.tool_call_id,
                summary=build_summary(pending),
                args=pending.args,
                dry_run=pending.dry_run_result,
                expires_at=pending.expires_at,
                resumed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            yield _format_sse_chunk(resumed_event, event_id=confirmation_id)

            # hang 等 wake（redis_pubsub 模式，新 worker 接管）
            try:
                action = await hitl_manager.hang(confirmation_id)
            except TimeoutError:
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_HITL_TIMEOUT",
                        message="HITL 确认超时（5min 无人确认），请重新发起",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                return

            if action == ConfirmAction.REJECTED:
                yield _format_sse_chunk(
                    ToolCallResultEvent(
                        tool_call_id=pending.tool_call_id,
                        ok=False,
                        error_code="USER_REJECTED",
                        error_msg="用户已取消此操作",
                        # ... 其它字段
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                return

            # APPROVED → execute_tool
            result = await execute_tool(
                tool_name=pending.tool_name,
                args=pending.args,
                # ... 从 pending 重建 ctx（参考 §8.3 resume_confirmation）
                trace_id=pending.trace_id,
            )
            yield _format_sse_chunk(
                ToolCallResultEvent(
                    tool_call_id=pending.tool_call_id,
                    ok=result.ok,
                    # ...
                )
            )

            # execute_tool 后续 text-delta 由 LLM 接续（可选 v1.6+：是否再跑一轮 LLM）
            # MVP 简化：续传只到 tool_call_result + done，不接续 LLM 文本生成
            # 用户重连后看到 tool 结果即可（如需 LLM 解读，重新发消息）
            yield _format_sse_chunk(DoneEvent())

        finally:
            # 释放 owner 锁（Lua 防误删）
            await redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, lock_key, worker_token
            )

    return StreamingResponse(
        resume_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

### 4.4 `ConfirmationResumedEvent` 新增

`app/modules/ai/agents/hitl/events.py` 加：

```python
@dataclass(frozen=True)
class ConfirmationResumedEvent:
    """spec §2.2 v1.5+: SSE 断流续传成功，前端重建 HITL 抽屉"""
    type: Literal["confirmation_resumed"] = "confirmation_resumed"
    confirmation_id: str
    tool: str
    tool_call_id: str
    summary: str
    args: dict[str, Any]
    dry_run: dict[str, Any] | None = None
    expires_at: str
    resumed_at: str  # ISO 8601 UTC
```

### 4.5 配置项

`app/core/config.py` 加：

```python
AI_SSE_RESUME_ENABLED: bool = True
```

---

## 5. 前端实现要点

### 5.1 `aiStore` 状态扩展

```typescript
interface AiState {
  // ... 现有 ...
  pendingConfirmationId: string | null  // 当前 HITL 期的 confirmation_id
  pendingToolCallId: string | null      // 配对的 tool_call_id（清状态用，§5.2）
  resumeAttempts: number                // 续传重试次数（防死循环，上限 3）
}
```

### 5.2 SSE 帧解析扩展

**关键设计**：`tool_call_result` 事件 schema 不含 `confirmationId` 字段（spec §8.1 仅含 `toolCallId`），前端通过 `pendingConfirmationId ↔ toolCallId` 一对一映射清状态（HITL 场景每个 confirmation_id 唯一对应一个 tool_call_id）。

```typescript
interface AiState {
  // ... 现有 ...
  pendingConfirmationId: string | null       // 当前 HITL 期的 confirmation_id
  pendingToolCallId: string | null           // 配对的 tool_call_id（清状态用）
  resumeAttempts: number                     // 续传重试次数（防死循环）
}

// 现有解析逻辑：按 \n\n 切 SSE 帧
// 补：每帧解析 id: 行（如有），缓存到 lastEventId

function parseSseFrame(frame: string): { event: AiStreamEvent, eventId?: string } {
  const lines = frame.split('\n')
  let dataLine = ''
  let eventId: string | undefined
  for (const line of lines) {
    if (line.startsWith('data: ')) dataLine = line.slice(6)
    else if (line.startsWith('id: ')) eventId = line.slice(4)
  }
  // ...
}

// 收到 confirmation_required 时
function onConfirmationRequired(event) {
  aiStore.pendingConfirmationId = event.confirmationId
  aiStore.pendingToolCallId = event.toolCallId
  showConfirmationDrawer(event)
}

// 收到 confirmation_resumed 时
function onConfirmationResumed(event) {
  aiStore.pendingConfirmationId = event.confirmationId
  aiStore.pendingToolCallId = event.toolCallId
  showConfirmationDrawer(event)
  drawer.setReconnectedBadge(event.resumedAt)
}

// 收到 tool_call_result 时（用 toolCallId 反查清状态）
function onToolCallResult(event) {
  if (event.toolCallId === aiStore.pendingToolCallId) {
    aiStore.pendingConfirmationId = null
    aiStore.pendingToolCallId = null
  }
  // ...
}
```

### 5.3 断流检测 + 重连

```typescript
async function doStream(messages: Message[]) {
  const response = await fetch('/ai/chat', { method: 'POST', body: ... })

  // 监听流断
  response.body.on('end', () => {
    if (aiStore.pendingConfirmationId && aiStore.resumeAttempts < 3) {
      // 流断且处于 HITL 期 → 续传
      attemptResume(aiStore.pendingConfirmationId)
    }
  })
  response.body.on('error', () => {
    if (aiStore.pendingConfirmationId && aiStore.resumeAttempts < 3) {
      attemptResume(aiStore.pendingConfirmationId)
    } else {
      $message.error('网络中断，请重新发起')
    }
  })
}

async function attemptResume(confirmationId: string) {
  aiStore.resumeAttempts++
  try {
    const response = await fetch('/ai/chat/resume', {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Last-Event-ID': confirmationId,  // SSE 标准协议
        'Accept': 'text/event-stream',
      },
    })

    if (response.status === 409) {
      // AI_RESUME_IN_PROGRESS → 2s 后重试
      await sleep(2000)
      return attemptResume(confirmationId)
    }
    if (response.status === 410) {
      $message.error('续传功能未启用，请重新发起')
      return
    }
    if (response.status === 422) {
      $message.error('确认窗口已临近超时，请重新发起')
      return
    }
    // 200 → 复用 doStream 的 SSE 解析逻辑
    await consumeSseStream(response.body)
  } catch (e) {
    $message.error('续传失败，请重新发起')
  }
}
```

---

## 6. 测试矩阵

### 6.1 后端单测

`tests/modules/ai/test_resume.py`（新增）：

| 测试 | 验证点 |
|---|---|
| `test_resume_success_approved` | 抢锁 → hang → wake APPROVED → execute_tool → emit tool_call_result + done |
| `test_resume_success_rejected` | wake REJECTED → emit tool_call_result(ok=false, USER_REJECTED) + done |
| `test_resume_timeout` | hang 超时 → emit ai_error(AI_HITL_TIMEOUT) + done |
| `test_resume_disabled_feature` | `AI_SSE_RESUME_ENABLED=false` → 410 + AI_RESUME_DISABLED |
| `test_resume_disabled_memory_mode` | `AI_HITL_MODE=memory` → 410 + AI_RESUME_DISABLED |
| `test_resume_missing_id` | 无 Last-Event-ID 头 + 无 query param → 400 + AI_RESUME_MISSING_ID |
| `test_resume_not_found` | confirmation_id 不在 Redis → 404 + AI_RESUME_NOT_FOUND |
| `test_resume_forbidden` | pending.user_id ≠ current_user.user_id → 403 + AI_RESUME_FORBIDDEN |
| `test_resume_ttl_too_short` | TTL 剩余 < 60s → 422 + AI_RESUME_TTL_TOO_SHORT |
| `test_resume_in_progress` | owner 锁已被占 → 409 + AI_RESUME_IN_PROGRESS |
| `test_resume_already_resolved` | pending.wake_action 已设 → 410 + AI_RESUME_ALREADY_RESOLVED |
| `test_resume_last_event_id_header_preferred` | 同时设头和 query param → 用头 |
| `test_resume_query_param_fallback` | 只设 query param → 正常工作 |
| `test_owner_lock_released_on_success` | execute_tool 完成后锁被释放 |
| `test_owner_lock_released_on_error` | hang 抛错后锁被释放 |
| `test_owner_lock_token_mismatch_no_delete` | Lua 脚本防误删（token 不匹配不删） |
| `test_confirmation_required_has_id_field` | confirmation_required SSE 帧含 `id: <confirmation_id>` 行 |
| `test_confirmation_required_no_id_when_disabled` | `AI_SSE_RESUME_ENABLED=false` → confirmation_required 不含 id 字段 |

### 6.2 前端单测

| 测试 | 验证点 |
|---|---|
| `test_pendingConfirmationId_set_on_required` | 收到 confirmation_required → pendingConfirmationId 正确 |
| `test_pendingConfirmationId_clear_on_result` | 收到 tool_call_result → pendingConfirmationId 清空 |
| `test_resume_triggered_on_hang_error` | HITL 期断流 → 触发 attemptResume |
| `test_resume_not_triggered_on_normal_end` | 非 HITL 期断流 → 不触发续传 |
| `test_resume_attempts_limit` | 3 次重试失败后停止 + $message.error |
| `test_resume_409_retry_after_2s` | 409 → 2s 后重试 |

### 6.3 E2E（Playwright）

| 测试 | 验证点 |
|---|---|
| `test_hitl_refresh_page` | 弹抽屉后刷新页面 → 自动续传 → 抽屉重显 → 点确认 → 看到 tool 结果 |

---

## 7. 并发 race 详细分析

### 7.1 正常路径（无 race）

```
T0: worker A 流在 hang（await event.wait）
T1: 客户端断流
T2: FastAPI cancel sse_with_save 协程
T3: cancel 传播到 finally → cancel pydantic_task
T4: pydantic_task cancel 传播到 hang（asyncio.wait_for 抛 CancelledError）
T5: hang finally → unsubscribe + aclose
T6: tool 函数收到 CancelledError → unwind → executor 捕获 → ToolResult.failure（不会发出，因 stream 已断）

T7: 客户端重连到 worker B（GET /ai/chat/resume）
T8: worker B 抢 owner 锁（A 已释放 / 没有持锁）→ 成功
T9: worker B hang → 等 wake
T10: 用户点 confirm → wake → publish channel
T11: worker B 收到 message → 醒来 → execute_tool → emit 结果
```

### 7.2 极端 race（worker A cancel 慢）

```
T0: worker A 流在 hang
T1: 客户端断流
T2: 客户端立即重连到 worker B
T3: worker B 抢 owner 锁 → 但 worker A 还没释放（cancel 还没到 hang）
T4: worker B SETNX 失败 → 409 + AI_RESUME_IN_PROGRESS
T5: 客户端 2s 后重试
T6: 此时 worker A 的 cancel 早已传播 → A 释放锁
T7: worker B 抢锁成功 → hang → wake → execute_tool
```

**关键保障**：worker A 的 cancel 一定会传播到 hang（asyncio 协作式 cancel 在 await 点让出）。即使 cancel 慢，最多延迟 60s（owner 锁 TTL），不会双执行。owner 锁 TTL ≥ `AI_TOOL_TIMEOUT` 是硬约束（详见 §2.3 / SR-10 反例 5）。

### 7.3 已被 wake 的场景

```
T0: worker A 流在 hang
T1: 用户点 confirm（在断流前！）→ wake → SET pending.wake_action + PUBLISH
T2: worker A 的 hang 收到 message → 醒来 → execute_tool
T3: 此时客户端断流
T4: 客户端重连到 worker B
T5: worker B GET pending → pending.wake_action 已设 → 410 + AI_RESUME_ALREADY_RESOLVED
T6: 前端提示"操作已处理"，停止重试，等 worker A 的 execute_tool 结果（无法看到）
```

**问题**：worker A 在 T2 已经开始 execute_tool，但客户端在 T3 断流后看不到结果。

**MVP 简化**：客户端 410 后退化为 §8.3 / §9.3 轮询兜底（`GET /ai/operation-log?tool_call_id=...`）。前端在 410 后启动 30s 轮询，拿最终 tool 结果。

**v1.6+ 优化**：worker A 在 execute_tool 完成时把结果存到 Redis（key = `ai:hitl:result:<conf_id>`，TTL 60s），worker B 在 resume 时检查这个 key，有则直接 emit。

---

## 8. 范围外（PR-2 / v1.6+）

- **text-delta 持久化 + 续传**：只覆盖 HITL 期，text-delta 生成期断流不支持续传（用户重新发消息）
- **worker A cancel 慢的优化**：当前 owner 锁 TTL 30s 已够，不优化
- **execute_tool 结果缓存**：worker B 检查 Redis 缓存直接 emit（v1.6+）
- **跨设备续传**：用户在 A 设备发起 HITL 后在 B 设备续传（需要 session 多设备同步，v2+）
- **多模态断流**：图片 / 文件上传中断的断点续传（与 SSE 流不同协议，v2+）

---

## 9. 关键文件清单

**新建**：
- `app/modules/ai/api/resume.py` — GET /ai/chat/resume 端点
- `tests/modules/ai/test_resume.py` — 18 个单测
- `tests/modules/ai/test_resume_owner_lock.py` — owner 锁单测（可选独立文件）

**修改**：
- `app/modules/ai/agents/hitl/constants.py` — `AI_HITL_OWNER_LOCK_PREFIX` / `AI_HITL_OWNER_LOCK_TTL_SEC`
- `app/modules/ai/agents/hitl/events.py` — 新增 `ConfirmationResumedEvent`
- `app/modules/ai/api/chat.py` — `_format_sse_chunk` 支持 `event_id`，confirmation_required emit 时附带 id
- `app/core/config.py` — `AI_SSE_RESUME_ENABLED`
- `app/main.py` — 注册 `/ai/chat/resume` 路由
- `pyproject.toml` — 无新依赖（用现有 redis.asyncio）

**前端**：
- `src/store/modules/ai/index.ts` — `pendingConfirmationId` 状态 + `attemptResume` action
- `src/service/api/ai.ts` — `fetchResumeChat(confirmationId)` 函数
- `src/views/ai/chat/modules/chat-confirmation-drawer.vue` — `setReconnectedBadge` 方法

**spec 回写**：
- `docs/specs/2026-07-02-ai-tool-gateway-design.md` §8.5 改写（MVP 简化 → v1.5+ 已实现）
- `docs/specs/2026-07-02-ai-tool-gateway-design.md` §14 Roadmap：SSE sequence_id 断点续传 标记 ✅
- `docs/specs/2026-07-02-ai-tool-gateway-design.md` §22 加 SR-9 / SR-10
- `docs/AI-DEPLOYMENT.md` §10 监控接入补"续传依赖 redis_pubsub 模式"

---

## 10. 决策记录（写入 spec §22）

### SR-9. **SSE 续传用标准协议字段（`id:` + `Last-Event-ID` 头）而非私有 query param**（2026-07-13 v1.5+ 落地，spec §3）

`confirmation_required` SSE 事件附带 `id: <confirmation_id>` 字段（SSE 协议标准）；新端点 `GET /ai/chat/resume` 读 `Last-Event-ID` 请求头作为 confirmation_id；同时支持 `?confirmation_id=` query param 作为调试 / 非 EventSource 客户端的后备。

**反例**: 私有 query param（`?confirmation_id=xxx`）— 需要看项目 spec 才懂，外部贡献者门槛高；非标准客户端（如 iOS SDK）要手工拼接 URL；浏览器原生 `EventSource` 类无法自动重连。

**回归**: 端点同时支持 query param 作为调试后备（curl 一目了然），但 spec 主推 `Last-Event-ID` 头。前端 fetch 模式手工加 `Last-Event-ID` 头。

### SR-10. **SSE 续传并发安全默认到位（Redis SETNX owner 锁）**（2026-07-13 v1.5+ 落地，spec §4）

新 worker 接管前抢 Redis 锁 `ai:hitl:owner:<confirmation_id>`，**TTL 60s（≥ `AI_TOOL_TIMEOUT` 30s + 余量）**，token 匹配（Lua 脚本防误删）。锁竞争失败 → 409 + `AI_RESUME_IN_PROGRESS`，前端 2s 后重试。

**反例**: (1) 不加锁 → worker A cancel 慢时 worker B 双执行 tool，破坏性操作（如 `user.batch_delete`）可能删两次。(2) 进程内 `threading.Lock` → 多 worker 失效。(3) `wake` 端做 owner 校验 → 改动面太大，涉及 `/ai/confirm` 现有契约。(4) 留到 v1.6+ → 开源项目留已知并发风险等于邀请用户踩坑。(5) owner 锁 TTL < `AI_TOOL_TIMEOUT` → execute_tool 慢时锁先过期，B 抢锁双执行（设计漏洞，TTL 必须 ≥ tool 超时）。

**回归**: 每次 resume 抢 60s TTL 锁；execute_tool 完成 / hang 抛错时释放（Lua 脚本 token 校验）；worker A cancel 一定会传播到 hang（asyncio 协作式），最多延迟 60s 不会双执行；部署文档明确「修改 `AI_TOOL_TIMEOUT` 时同步检查 `AI_HITL_OWNER_LOCK_TTL_SEC`」。

### SR-11. **续传仅覆盖 HITL 期，不缓存流式 text-delta**（2026-07-13 v1.5+ 落地，spec §1.3）

sequence_id 只在 `confirmation_required` 事件上分配（SSE `id:` 字段）；text-delta / reasoning-delta / tool_call_started 等中间事件不分配 sequence，不缓存到 Redis。

**反例**: (1) 全 SSE 流事件缓存 → 高频 text-delta 写入 Redis 撑爆内存，去重 / 顺序逻辑复杂。(2) 缓存关键事件（tool_call_started 等） → 收益有限（这些事件跟着 confirmation_required 一起丢失），增加缓存复杂度。

**回归**: HITL 期是最长等待窗口（5min），断流概率最高，续传收益最大；流式生成期断流用户重新发消息即可（LLM 重跑成本可接受）。

### SR-12. **新增 `confirmation_resumed` 事件而非重放 `confirmation_required`**（2026-07-13 v1.5+ 落地，spec §2.2）

重连后服务端 emit `confirmation_resumed`（schema 兼容 `confirmation_required` + 新增 `resumedAt` 字段），前端区分首次 vs 重连，做差异化 UI（重连后抽屉显示"已重连"chip）。

**反例**: 重放 `confirmation_required` → 滥用事件语义（事件名说"要求确认"，重连后再发让读 spec 的人困惑）；前端要做"是否已在 pending 状态"去重逻辑；外部贡献者读 spec 困惑。

**回归**: 两事件 schema 兼容（共用字段），前端可统一渲染逻辑；仅 UI badge 差异。

---

## 11. Roadmap 回写（实现 ship 后执行）

`docs/specs/2026-07-02-ai-tool-gateway-design.md` §14 v1.5+ Roadmap 表更新（本 spec 进入实现阶段时同步）：

| 原 | 改 |
|---|---|
| ⚠️ SSE sequence_id 断点续传 \| 网络抖动频繁 \| 单调递增 sequence + `Last-Event-ID` 头重连 | ✅ **SSE 续传（HITL 期热接管） — 已完成 YYYY-MM-DD**（spec `2026-07-13-sse-resume-design.md` / SR-9 / SR-10 / SR-11 / SR-12）\| 网络抖动频繁 \| SSE 标准 `id:` 字段 + `Last-Event-ID` 头 + Redis SETNX owner 锁 + `confirmation_resumed` 新事件 |

> ship 时把 `YYYY-MM-DD` 替换为实际完成日期，并把本 spec 顶部 Status 从 `⚠️ Plan v1.5+` 改为 `✅ 已完成`。
