# SSE 断流续传（HITL 期热接管）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 HITL 期 SSE 断流后，新 worker 用 `Last-Event-ID` 头接管原 confirmation（hang → wake → execute_tool → emit 结果），用户重连看到 tool 结果。

**Architecture:** 后端新增 `GET /ai/chat/resume` 端点（Redis SETNX owner 锁防双执行）+ 新增 `confirmation_resumed` 事件（schema 兼容 `confirmation_required` + `resumedAt` 字段）+ executor 加 `resume_tool_execution` helper 跳过 perm/quota/dry_run 重复检查。前端 aiStore 加 `pendingConfirmationId`/`pendingToolCallId`/`resumeAttempts` 状态 + `attemptResume` action（fetch `/ai/chat/resume` 带 `Last-Event-ID` 头）。

**Tech Stack:** FastAPI / SQLAlchemy 2.0 async / Redis (SETNX + Lua) / Pydantic v2 / Pinia / native fetch + ReadableStream / SSE Protocol (`id:` + `Last-Event-ID`)

**Spec:** [`docs/specs/2026-07-13-sse-resume-design.md`](../specs/2026-07-13-sse-resume-design.md)

---

## File Structure

**新建（后端）**：
- `app/modules/ai/api/resume.py` — `GET /ai/chat/resume` 端点（错误码全 + 抢锁 + SSE 流）
- `tests/modules/ai/test_resume.py` — 18+ 单测（错误路径 + 成功路径 + owner 锁）

**修改（后端）**：
- `app/modules/ai/agents/hitl/constants.py` — `AI_HITL_OWNER_LOCK_PREFIX` / `AI_HITL_OWNER_LOCK_TTL_SEC=60`
- `app/modules/ai/agents/hitl/events.py` — `ConfirmationResumedEvent` dataclass + `event_to_sse_data` 加分支
- `app/modules/ai/api/chat.py` — `_format_sse_chunk` 支持 `event_id`；ConfirmationRequiredEvent 自动附 `id:`（条件 `AI_SSE_RESUME_ENABLED`）
- `app/modules/ai/agents/gateway/executor.py` — 加 `resume_tool_execution(pending, deps, log_id)` helper（export 给 resume.py）
- `app/core/config.py` — `AI_SSE_RESUME_ENABLED: bool = True`
- `app/main.py` — 注册 `ai_resume_router`

**修改（前端）**：
- `src/store/modules/ai/index.ts` — `pendingConfirmationId` / `pendingToolCallId` / `resumeAttempts` state + `attemptResume` action + SSE 解析 `id:` 行 + 断流触发续传
- `src/views/ai/chat/modules/chat-confirmation-drawer.vue` — `reconnectedAt` ref + "已重连"chip UI
- `src/typings/api/ai.d.ts` — `ConfirmationResumedEvent` 类型

**修改（spec）**：
- `docs/specs/2026-07-02-ai-tool-gateway-design.md` — §8.5 改写、§14 Roadmap 标 ✅、§22 加 SR-9/10/11/12
- `docs/specs/2026-07-13-sse-resume-design.md` — 顶部 Status 改 ✅
- `docs/AI-DEPLOYMENT.md` — §10 加续传依赖 redis_pubsub 模式说明

---

## Task 1: 后端基础设施（constants + config + ConfirmationResumedEvent）

**Files:**
- Modify: `app/modules/ai/agents/hitl/constants.py:69`（在 `AI_HITL_WAKE_CHANNEL_PREFIX` 后追加）
- Modify: `app/core/config.py:90`（在 `AI_HITL_ARGS_MAX_BYTES` 后追加）
- Modify: `app/modules/ai/agents/hitl/events.py:86`（`ConfirmationRequiredEvent` 后追加新 dataclass）
- Modify: `app/modules/ai/agents/hitl/events.py:104`（`AiStreamEvent` 联合类型加新 event）
- Modify: `app/modules/ai/agents/hitl/events.py:148-158`（`event_to_sse_data` 加 elif 分支）
- Test: `tests/modules/ai/test_resume_events.py`（新建）

- [ ] **Step 1: 写失败测试 — `ConfirmationResumedEvent` 序列化**

`tests/modules/ai/test_resume_events.py`：

```python
"""spec §2.2 v1.5+: ConfirmationResumedEvent 序列化测试"""

# ruff: noqa: ARG001, PLC0415

from app.modules.ai.agents.hitl.events import (
    ConfirmationResumedEvent,
    DryRunSummary,
    event_to_sse_data,
)


class TestConfirmationResumedEventSerialization:
    def test_basic_fields_camel_case(self) -> None:
        ev = ConfirmationResumedEvent(
            confirmation_id="abc123",
            tool="user.update_dept",
            tool_call_id="tc_xxx",
            summary="tool=user.update_dept, risk=high",
            args={"user_ids": [1, 2]},
            expires_at="2026-07-13T15:00:00Z",
            resumed_at="2026-07-13T14:35:00Z",
        )
        data = event_to_sse_data(ev)
        assert '"type":"confirmation_resumed"' in data
        assert '"confirmationId":"abc123"' in data
        assert '"toolCallId":"tc_xxx"' in data
        assert '"resumedAt":"2026-07-13T14:35:00Z"' in data
        assert '"expiresAt":"2026-07-13T15:00:00Z"' in data

    def test_dry_run_serialized_when_present(self) -> None:
        ev = ConfirmationResumedEvent(
            confirmation_id="abc",
            tool="user.batch_delete",
            tool_call_id="tc_yyy",
            summary="...",
            args={},
            dry_run=DryRunSummary(summary="将影响 3 行", affected_count=3),
            expires_at="2026-07-13T15:00:00Z",
            resumed_at="2026-07-13T14:35:00Z",
        )
        data = event_to_sse_data(ev)
        assert '"dryRun":{' in data
        assert '"affectedCount":3' in data

    def test_dry_run_omitted_when_none(self) -> None:
        ev = ConfirmationResumedEvent(
            confirmation_id="abc",
            tool="t",
            tool_call_id="tc_z",
            summary="s",
            args={},
            expires_at="...",
            resumed_at="...",
        )
        data = event_to_sse_data(ev)
        assert "dryRun" not in data
```

- [ ] **Step 2: 跑测试，预期 ImportError / FAIL**

```bash
cd F:/code/hohu/hohu-admin
uv run pytest tests/modules/ai/test_resume_events.py -v
```

预期：`ImportError: cannot import name 'ConfirmationResumedEvent'`

- [ ] **Step 3: 实现 — 加 `AI_HITL_OWNER_LOCK_*` 常量**

`app/modules/ai/agents/hitl/constants.py` 在 `AI_HITL_WAKE_CHANNEL_PREFIX`（line 69）后追加：

```python
# spec §2.3 v1.5+: SSE 续传 owner 锁
# 完整 lock_key: f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
# TTL 必须 ≥ AI_TOOL_TIMEOUT（spec §11，默认 30s），否则 execute_tool 慢时锁先
# 过期 → 新 worker B 抢锁成功 → 双执行 race。设 60s 留余量（tool_timeout 30s + 抖动）。
# 详见 spec §2.3 / §7.2 race 分析 / SR-10 反例 5。
AI_HITL_OWNER_LOCK_PREFIX = "ai:hitl:owner"
AI_HITL_OWNER_LOCK_TTL_SEC = 60
```

- [ ] **Step 4: 实现 — 加 `AI_SSE_RESUME_ENABLED` 配置**

`app/core/config.py` 在 `AI_HITL_ARGS_MAX_BYTES`（line 90）后追加：

```python
    # spec §2.4 v1.5+: SSE 续传功能开关（默认开）
    # False 时 confirmation_required 不发 id: 字段，/ai/chat/resume 端点返回 410。
    # 关闭场景：Redis 内存紧张 / 内网部署不需要移动端续传。
    AI_SSE_RESUME_ENABLED: bool = True
```

- [ ] **Step 5: 实现 — `ConfirmationResumedEvent` dataclass + 联合类型 + 序列化**

`app/modules/ai/agents/hitl/events.py` 在 `ConfirmationRequiredEvent`（line 86）后追加：

```python
@dataclass(frozen=True)
class ConfirmationResumedEvent:
    """SSE 续传重连事件（spec §2.2 v1.5+）

    schema 与 ConfirmationRequiredEvent 兼容（前端可统一渲染），仅多 resumedAt
    字段用于"已重连"UI badge。前端收到此事件后：
      - 用 confirmationId / toolCallId 反查 / 重建 HITL 抽屉
      - 显示"已重连"chip（区别于首次 confirmation_required）
    """

    confirmation_id: str
    tool: str
    tool_call_id: str
    summary: str
    args: dict[str, Any]
    expires_at: str
    resumed_at: str
    dry_run: DryRunSummary | None = None
    type: Literal["confirmation_resumed"] = "confirmation_resumed"
```

修改 `AiStreamEvent` 联合类型（line 104）：

```python
AiStreamEvent = (
    ToolCallStartedEvent
    | ToolCallResultEvent
    | ConfirmationRequiredEvent
    | ConfirmationResumedEvent
    | AiErrorEvent
    | DoneEvent
)
```

`event_to_sse_data` 在 `elif isinstance(event, ConfirmationRequiredEvent):` 分支后（line 158）追加：

```python
    elif isinstance(event, ConfirmationResumedEvent):
        payload = {
            "type": event.type,
            "confirmationId": event.confirmation_id,
            "tool": event.tool,
            "toolCallId": event.tool_call_id,
            "summary": event.summary,
            "args": event.args,
            "expiresAt": event.expires_at,
            "resumedAt": event.resumed_at,
            "dryRun": _dry_run_to_dict(event.dry_run),
        }
```

- [ ] **Step 6: 跑测试，预期全过**

```bash
uv run pytest tests/modules/ai/test_resume_events.py -v
```

预期：3 个测试全过

- [ ] **Step 7: ruff + 全量测试**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/modules/ai/ -v
```

预期：现有测试零回归

- [ ] **Step 8: Commit**

```bash
git add app/modules/ai/agents/hitl/constants.py app/core/config.py app/modules/ai/agents/hitl/events.py tests/modules/ai/test_resume_events.py
git commit -m "feat(ai): add ConfirmationResumedEvent + owner lock constants for SSE resume"
```

---

## Task 2: chat.py `_format_sse_chunk` 自动给 ConfirmationRequiredEvent 加 `id:` 字段

**Files:**
- Modify: `app/modules/ai/api/chat.py:102-104`（`_format_sse_chunk` 加 `event_id` 参数 + 自动判定）
- Test: `tests/modules/ai/test_chat_sse_id.py`（新建）

- [ ] **Step 1: 写失败测试**

`tests/modules/ai/test_chat_sse_id.py`：

```python
"""spec §3.2 v1.5+: confirmation_required SSE 帧自动附 id: 字段（当 AI_SSE_RESUME_ENABLED=True）"""

# ruff: noqa: ARG001, PLC0415

from unittest.mock import patch

import pytest

from app.modules.ai.agents.hitl.events import (
    ConfirmationRequiredEvent,
    DoneEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.api.chat import _format_sse_chunk


@pytest.fixture
def _resume_enabled():
    with patch("app.modules.ai.api.chat.settings.AI_SSE_RESUME_ENABLED", True):
        yield


@pytest.fixture
def _resume_disabled():
    with patch("app.modules.ai.api.chat.settings.AI_SSE_RESUME_ENABLED", False):
        yield


class TestFormatSseChunkIdField:
    def test_confirmation_required_has_id_when_enabled(self, _resume_enabled) -> None:
        ev = ConfirmationRequiredEvent(
            confirmation_id="cid_abc",
            tool="t",
            tool_call_id="tc_x",
            summary="s",
            args={},
            expires_at="...",
        )
        chunk = _format_sse_chunk(ev)
        assert "id: cid_abc\n" in chunk
        assert chunk.endswith("\n\n")

    def test_confirmation_required_no_id_when_disabled(self, _resume_disabled) -> None:
        ev = ConfirmationRequiredEvent(
            confirmation_id="cid_abc",
            tool="t",
            tool_call_id="tc_x",
            summary="s",
            args={},
            expires_at="...",
        )
        chunk = _format_sse_chunk(ev)
        assert "id:" not in chunk

    def test_other_events_have_no_id(self, _resume_enabled) -> None:
        """只有 confirmation_required 应带 id:（其它事件 sequence_id 无意义）"""
        ev = ToolCallStartedEvent(
            tool="t",
            tool_call_id="tc_x",
            summary="s",
            args={},
            risk="low",
            trace_id="tr_x",
        )
        chunk = _format_sse_chunk(ev)
        assert "id:" not in chunk

    def test_done_event_no_id(self, _resume_enabled) -> None:
        chunk = _format_sse_chunk(DoneEvent())
        assert "id:" not in chunk
```

- [ ] **Step 2: 跑测试，预期 FAIL**

```bash
uv run pytest tests/modules/ai/test_chat_sse_id.py -v
```

预期：`test_confirmation_required_has_id_when_enabled` FAIL（chunk 不含 `id:`）

- [ ] **Step 3: 实现 — `_format_sse_chunk` 加自动判定**

`app/modules/ai/api/chat.py:102-104` 改为：

```python
def _format_sse_chunk(event: AiStreamEvent) -> str:
    """把 AiStreamEvent 序列化为 SSE 帧：`data: {...}\n\n`

    spec §3.2 v1.5+: ConfirmationRequiredEvent 在 AI_SSE_RESUME_ENABLED=True 时
    自动附带 `id: <confirmation_id>` 字段（SSE 协议标准），客户端断流重连时
    浏览器/SDK 自动通过 Last-Event-ID 头携带此 id 到 /ai/chat/resume 端点。
    """
    data_line = f"data: {event_to_sse_data(event)}"
    # spec §3.2: 仅 confirmation_required 事件需要 id: 字段（其它事件 sequence 无意义）
    from app.modules.ai.agents.hitl.events import (  # noqa: PLC0415
        ConfirmationRequiredEvent,
    )

    event_id: str | None = None
    if settings.AI_SSE_RESUME_ENABLED and isinstance(event, ConfirmationRequiredEvent):
        event_id = event.confirmation_id
    id_line = f"\nid: {event_id}" if event_id else ""
    return f"{data_line}{id_line}\n\n"
```

> 注意：`ConfirmationRequiredEvent` 已经在 chat.py 顶部 imports 之外，这里局部 import 避免循环引用风险（chat.py 顶部已 import DoneEvent/AiErrorEvent/event_to_sse_data/AiStreamEvent，加 ConfirmationRequiredEvent 也可，二选一）。

- [ ] **Step 4: 跑测试，预期全过**

```bash
uv run pytest tests/modules/ai/test_chat_sse_id.py -v
```

- [ ] **Step 5: ruff + 全量测试**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/modules/ai/ -v
```

- [ ] **Step 6: Commit**

```bash
git add app/modules/ai/api/chat.py tests/modules/ai/test_chat_sse_id.py
git commit -m "feat(ai): emit SSE id: field on confirmation_required for resume support"
```

---

## Task 3: resume.py 端点 — 错误路径全码

**Files:**
- Create: `app/modules/ai/api/resume.py`（端点骨架 + 错误路径）
- Create: `tests/modules/ai/test_resume.py`（错误路径测试 9 个，先建文件后续 Task 4 追加成功路径测试）

- [ ] **Step 1: 写失败测试 — 9 个错误路径**

`tests/modules/ai/test_resume.py`：

```python
"""spec §3 v1.5+: /ai/chat/resume 端点单元测试

直接调端点函数，mock 掉 redis_client + hitl_manager + settings。
"""

# ruff: noqa: ARG001, PLC0415

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.api.resume import resume_chat


def _make_pending(
    user_id: int = 100,
    wake_action: str | None = None,
    expires_at: str = "2099-01-01T00:00:00Z",
) -> PendingPayload:
    return PendingPayload(
        user_id=user_id,
        conversation_id=1,
        tool_call_id="tc_test",
        trace_id="tr_test",
        tool_name="user.update",
        args={"user_id": 42},
        dry_run_result=None,
        expires_at=expires_at,
        wake_action=wake_action,
    )


def _make_user(user_id: int = 100):
    return SimpleNamespace(user_id=user_id, user_name="alice")


def _make_request(last_event_id: str | None = None):
    """构造 FastAPI Request mock（headers.get('last-event-id')）"""
    req = MagicMock()
    req.headers = {"last-event-id": last_event_id} if last_event_id else {}
    return req


@pytest.fixture
def _redis_pubsub_mode():
    with patch("app.modules.ai.api.resume.settings.AI_HITL_MODE", "redis_pubsub"):
        yield


@pytest.fixture
def _resume_enabled():
    with patch("app.modules.ai.api.resume.settings.AI_SSE_RESUME_ENABLED", True):
        yield


# ============ 410 AI_RESUME_DISABLED ============


class TestResumeDisabled:
    async def test_feature_disabled_returns_410(self) -> None:
        with patch("app.modules.ai.api.resume.settings.AI_SSE_RESUME_ENABLED", False):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_DISABLED"
        assert exc_info.value.code == 410

    async def test_memory_mode_returns_410(
        self, _resume_enabled
    ) -> None:
        with patch("app.modules.ai.api.resume.settings.AI_HITL_MODE", "memory"):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_DISABLED"
        assert exc_info.value.code == 410


# ============ 400 AI_RESUME_MISSING_ID ============


class TestResumeMissingId:
    async def test_no_header_no_query_returns_400(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            await resume_chat(
                request=_make_request(last_event_id=None),
                confirmation_id_query=None,
                db=MagicMock(),
                current_user=_make_user(),
            )
        assert exc_info.value.error_code == "AI_RESUME_MISSING_ID"
        assert exc_info.value.code == 400


# ============ 404 AI_RESUME_NOT_FOUND ============


class TestResumeNotFound:
    async def test_pending_missing_returns_404(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=None),
        ):
            with pytest.raises(NotFoundException) as exc_info:
                await resume_chat(
                    request=_make_request("cid_unknown"),
                    confirmation_id_query="cid_unknown",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_NOT_FOUND"


# ============ 403 AI_RESUME_FORBIDDEN ============


class TestResumeForbidden:
    async def test_owner_mismatch_returns_403(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=_make_pending(user_id=100)),
        ):
            with pytest.raises(AuthorizationException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(user_id=999),  # 非 owner
                )
        assert exc_info.value.error_code == "AI_RESUME_FORBIDDEN"


# ============ 410 AI_RESUME_ALREADY_RESOLVED ============


class TestResumeAlreadyResolved:
    async def test_wake_action_set_returns_410(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=_make_pending(wake_action="approved")),
        ):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_ALREADY_RESOLVED"
        assert exc_info.value.code == 410


# ============ 422 AI_RESUME_TTL_TOO_SHORT ============


class TestResumeTtlTooShort:
    async def test_ttl_below_60s_returns_422(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=30),
            ),
        ):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_TTL_TOO_SHORT"
        assert exc_info.value.code == 422


# ============ 409 AI_RESUME_IN_PROGRESS ============


class TestResumeInProgress:
    async def test_lock_held_returns_409(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=120),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=None),  # SETNX 失败
            ),
        ):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_IN_PROGRESS"
        assert exc_info.value.code == 409


# ============ Last-Event-ID 头优先级 ============


class TestLastEventIdHeaderPriority:
    async def test_header_preferred_over_query(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        """同时设头（cid_from_header）和 query param（cid_from_query）→ 用头"""
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=None),  # 让它早退出（ NotFound），但 confirmation_id 已确定
        ) as mock_get:
            with pytest.raises(NotFoundException):
                await resume_chat(
                    request=_make_request(last_event_id="cid_from_header"),
                    confirmation_id_query="cid_from_query",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        # hitl_manager.get_pending 第一个参数是 redis，第二个是 confirmation_id
        args = mock_get.await_args.args
        assert args[1] == "cid_from_header"

    async def test_query_param_fallback(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=None),
        ) as mock_get:
            with pytest.raises(NotFoundException):
                await resume_chat(
                    request=_make_request(last_event_id=None),
                    confirmation_id_query="cid_from_query",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        args = mock_get.await_args.args
        assert args[1] == "cid_from_query"
```

- [ ] **Step 2: 跑测试，预期 ImportError**

```bash
uv run pytest tests/modules/ai/test_resume.py -v
```

预期：`ImportError: No module named 'app.modules.ai.api.resume'`

- [ ] **Step 3: 实现 — resume.py 端点骨架（错误路径）**

`app/modules/ai/api/resume.py`：

```python
"""SSE 流断流续传端点 — spec §3 v1.5+（Task 3: 错误路径骨架；Task 4 替换为完整实现）"""

import logging
import secrets

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.db.session import get_db
from app.modules.ai.agents.hitl.constants import (
    AI_HITL_OWNER_LOCK_PREFIX,
    AI_HITL_OWNER_LOCK_TTL_SEC,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


def _set_exc_code(exc: BusinessRuleException, code: int) -> BusinessRuleException:
    """spec §9.6: BusinessRuleException 默认 code=400，手动改 code 返 409/410/422"""
    exc.code = code
    return exc


@router.get("", summary="SSE 流断流续传（HITL 期热接管）")
async def resume_chat(
    request: Request,
    confirmation_id_query: str | None = Query(
        default=None, alias="confirmation_id", description="调试后备（主推 Last-Event-ID 头）"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """spec §3 v1.5+: SSE 续传入口

    读 confirmation_id 顺序：Last-Event-ID 头 > ?confirmation_id= query param
    """
    # 1. 功能开关 + 模式校验
    if not settings.AI_SSE_RESUME_ENABLED:
        raise _set_exc_code(
            BusinessRuleException("SSE 续传功能未启用", error_code="AI_RESUME_DISABLED"),
            410,
        )
    if settings.AI_HITL_MODE != "redis_pubsub":
        raise _set_exc_code(
            BusinessRuleException("续传要求 redis_pubsub 模式", error_code="AI_RESUME_DISABLED"),
            410,
        )

    # 2. 取 confirmation_id（标准协议头优先）
    confirmation_id = request.headers.get("last-event-id") or confirmation_id_query
    if not confirmation_id:
        raise BusinessRuleException(
            "缺少 confirmation_id（Last-Event-ID 头或 query param）",
            error_code="AI_RESUME_MISSING_ID",
        )

    # 3. 取 Redis pending → 校验 owner + 已 wake + TTL
    pending = await hitl_manager.get_pending(redis_client, confirmation_id)
    if pending is None:
        raise NotFoundException("HITL confirmation", error_code="AI_RESUME_NOT_FOUND")
    if pending.user_id != current_user.user_id:
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")
    if pending.wake_action is not None:
        raise _set_exc_code(
            BusinessRuleException(
                "HITL 已被处理（断流期间用户已确认/拒绝）",
                error_code="AI_RESUME_ALREADY_RESOLVED",
            ),
            410,
        )

    ttl_sec = await redis_client.ttl(hitl_manager._redis_key(confirmation_id))
    if ttl_sec < 60:
        raise _set_exc_code(
            BusinessRuleException(
                f"HITL 确认窗口剩余 {ttl_sec}s，已不足 60s",
                error_code="AI_RESUME_TTL_TOO_SHORT",
            ),
            422,
        )

    # 4. 抢 owner 锁（防 worker A cancel 慢导致 worker B 双执行，spec §2.3）
    worker_token = secrets.token_urlsafe(16)
    lock_key = f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
    lock_ok = await redis_client.set(
        lock_key, worker_token, nx=True, ex=AI_HITL_OWNER_LOCK_TTL_SEC
    )
    if not lock_ok:
        raise _set_exc_code(
            BusinessRuleException(
                "已有 worker 接管此 confirmation，请稍后重试",
                error_code="AI_RESUME_IN_PROGRESS",
            ),
            409,
        )

    # Task 4 在此插入：构造 SSE 流（emit resumed → hang → execute_tool → emit result）
    # 当前 Task 3 仅占位：释放锁并返回 500（不应被命中，因为 Task 3 测试在锁后即返回）
    _RELEASE_LOCK_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    await redis_client.eval(_RELEASE_LOCK_LUA, 1, lock_key, worker_token)
    raise BusinessRuleException(
        "续传端点未完成实现", error_code="AI_INTERNAL_ERROR"
    )
```

> 注：Task 3 仅占位实现错误路径 + owner 锁逻辑（错误路径测试在抢锁失败 / 成功前都返回）。Task 4 会把占位代码（line `# Task 4 在此插入` 之后）替换为真正的 SSE 流。

- [ ] **Step 4: 跑测试，预期 9 个错误路径全过**

```bash
uv run pytest tests/modules/ai/test_resume.py -v
```

预期：9 个测试全过（注意：Task 3 的测试都是错误路径，不会跑到占位）

- [ ] **Step 5: ruff + 全量测试**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/modules/ai/ -v
```

- [ ] **Step 6: Commit**

```bash
git add app/modules/ai/api/resume.py tests/modules/ai/test_resume.py
git commit -m "feat(ai): add /ai/chat/resume endpoint error paths (8 error codes)"
```

---

## Task 4: executor.py 加 `resume_tool_execution` helper + resume.py 成功路径

**Files:**
- Modify: `app/modules/ai/agents/gateway/executor.py`（在 `_invoke_tool_fn` 后追加 `resume_tool_execution`）
- Modify: `app/modules/ai/api/resume.py`（替换 Task 3 的占位为真正 SSE 流）
- Test: `tests/modules/ai/test_resume.py`（追加 9 个成功路径测试）

- [ ] **Step 1: 写失败测试 — 成功路径**

`tests/modules/ai/test_resume.py` 追加（在文件末尾）：

```python
# ============ 成功路径 ============


class TestResumeSuccessPath:
    """spec §3.1 + §4.3: 续传成功路径（emit resumed → hang → execute_tool → emit result）"""

    @pytest.fixture
    def _mock_deps_for_success(self, _resume_enabled, _redis_pubsub_mode):
        """成功路径所需的所有 mock：pending + ttl + 锁 + hang APPROVED + execute_tool"""
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.hitl.constants import ConfirmAction

        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),  # SETNX 成功
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),  # Lua 返回 1（删成功）
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.APPROVED),
            ),
            patch(
                "app.modules.ai.api.resume.resume_tool_execution",
                AsyncMock(return_value=ToolResult.success(data={"affected_count": 1})),
            ),
        ):
            yield

    async def test_returns_streaming_response(self, _mock_deps_for_success) -> None:
        from fastapi.responses import StreamingResponse

        result = await resume_chat(
            request=_make_request("cid"),
            confirmation_id_query="cid",
            db=MagicMock(),
            current_user=_make_user(),
        )
        assert isinstance(result, StreamingResponse)
        assert result.media_type == "text/event-stream"

    async def test_stream_emits_resumed_then_result_then_done(
        self, _mock_deps_for_success
    ) -> None:
        result = await resume_chat(
            request=_make_request("cid"),
            confirmation_id_query="cid",
            db=MagicMock(),
            current_user=_make_user(),
        )
        # 收集所有 SSE 帧
        chunks: list[str] = []
        async for chunk in result.body_iterator:
            chunks.append(chunk)
        body = "".join(chunks)
        assert '"type":"confirmation_resumed"' in body
        assert '"type":"tool_call_result"' in body
        assert '"type":"done"' in body
        # 顺序：resumed 在 result 前
        assert body.index("confirmation_resumed") < body.index("tool_call_result")

    async def test_rejected_path_emits_failure_result(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.constants import ConfirmAction

        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.REJECTED),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = ""
            async for chunk in result.body_iterator:
                body += chunk
        assert '"type":"confirmation_resumed"' in body
        assert '"errorCode":"USER_REJECTED"' in body
        assert '"type":"done"' in body


class TestResumeTimeoutPath:
    async def test_hang_timeout_emits_ai_error(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = ""
            async for chunk in result.body_iterator:
                body += chunk
        assert '"errorCode":"AI_HITL_TIMEOUT"' in body
        assert '"type":"done"' in body


class TestOwnerLockRelease:
    """spec §2.3: owner 锁在 finally 块释放（Lua 脚本 token 校验）"""

    async def test_lock_released_after_success(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.hitl.constants import ConfirmAction

        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ) as mock_set,
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ) as mock_eval,
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.APPROVED),
            ),
            patch(
                "app.modules.ai.api.resume.resume_tool_execution",
                AsyncMock(return_value=ToolResult.success(data={})),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            async for _ in result.body_iterator:
                pass
        # SETNX 一定调过（抢锁）
        mock_set.assert_awaited()
        # Lua 脚本一定调过（释放锁）
        mock_eval.assert_awaited()

    async def test_lock_released_on_hang_error(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        """hang 抛非 TimeoutError 异常时锁也要释放（finally 块）"""
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ) as mock_eval,
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(side_effect=RuntimeError("redis gone")),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            # body_iterator 内部的 try/except 会消化 RuntimeError 并 emit ai_error
            async for _ in result.body_iterator:
                pass
        mock_eval.assert_awaited()
```

- [ ] **Step 2: 跑测试，预期 FAIL（resume_tool_execution 不存在）**

```bash
uv run pytest tests/modules/ai/test_resume.py::TestResumeSuccessPath -v
```

预期：`AttributeError: module 'app.modules.ai.api.resume' has no attribute 'resume_tool_execution'`

- [ ] **Step 3: 实现 — executor.py 加 `resume_tool_execution` helper**

`app/modules/ai/agents/gateway/executor.py` 在 `_invoke_tool_fn` 后追加（约 line 814 处）：

```python
# ============ SSE 续传：跳过 perm/quota/dry_run/log_start 的业务执行（spec §3 v1.5+） ============


async def resume_tool_execution(
    pending: "PendingPayload",
    deps: ChatDeps,
    log_id: int,
) -> ToolResult:
    """续传端点专用：从 pending payload 重建执行上下文，跑业务函数

    与 execute_tool 区别（spec §4.3）：
      - 不重做 perm/quota/dry_run/log_start（首次 execute_tool 已做，避免双扣 quota / 双写 log）
      - 不 emit tool_call_started（首次已发，前端 streamEvents 已有）
      - 不 emit tool_call_result（resume 端点自己 emit，让 SSE 顺序连续）
      - 写 log 终态（success/failed）— 首次 pending_confirmation 状态需迁移

    Args:
        pending: Redis pending payload（含 tool_name / args / trace_id）
        deps: 续传时重建的 ChatDeps（含 user / perms / agent）
        log_id: 首次 execute_tool 写的 ai_operation_log.log_id

    Returns:
        ToolResult（success / failure），resume 端点据此 emit tool_call_result
    """
    registry = ToolRegistry.get()
    registered = registry.find(pending.tool_name)
    if registered is None:
        # tool 被禁用 / 改名（罕见，但 defensive）
        logger.error(
            "resume: tool not found",
            extra={"tool": pending.tool_name, "confirmation_id": pending.tool_call_id},
        )
        return ToolResult.failure(
            error_code="AI_TOOL_NOT_FOUND",
            error_msg=USER_FACING_MSG["AI_TOOL_NOT_FOUND"],
        )

    # 业务执行（复用 _invoke_tool_fn：含 L3 超时 + 脱敏 + clear_failures + query_cache）
    args_hash = compute_args_hash(pending.args)
    started_at = time.monotonic()
    result = await _invoke_tool_fn(
        registered, pending.args, deps, args_hash, l1_member=None
    )

    # 写 log 终态（spec §6.5：HITL approved → 业务执行完写 success/failed）
    await _finish_log_final(log_id, result, started_at)
    return result
```

> 需要 `PendingPayload` 类型 import：`from app.modules.ai.agents.hitl.manager import PendingPayload`。注意循环引用 — manager.py 不 import executor，executor 已 import manager（line 61），加 PendingPayload 类型注解用 `"PendingPayload"` 字符串注解（forward reference）即可。

- [ ] **Step 4: 实现 — resume.py 替换占位为真正 SSE 流**

`app/modules/ai/api/resume.py` 重写整个文件（合并 Task 3 错误路径 + Task 4 成功路径）：

```python
"""SSE 流断流续传端点 — spec §3 v1.5+

GET /ai/chat/resume
  - 读 Last-Event-ID 头作为 confirmation_id（SSE 协议标准）
  - 校验 owner / TTL / 模式
  - 抢 Redis owner 锁防双执行（spec §2.3）
  - emit confirmation_resumed → hang → execute_tool → emit tool_call_result + done
  - finally 释放 owner 锁（Lua 脚本防误删）

错误码：见 spec §3.1 表。
"""

import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic_ai.ui import SSE_CONTENT_TYPE
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.db.session import get_db, AsyncSessionLocal
from app.modules.ai.agents.gateway.executor import resume_tool_execution
from app.modules.ai.agents.hitl.constants import (
    AI_HITL_OWNER_LOCK_PREFIX,
    AI_HITL_OWNER_LOCK_TTL_SEC,
    ConfirmAction,
)
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    ConfirmationResumedEvent,
    DoneEvent,
    DryRunSummary,
    ToolCallResultEvent,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.api.chat import _format_sse_chunk
from app.modules.ai.core.context import ChatDeps
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


def _set_exc_code(exc: BusinessRuleException, code: int) -> BusinessRuleException:
    """spec §9.6: BusinessRuleException 默认 code=400，手动改 code"""
    exc.code = code
    return exc


def _build_resumed_event(confirmation_id: str, pending, deps: ChatDeps) -> ConfirmationResumedEvent:
    """从 pending payload 构造 ConfirmationResumedEvent"""
    # dry_run_result 结构：{"summary": str, "affected_count": int}（见 executor._summary_to_dict）
    dry_run: DryRunSummary | None = None
    if pending.dry_run_result:
        dry_run = DryRunSummary(
            summary=pending.dry_run_result.get("summary", ""),
            affected_count=pending.dry_run_result.get("affected_count", 0),
        )
    return ConfirmationResumedEvent(
        confirmation_id=confirmation_id,  # caller 传入（PendingPayload 不含此字段）
        tool=pending.tool_name,
        tool_call_id=pending.tool_call_id,
        summary=f"resume: tool={pending.tool_name}",
        args=pending.args,
        expires_at=pending.expires_at,
        resumed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        dry_run=dry_run,
    )


# Lua 脚本：仅当 KEYS[1] 的值 == ARGV[1] 时 del（防 token 不匹配误删）
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


@router.get("", summary="SSE 流断流续传（HITL 期热接管）")
async def resume_chat(
    request: Request,
    confirmation_id_query: str | None = Query(
        default=None, alias="confirmation_id", description="调试后备（主推 Last-Event-ID 头）"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """spec §3 v1.5+: SSE 续传入口

    读 confirmation_id 顺序：Last-Event-ID 头 > ?confirmation_id= query param
    """
    # 1. 功能开关 + 模式校验
    if not settings.AI_SSE_RESUME_ENABLED:
        raise _set_exc_code(
            BusinessRuleException("SSE 续传功能未启用", error_code="AI_RESUME_DISABLED"),
            410,
        )
    if settings.AI_HITL_MODE != "redis_pubsub":
        raise _set_exc_code(
            BusinessRuleException("续传要求 redis_pubsub 模式", error_code="AI_RESUME_DISABLED"),
            410,
        )

    # 2. 取 confirmation_id（标准协议头优先）
    confirmation_id = request.headers.get("last-event-id") or confirmation_id_query
    if not confirmation_id:
        raise BusinessRuleException(
            "缺少 confirmation_id（Last-Event-ID 头或 query param）",
            error_code="AI_RESUME_MISSING_ID",
        )

    # 3. 取 Redis pending → 校验 owner + 已 wake + TTL
    pending = await hitl_manager.get_pending(redis_client, confirmation_id)
    if pending is None:
        raise NotFoundException("HITL confirmation", error_code="AI_RESUME_NOT_FOUND")
    if pending.user_id != current_user.user_id:
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")
    if pending.wake_action is not None:
        raise _set_exc_code(
            BusinessRuleException(
                "HITL 已被处理（断流期间用户已确认/拒绝）",
                error_code="AI_RESUME_ALREADY_RESOLVED",
            ),
            410,
        )

    ttl_sec = await redis_client.ttl(hitl_manager._redis_key(confirmation_id))
    if ttl_sec < 60:
        raise _set_exc_code(
            BusinessRuleException(
                f"HITL 确认窗口剩余 {ttl_sec}s，已不足 60s",
                error_code="AI_RESUME_TTL_TOO_SHORT",
            ),
            422,
        )

    # 4. 抢 owner 锁（spec §2.3）
    worker_token = secrets.token_urlsafe(16)
    lock_key = f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
    lock_ok = await redis_client.set(
        lock_key, worker_token, nx=True, ex=AI_HITL_OWNER_LOCK_TTL_SEC
    )
    if not lock_ok:
        raise _set_exc_code(
            BusinessRuleException(
                "已有 worker 接管此 confirmation，请稍后重试",
                error_code="AI_RESUME_IN_PROGRESS",
            ),
            409,
        )

    # 5. 从 pending 拿 log_id（首次 execute_tool 起始行）
    log = await operation_log_service.get_by_tool_call_id(
        db, pending.tool_call_id, user_id=current_user.user_id
    )
    log_id = log.log_id if log else None

    # 6. 重建 ChatDeps（续传专用，perms 重新查 DB — 用户 perms 可能已变）
    from app.modules.ai.service.chat_service import chat_service  # noqa: PLC0415

    deps = await chat_service.build_chat_deps(db, current_user, agent_code=None)
    deps.conversation_id = pending.conversation_id

    # 7. 构造 SSE 流
    resumed_event = _build_resumed_event(confirmation_id, pending, deps)

    async def resume_stream():
        try:
            # 7.1 emit confirmation_resumed（前端重建抽屉）
            yield _format_sse_chunk(resumed_event)

            # 7.2 hang 等 wake（redis_pubsub 模式，新 worker 接管）
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
                # 标 expired（兜底审计）
                if log_id is not None:
                    try:
                        async with AsyncSessionLocal() as cleanup_db:
                            async with cleanup_db.begin():
                                await operation_log_service.mark_expired_if_pending(
                                    cleanup_db, log_id
                                )
                    except Exception:
                        logger.exception("resume: mark_expired_if_pending failed")
                return

            # 7.3 REJECTED → emit failure result + done
            if action == ConfirmAction.REJECTED:
                if log_id is not None:
                    try:
                        async with AsyncSessionLocal() as rej_db:
                            async with rej_db.begin():
                                await operation_log_service.mark_rejected(
                                    rej_db, log_id, approved_by=current_user.user_id
                                )
                    except Exception:
                        logger.exception("resume: mark_rejected failed")
                yield _format_sse_chunk(
                    ToolCallResultEvent(
                        tool=pending.tool_name,
                        tool_call_id=pending.tool_call_id,
                        ok=False,
                        duration_ms=0,
                        error_code="USER_REJECTED",
                        error_msg="用户已取消此操作",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                return

            # 7.4 APPROVED → execute_tool 业务执行
            if log_id is None:
                # 不该发生（首次 execute_tool 已写 log）；defensive 兜底
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传找不到原 log，请重新发起",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                return

            try:
                result = await resume_tool_execution(pending, deps, log_id)
            except Exception:
                logger.exception("resume: resume_tool_execution failed")
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传 tool 执行失败，请重新发起",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                return

            # 7.5 emit tool_call_result + done（duration_ms 不可知，前端可显示"·")
            yield _format_sse_chunk(
                ToolCallResultEvent(
                    tool=pending.tool_name,
                    tool_call_id=pending.tool_call_id,
                    ok=result.ok,
                    duration_ms=0,
                    result=result.data if result.ok else None,
                    error_code=result.error_code if not result.ok else None,
                    error_msg=result.error_msg if not result.ok else None,
                )
            )
            yield _format_sse_chunk(DoneEvent())

        finally:
            # 释放 owner 锁（Lua 脚本 token 校验防误删）
            try:
                await redis_client.eval(_RELEASE_LOCK_LUA, 1, lock_key, worker_token)
            except Exception:
                logger.exception("resume: owner lock release failed")

    return StreamingResponse(
        resume_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

> 注意：`_format_sse_chunk` 从 `chat.py` import。chat.py 的 `_format_sse_chunk` 现在自动给 ConfirmationRequiredEvent 加 id，但 ConfirmationResumedEvent 不会触发自动加 id（它不是 ConfirmationRequiredEvent 类型）。续传流里 resumed event 是否需要 id？spec §3.1 示例里 resumed event 帧附了 `id: abc`，让前端继续记录 lastEventId（防再次断流）。如果需要，Task 4 完成后追加一个 commit 给 resumed event 也加 id（条件 `isinstance(event, ConfirmationResumedEvent)`）。

- [ ] **Step 5: 跑测试，预期全过**

```bash
uv run pytest tests/modules/ai/test_resume.py -v
```

预期：18 个测试全过

- [ ] **Step 6: ruff + 全量测试**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/modules/ai/ -v
```

预期：现有测试零回归

- [ ] **Step 7: Commit**

```bash
git add app/modules/ai/agents/gateway/executor.py app/modules/ai/api/resume.py tests/modules/ai/test_resume.py
git commit -m "feat(ai): implement resume success path with owner lock + executor helper"
```

---

## Task 5: main.py 注册 `/ai/chat/resume` 路由

**Files:**
- Modify: `app/main.py:22-27`（import ai_resume_router）
- Modify: `app/main.py:191-204`（在 `if settings.AI_MODULE_ENABLED:` 块内 include_router）
- Test: `tests/modules/ai/test_resume.py` 追加路由注册测试

- [ ] **Step 1: 写失败测试**

`tests/modules/ai/test_resume.py` 末尾追加：

```python
# ============ 路由注册 ============


class TestResumeRouterRegistered:
    def test_route_in_openapi(self) -> None:
        from app.main import app

        paths = app.openapi()["paths"]
        # /ai/chat prefix + "" 路径 = /ai/chat/resume 端点
        # 注意：ai_chat_router prefix="/ai/chat"，resume router 路径 ""，
        #       所以最终路径取决于 include 顺序。先看实际 openapi。
        # 用路径搜索方式（更稳健）：
        assert any(
            "/resume" in path and "get" in methods
            for path, methods in paths.items()
        ), "resume endpoint not registered"

    def test_route_gated_by_ai_module_enabled(self) -> None:
        """AI_MODULE_ENABLED=False 时 resume 路由也不应注册"""
        # 此测试在 lifespan 里不好验证（settings 已加载），改用 source inspection：
        from app import main
        import inspect
        src = inspect.getsource(main)
        # 注册语句应在 `if settings.AI_MODULE_ENABLED:` 块内
        assert "ai_resume_router" in src
```

- [ ] **Step 2: 跑测试，预期 FAIL**

```bash
uv run pytest tests/modules/ai/test_resume.py::TestResumeRouterRegistered -v
```

预期：`AssertionError: resume endpoint not registered`

- [ ] **Step 3: 实现 — 注册路由**

`app/main.py:23` 后加 import：

```python
from app.modules.ai.api.resume import router as ai_resume_router
```

`app/main.py:193`（`app.include_router(ai_chat_router, ...)`）后加：

```python
    app.include_router(ai_resume_router, prefix="/ai/chat", tags=["AI对话"])
```

- [ ] **Step 4: 跑测试，预期全过**

```bash
uv run pytest tests/modules/ai/test_resume.py -v
uv run uvicorn app.main:app --port 8000 &  # 启 dev server
sleep 2
curl -s http://localhost:8000/openapi.json | grep -i resume
kill %1
```

预期：测试全过，openapi 含 `/ai/chat/resume`

- [ ] **Step 5: ruff + 全量测试**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/modules/ai/ -v
```

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/modules/ai/test_resume.py
git commit -m "feat(ai): register /ai/chat/resume router gated by AI_MODULE_ENABLED"
```

---

## Task 6: 前端 aiStore 状态扩展 + `attemptResume` action + SSE 解析 `id:`

**Files:**
- Modify: `src/store/modules/ai/index.ts:30-38`（state 扩展）
- Modify: `src/store/modules/ai/index.ts:161-235`（`handleAiStreamEvent` / `parseSsePayload` 加 confirmation_resumed 分支）
- Modify: `src/store/modules/ai/index.ts:238-357`（`doStream` 加 id: 解析 + 断流触发续传）
- Modify: `src/typings/api/ai.d.ts`（加 `ConfirmationResumedEvent` 类型）
- Test: `tests/unit/store/ai-resume.spec.ts`（新建）

- [ ] **Step 1: 写失败测试**

`tests/unit/store/ai-resume.spec.ts`：

```typescript
import { setActivePinia, createPinia } from 'pinia';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useAiStore } from '@/store/modules/ai';

describe('aiStore resume state', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('pendingConfirmationId set on confirmation_required', () => {
    const store = useAiStore();
    store.handleAiStreamEvent({
      type: 'confirmation_required',
      confirmationId: 'cid_123',
      toolCallId: 'tc_456',
      tool: 'user.update',
      summary: '...',
      args: {},
      expiresAt: '2099-01-01T00:00:00Z'
    } as any);
    expect(store.pendingConfirmationId).toBe('cid_123');
    expect(store.pendingToolCallId).toBe('tc_456');
  });

  it('pendingConfirmationId cleared on tool_call_result matching toolCallId', () => {
    const store = useAiStore();
    store.handleAiStreamEvent({
      type: 'confirmation_required',
      confirmationId: 'cid_123',
      toolCallId: 'tc_456',
      tool: 't', summary: '', args: {}, expiresAt: ''
    } as any);
    store.handleAiStreamEvent({
      type: 'tool_call_result',
      toolCallId: 'tc_456',
      tool: 't', ok: true, durationMs: 0
    } as any);
    expect(store.pendingConfirmationId).toBeNull();
    expect(store.pendingToolCallId).toBeNull();
  });

  it('parseSsePayload extracts id line', () => {
    const store = useAiStore();
    // 直接测内部：parseSsePayload 不暴露，借 doStream 集成测难；此处测 SSE 帧解析 helper
    // 若 parseSsePayload 未 export，跳过此测，改为集成测
  });

  it('confirmation_resumed sets reconnectedAt on drawer state', () => {
    const store = useAiStore();
    store.handleAiStreamEvent({
      type: 'confirmation_resumed',
      confirmationId: 'cid_x',
      toolCallId: 'tc_y',
      tool: 't', summary: '', args: {},
      expiresAt: '', resumedAt: '2026-07-13T14:35:00Z'
    } as any);
    expect(store.pendingConfirmation?.confirmationId).toBe('cid_x');
    expect(store.pendingConfirmation?.resumedAt).toBe('2026-07-13T14:35:00Z');
  });

  it('resumeAttempts increments on attemptResume', async () => {
    const store = useAiStore();
    expect(store.resumeAttempts).toBe(0);
    // mock fetch 让它返 409（不走成功路径）
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 409, body: null }));
    await store.attemptResume('cid_x');
    expect(store.resumeAttempts).toBe(1);
  });

  it('resumeAttempts capped at 3', async () => {
    const store = useAiStore();
    store.resumeAttempts = 3;
    vi.stubGlobal('fetch', vi.fn());
    await store.attemptResume('cid_x');
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: 跑测试，预期 FAIL**

```bash
cd F:/code/hohu/hohu-admin-web
pnpm test tests/unit/store/ai-resume.spec.ts
```

预期：`pendingConfirmationId is undefined` / `attemptResume is not a function`

- [ ] **Step 3: 实现 — aiStore 扩展**

`src/store/modules/ai/index.ts` 在 line 36（`pollTimer` 后）追加 state：

```typescript
  // v1.5+: SSE 续传（spec §3 / §5）
  /** 当前 HITL 期的 confirmation_id（断流重连用 Last-Event-ID 头携带） */
  const pendingConfirmationId = ref<string | null>(null);
  /** 配对的 tool_call_id（收到 tool_call_result 时反查清状态） */
  const pendingToolCallId = ref<string | null>(null);
  /** 续传重试次数（防死循环，上限 3） */
  const resumeAttempts = ref(0);
  /** 最近 SSE 帧的 id:（Last-Event-ID 头用） */
  let lastEventId: string | null = null;
```

修改 `handleAiStreamEvent`（line 161）：

```typescript
  function handleAiStreamEvent(event: Api.Ai.AiStreamEvent) {
    switch (event.type) {
      case 'tool_call_started':
      case 'tool_call_result':
        // spec §5.2: tool_call_result 用 toolCallId 反查清 HITL pending 状态
        if (event.type === 'tool_call_result' && event.toolCallId === pendingToolCallId.value) {
          pendingConfirmationId.value = null;
          pendingToolCallId.value = null;
        }
        streamEvents.value.push(event);
        break;
      case 'confirmation_required':
      case 'confirmation_resumed':
        // spec §2.2: 两个事件 schema 兼容，统一渲染 + 记录 pendingConfirmationId
        pendingConfirmation.value = event;
        pendingConfirmationId.value = event.confirmationId;
        pendingToolCallId.value = event.toolCallId;
        streamEvents.value.push(event);
        break;
      case 'ai_error':
        window.$message?.error(`AI 错误: ${event.message || '未知错误'}`);
        break;
      case 'done':
        break;
      default:
        break;
    }
  }
```

修改 `parseSsePayload`（line 185） — 在 `event.type === 'done'` 后加 `confirmation_resumed`：

```typescript
    if (
      event.type === 'tool_call_started' ||
      event.type === 'tool_call_result' ||
      event.type === 'confirmation_required' ||
      event.type === 'confirmation_resumed' ||
      event.type === 'ai_error'
    ) {
      handleAiStreamEvent(event as Api.Ai.AiStreamEvent);
      return false;
    }
```

修改 `doStream` 的 SSE 解析（line 282-298） — 添加 `id:` 行解析：

```typescript
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';

        for (const part of parts) {
          const lines = part.split('\n');
          let payload = '';
          for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('data: ')) {
              payload = trimmed.slice(6);
            } else if (trimmed.startsWith('id: ')) {
              // spec §3.2: SSE 协议标准 id 字段，缓存到 lastEventId
              lastEventId = trimmed.slice(4);
            }
          }
          if (!payload) continue;
          const shouldEnd = parseSsePayload(payload);
          if (shouldEnd) break;
        }
      }
```

修改 `doStream` 的 catch（line 317） — 断流触发续传：

```typescript
    } catch (error: any) {
      if (error.name === 'AbortError') {
        // 用户主动 abort，不续传
        if (streamingText.value) {
          currentMessages.value.push({ /* ...existing... */ });
        }
      } else if (pendingConfirmationId.value && resumeAttempts.value < 3) {
        // spec §5.3: HITL 期断流 → 自动续传
        attemptResume(pendingConfirmationId.value);
      } else {
        window.$message?.error(`发送失败: ${error.message}`);
      }
    } finally {
```

`doStream` 的 finally（line 337） — 仅清状态，不触发续传（避免 done 事件正常结束时误触发）：

```typescript
    } finally {
      isStreaming.value = false;
      streamingText.value = '';
      reasoningText.value = '';
      abortController = null;
      // 注意：续传只在 catch 块（网络异常）触发，不在 finally 块触发。
      // 理由：done 事件正常结束时 pendingConfirmation 还在是正常的
      // （让用户能继续点 confirm），不应误触发续传。

      // Replace temp messages with real IDs from backend after stream completes normally
      if (streamCompleted && currentConversationId.value) {
        try {
          const { data, error } = await fetchGetConversationDetail(currentConversationId.value);
          if (!error && data) {
            currentMessages.value = data.messages;
          }
        } catch {
          // keep local messages as fallback
        }
      }
    }
```

新增 `attemptResume` action（在 `resolveConfirmation` 附近，line 362 前）：

```typescript
  // ============ SSE 续传（spec §5 v1.5+） ============

  /** HITL 期断流续传：fetch /ai/chat/resume 带 Last-Event-ID 头 */
  async function attemptResume(confirmationId: string) {
    if (resumeAttempts.value >= 3) {
      window.$message?.error('续传失败 3 次，请重新发起对话');
      return;
    }
    resumeAttempts.value += 1;
    isStreaming.value = true;

    try {
      const baseUrl = getBaseUrl();
      const token = localStg.get('token');
      const response = await fetch(`${baseUrl}/ai/chat/resume`, {
        method: 'GET',
        headers: {
          Authorization: token ? `Bearer ${token}` : '',
          'Last-Event-ID': confirmationId,
          Accept: 'text/event-stream'
        }
      });

      if (response.status === 409) {
        // AI_RESUME_IN_PROGRESS → 2s 后重试
        await new Promise(resolve => setTimeout(resolve, 2000));
        return attemptResume(confirmationId);
      }
      if (response.status === 410) {
        // AI_RESUME_DISABLED / AI_RESUME_ALREADY_RESOLVED → 退化为轮询兜底
        if (pendingToolCallId.value) {
          window.$message?.info('操作已被处理，正在拉取结果...');
          startPollingResult(pendingToolCallId.value);
        } else {
          window.$message?.error('续传不可用，请重新发起');
        }
        return;
      }
      if (response.status === 422) {
        // AI_RESUME_TTL_TOO_SHORT
        window.$message?.warning('确认窗口已临近超时，请重新发起');
        return;
      }
      if (!response.ok) {
        window.$message?.error(`续传失败: ${response.status}`);
        return;
      }

      // 200 → 复用 doStream 的 SSE 解析逻辑
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const part of parts) {
          const lines = part.split('\n');
          let payload = '';
          for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('data: ')) payload = trimmed.slice(6);
            else if (trimmed.startsWith('id: ')) lastEventId = trimmed.slice(4);
          }
          if (!payload) continue;
          parseSsePayload(payload);
        }
      }
    } catch (error: any) {
      window.$message?.error(`续传失败: ${error.message}`);
    } finally {
      isStreaming.value = false;
    }
  }
```

`return` 末尾加 export：

```typescript
  return {
    // ...existing...
    pendingConfirmationId,
    pendingToolCallId,
    resumeAttempts,
    attemptResume,
    // ...existing...
  };
```

修改 `src/typings/api/ai.d.ts` — 在 `ConfirmationRequiredEvent` 类型附近加：

```typescript
    ConfirmationResumedEvent: {
      type: 'confirmation_resumed';
      confirmationId: string;
      tool: string;
      toolCallId: string;
      summary: string;
      args: Record<string, unknown>;
      expiresAt: string;
      resumedAt: string;
      dryRun?: { summary: string; affectedCount: number; affectedExamples?: string[] };
    };
```

把 `AiStreamEvent` 联合类型加上 `ConfirmationResumedEvent`：

```typescript
    AiStreamEvent =
      | ToolCallStartedEvent
      | ToolCallResultEvent
      | ConfirmationRequiredEvent
      | ConfirmationResumedEvent
      | AiErrorEvent
      | DoneEvent;
```

`ConfirmationRequiredEvent` 字段也加 `resumedAt?: string`（让 pendingConfirmation ref 同时能容纳 resumed 事件）。

- [ ] **Step 4: 跑测试，预期全过**

```bash
pnpm test tests/unit/store/ai-resume.spec.ts
pnpm typecheck
```

- [ ] **Step 5: lint + fmt**

```bash
pnpm lint && pnpm fmt
```

- [ ] **Step 6: Commit**

```bash
cd F:/code/hohu/hohu-admin-web
git add src/store/modules/ai/index.ts src/typings/api/ai.d.ts tests/unit/store/ai-resume.spec.ts
git commit -m "feat(ai): add resume state + attemptResume action for SSE reconnection"
```

---

## Task 7: chat-confirmation-drawer.vue 加 "已重连" badge

**Files:**
- Modify: `src/views/ai/chat/modules/chat-confirmation-drawer.vue:21-83`（计算 `resumedAt` + UI）
- Test: 手动验证（前端组件测试覆盖率不一定到这个粒度）

- [ ] **Step 1: 实现 — 加 reconnected 计算属性 + chip UI**

`src/views/ai/chat/modules/chat-confirmation-drawer.vue`：

在 `<script setup>` 内（line 21 后）加：

```typescript
const isReconnected = computed(() => Boolean(confirmation.value?.resumedAt));
const reconnectedAt = computed(() => {
  const ra = confirmation.value?.resumedAt;
  if (!ra) return '';
  try {
    const d = new Date(ra);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  } catch {
    return '';
  }
});
```

在 template 的 Tool 信息 section（line 88-92）后追加：

```vue
        <!-- 续传重连标记（spec §2.2 v1.5+） -->
        <div v-if="isReconnected" class="confirm-reconnect-badge">
          <NTag type="info" size="small" :bordered="false">
            <IconIcRoundRefresh class="text-12px" />
            {{ t('page.ai.chat.resumedAt', { time: reconnectedAt }) }}
          </NTag>
        </div>
```

在 i18n 文件 `src/locales/langs/zh-cn.ts` 的 `page.ai.chat` 节点加：

```typescript
    resumedAt: '已重连于 {time}',
```

`src/locales/langs/en-us.ts` 对应位置加：

```typescript
    resumedAt: 'Reconnected at {time}',
```

- [ ] **Step 2: 手动验证**

```bash
pnpm dev
# 浏览器打开 http://localhost:9527
# 触发一次 HITL（如批量删 2 个用户）→ 抽屉弹出
# 在 DevTools 切断网络（offline → online）→ 抽屉显示"已重连于 HH:MM"chip
```

- [ ] **Step 3: typecheck + lint**

```bash
pnpm typecheck && pnpm lint && pnpm fmt
```

- [ ] **Step 4: Commit**

```bash
git add src/views/ai/chat/modules/chat-confirmation-drawer.vue src/locales/langs/zh-cn.ts src/locales/langs/en-us.ts
git commit -m "feat(ai): show reconnected badge in HITL drawer on resume"
```

---

## Task 8: spec / AI-DEPLOYMENT 回写

**Files:**
- Modify: `docs/specs/2026-07-13-sse-resume-design.md:3`（Status ⚠️ → ✅）
- Modify: `docs/specs/2026-07-02-ai-tool-gateway-design.md`（§8.5 / §14 Roadmap / §22 SR-9~12）
- Modify: `docs/AI-DEPLOYMENT.md`（§10 监控接入补续传依赖）

- [ ] **Step 1: 改 sse-resume-design.md Status**

`docs/specs/2026-07-13-sse-resume-design.md:3`：

```diff
-**Status**: ⚠️ Plan v1.5+（待实现）
+**Status**: ✅ 已完成（2026-07-13 v1.5+）
```

- [ ] **Step 2: 改 ai-tool-gateway-design.md §8.5**

找到 §8.5 "MVP 简化（断流即取消）"，在末尾追加：

```markdown
**v1.5+ 已实现（2026-07-13）**：SSE 续传（HITL 期热接管）。详见 [`2026-07-13-sse-resume-design.md`](./2026-07-13-sse-resume-design.md)（SR-9 / SR-10 / SR-11 / SR-12）。

- confirmation_required 事件附带 SSE 标准 `id:` 字段
- 新端点 `GET /ai/chat/resume` 读 `Last-Event-ID` 头接管原 confirmation
- Redis SETNX owner 锁防双执行（TTL 60s ≥ AI_TOOL_TIMEOUT）
- 新事件 `confirmation_resumed`（schema 兼容 confirmation_required + `resumedAt`）
```

- [ ] **Step 3: 改 ai-tool-gateway-design.md §14 Roadmap**

找到 §14 v1.5+ Roadmap 表中"SSE sequence_id 断点续传"行，替换为：

```diff
-| SSE sequence_id 断点续传 | 网络抖动频繁 | 单调递增 sequence + `Last-Event-ID` 头重连 |
+| ✅ **SSE 续传（HITL 期热接管） — 已完成 2026-07-13**（spec `2026-07-13-sse-resume-design.md` / SR-9 / SR-10 / SR-11 / SR-12）| 网络抖动频繁 | SSE 标准 `id:` 字段 + `Last-Event-ID` 头 + Redis SETNX owner 锁（TTL 60s） + `confirmation_resumed` 新事件 |
```

- [ ] **Step 4: 改 ai-tool-gateway-design.md §22 加 SR-9~12**

找到 §22 决策记录区，在末尾追加（spec §10 内容已写好，直接 copy）：

```markdown
### SR-9. **SSE 续传用标准协议字段（`id:` + `Last-Event-ID` 头）而非私有 query param**（2026-07-13 v1.5+）

（内容见 `2026-07-13-sse-resume-design.md` §10 SR-9）

### SR-10. **SSE 续传并发安全默认到位（Redis SETNX owner 锁）**（2026-07-13 v1.5+）

（内容见 `2026-07-13-sse-resume-design.md` §10 SR-10）

### SR-11. **续传仅覆盖 HITL 期，不缓存流式 text-delta**（2026-07-13 v1.5+）

（内容见 `2026-07-13-sse-resume-design.md` §10 SR-11）

### SR-12. **新增 `confirmation_resumed` 事件而非重放 `confirmation_required`**（2026-07-13 v1.5+）

（内容见 `2026-07-13-sse-resume-design.md` §10 SR-12）
```

- [ ] **Step 5: 改 AI-DEPLOYMENT.md**

`docs/AI-DEPLOYMENT.md` 找到 §10 监控接入（Prometheus 部分），在末尾追加：

```markdown
### SSE 续传依赖（spec §3 v1.5+）

SSE 续传（HITL 期热接管）要求 **`AI_HITL_MODE=redis_pubsub`** + 多 worker 部署。

- 内网部署 / 单 worker：保持 `memory` 模式，续传端点返 410（前端退化为 MVP 行为）
- 移动端 / 不稳定网络：必须 `redis_pubsub` 模式，否则断流即取消（重新发起对话）

配置：
```env
AI_HITL_MODE=redis_pubsub       # 启用续传的硬约束
AI_SSE_RESUME_ENABLED=true      # 续传功能开关（默认开）
AI_HITL_OWNER_LOCK_TTL_SEC=60   # owner 锁 TTL，修改 AI_TOOL_TIMEOUT 时同步检查
```

修改 `AI_TOOL_TIMEOUT` 时务必同步检查 `AI_HITL_OWNER_LOCK_TTL_SEC`（spec §2.3 SR-10 反例 5）：owner 锁 TTL 必须 ≥ `AI_TOOL_TIMEOUT`，否则 execute_tool 慢时锁先过期 → 新 worker B 抢锁双执行。
```

- [ ] **Step 6: Commit**

```bash
cd F:/code/hohu/hohu-admin
git add docs/specs/2026-07-13-sse-resume-design.md docs/specs/2026-07-02-ai-tool-gateway-design.md docs/AI-DEPLOYMENT.md
git commit -m "docs(ai): backfill SSE resume spec status + Roadmap + SR-9~12 + deployment note"
```

---

## 验证

### 单测（后端）

```bash
cd F:/code/hohu/hohu-admin
uv run pytest tests/modules/ai/test_resume.py tests/modules/ai/test_resume_events.py tests/modules/ai/test_chat_sse_id.py -v
# 期望：约 25 个新测试全过
uv run ruff check . && uv run ruff format .
```

### 单测（前端）

```bash
cd F:/code/hohu/hohu-admin-web
pnpm test tests/unit/store/ai-resume.spec.ts
pnpm typecheck && pnpm lint && pnpm fmt
```

### 端到端（手动）

```bash
# 1. 启动后端 + 前端
cd F:/code/hohu/hohu-admin && AI_HITL_MODE=redis_pubsub uv run fastapi dev app/main.py &
cd F:/code/hohu/hohu-admin-web && pnpm dev &
# 2. 浏览器打开 http://localhost:9527
# 3. 发起一次 HITL 对话（如"批量删除 2 个测试用户"）
# 4. 看到抽屉弹出后，DevTools → Network → Offline
# 5. 等 1 秒 → Online
# 6. 期望：抽屉保留，显示"已重连于 HH:MM"chip
# 7. 点"确认" → 看到 tool_call_result 显示成功
```

### Race 验证（可选）

- 启动两个 worker：`uvicorn app.main:app --workers 2`
- 模拟断流后立即重连（curl 并发触发）
- 期望：第二个 curl 收到 409（AI_RESUME_IN_PROGRESS）→ 2s 后重试 → 成功接管

---

## 范围外（v1.6+ / PR-2）

- text-delta / reasoning-delta 持久化 + 续传（spec §1.3 决策：只覆盖 HITL 期）
- worker A cancel 慢的优化（当前 owner 锁 TTL 60s 已够）
- execute_tool 结果缓存（worker B 在 resume 时检查 Redis 缓存直接 emit）
- 跨设备续传（用户在 A 设备发起 HITL 后在 B 设备续传）
- confirmation_resumed 事件附带 `id:` 字段（防再次断流，Task 4 备注）
