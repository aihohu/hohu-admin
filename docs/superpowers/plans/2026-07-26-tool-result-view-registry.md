# Tool Result View Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tool result 拆双层（`data` 给 LLM / `ui` 给前端），引入 5 种标准 view_type + chip_target 声明式字段，彻底消除前端 `CHIP_TARGETS` / `statsData` / `resultJson` 三处硬编码 by tool name。

**Architecture:**
- 后端 `ToolResult` 加 `ui: UIResult | None` 字段（可选，向后兼容）；`UIResult(view_type, view_data, audit, label_key, label_params)`
- `AiToolMeta` 加 `result_view: str = "plain_json"` + `chip_target: str | None = None`（合并 query_cache_module 含义）
- `executor._run_tool_fn` 加 `isinstance(raw, ToolResult)` 双路径分支：业务方返回 ToolResult 直接用；返回 dict 则 fallback 包装为 ui=None（向后兼容第三方 tool）
- 5 种 view_type：`rows_affected` / `data_list` / `stats_chart` / `detail_card` / `plain_json`
- 前端 `<component :is="resolveToolView(ui.viewType)" :data="ui" />` 按 `viewType` 路由标准组件
- 新增 `scripts/check_ai_tools_ui.py` lint：builtin tool 装饰的函数返回 ToolResult.success 时强制带 ui=

**Tech Stack:** Python 3.13 / SQLAlchemy 2.0 async / dataclasses / Vue 3 / TypeScript discriminated union / NaiveUI / vue-i18n

---

## 设计决策（11 条，已锁定）

| # | 决策 | 反例 |
|---|---|---|
| 1 | view_type 5 种：`rows_affected` / `data_list` / `stats_chart` / `detail_card` / `plain_json` | spec 原 7 种含 `redirect_chip`（基于 tool 性质非 result）和 `confirmation_summary`（时态早于 result）—— 范畴错误 |
| 2 | `AiToolMeta.chip_target` 声明式（替代 `query_cache_module` + 前端 `CHIP_TARGETS` map） | 前端硬编码 by tool name，每加 readonly tool 都要改前端 |
| 3 | **`ToolResult.success(data, *, ui: UIResult \| None = None)`** ui 可选；executor 双路径分支；**lint 强制 builtin tool 函数返回 ToolResult.success 时带 ui=**（决策 3 修正） | ui 必填 → break 现有 executor.py:805 fallback + test_events.py:356/367/387 + 第三方 tool |
| 4 | 一次性全迁移 10 个 builtin tool | 渐进迁移导致 tool 视觉不一致 + 长期 fallback 没压力 |
| 5 | `meta.result_view` 默认 `"plain_json"` + 启动校验合法性 | 默认 None 强制声明 → 现有测试 fixture 全部要改 |
| 6 | `affected_rows` 优先级：`dry_run_count` > `ui.audit.affected_count` > `_infer_affected_rows` 推断 | 双源不一致 → 卡片显示两个不同数字 |
| 7 | `ui` 字段不进 LLM context（executor 脱敏 `data` 后 return；`ui` 不经 `serialize_for_llm`） | ui 进 prompt → 浪费 token + audit 字段泄漏 |
| 8 | `query_cache_module` 字段保留为 alias，新代码用 `chip_target`；executor 读 `meta.chip_target or meta.query_cache_module` | 立即删除 → 现有 8 个 tool 全部报错 |
| 9 | 前端 `tool_call_started` 事件追加 `chipTarget` 字段 | 前端拉 `/ai/tools/registry` 一次性缓存 → 量大，留 v2+ |
| 10 | TS discriminated union 给 view_data 强类型；后端不强校验 view_data schema | 后端 Pydantic 校验 → 业务方写错启动失败，过度严苛 |
| 11 | executor 双路径分支（`isinstance(raw, ToolResult)` / fallback 包装 dict） | 单路径 → 现有 dict 返回值的 tool 全部 double-wrap |

---

## 文件结构

### 后端（`hohu-admin/`）

| 文件 | 类型 | 责任 |
|---|---|---|
| `app/modules/ai/agents/gateway/result.py` | 改 | 加 `UIResult` dataclass + `ToolResult.ui` 字段（optional） |
| `app/modules/ai/agents/tools/meta.py` | 改 | 加 `result_view: str = "plain_json"` + `chip_target: str \| None = None` + `STANDARD_VIEW_TYPES` |
| `app/modules/ai/agents/hitl/events.py` | 改 | `ToolCallResultEvent.ui` + `ToolCallStartedEvent.chip_target` + 序列化 |
| `app/modules/ai/agents/tools/registry.py` | 改 | `validate_on_startup` 加 result_view 校验 |
| `app/modules/ai/agents/gateway/executor.py` | 改 | emit 时填 ui / chip_target；`_run_tool_fn` 加 isinstance 双路径；affected_rows 优先 ui.audit；query_cache 读 chip_target fallback query_cache_module |
| `app/modules/system/ai_tools.py` | 改 | 8 个 tool 函数改返回 `ToolResult.success(data=..., ui=...)` + meta 加 chip_target |
| `app/modules/job/ai_tools.py` | 改 | 1 个 tool（job.update_cron） |
| `app/modules/ai/agents/tools/file_tools.py` | 改 | 1 个 tool（file.parse） |
| `scripts/check_ai_tools_ui.py` | 新 | lint：builtin tool 函数返回 ToolResult.success 时必须带 ui= |
| `.pre-commit-config.yaml` 或 `pyproject.toml` | 改 | 集成新 lint |
| `tests/modules/ai/test_tool_result.py` | 新 | UIResult / ToolResult.ui 测试 |
| `tests/modules/ai/test_tool_registry.py` | 改 | 加 result_view 校验测试 |
| `tests/modules/ai/test_gateway.py` | 改 | 加 ui 序列化 + LLM strip + executor 双路径测试 |
| `tests/modules/ai/test_system_ai_tools.py` | 改 | 14 处断言 `result == {"count": N}` → `result.data == {"count": N}` |
| `tests/modules/system/test_ai_tools_user_batch_delete.py` | 改 | 断言适配 ToolResult |
| `tests/modules/job/test_job_service.py` 或新建 | 改/新 | job.update_cron 返回 ToolResult 测试 |

### 前端（`hohu-admin-web/`）

| 文件 | 类型 | 责任 |
|---|---|---|
| `src/views/ai/chat/modules/tool-views/RowsAffectedView.vue` | 新 | rows_affected view 组件 |
| `src/views/ai/chat/modules/tool-views/DataListView.vue` | 新 | data_list view 组件 |
| `src/views/ai/chat/modules/tool-views/StatsChartView.vue` | 新 | stats_chart view 组件（迁移现有 ChatToolStatsTabs 逻辑） |
| `src/views/ai/chat/modules/tool-views/DetailCardView.vue` | 新 | detail_card view 组件 |
| `src/views/ai/chat/modules/tool-views/PlainJsonView.vue` | 新 | plain_json fallback |
| `src/views/ai/chat/modules/tool-views/index.ts` | 新 | view_type → 组件 registry + resolveToolView() |
| `src/views/ai/chat/modules/chat-tool-call.vue` | 改 | `<component :is>` + 删 CHIP_TARGETS / statsData / resultJson / ChatToolStatsTabs import |
| `src/views/ai/chat/modules/chat-tool-stats-tabs.vue` | 删 | 逻辑迁移到 StatsChartView.vue |
| `src/typings/api/ai.d.ts` | 改 | UIResult discriminated union + 事件字段 |
| `src/locales/langs/zh-cn/ai.ts` | 改 | tool result label keys |
| `src/locales/langs/en-us/ai.ts` | 改 | tool result label keys |

---

## Task 1: 后端 UIResult dataclass + ToolResult.ui 字段（optional）

**Files:**
- Modify: `app/modules/ai/agents/gateway/result.py`
- Test: `tests/modules/ai/test_tool_result.py` (新)

- [ ] **Step 1: 写失败测试 — UIResult 构造 + ToolResult.ui optional**

```python
# tests/modules/ai/test_tool_result.py
"""UIResult + ToolResult.ui 字段测试（spec 2026-07-16-tool-result-view-design.md §2.1/§2.2）。"""

from app.modules.ai.agents.gateway.result import ToolResult, UIResult


class TestUIResult:
    def test_construct_with_all_fields(self):
        ui = UIResult(
            view_type="rows_affected",
            view_data={"count": 2, "ids": ["u1", "u2"]},
            audit={"affected_user_ids": ["u1", "u2"]},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": 2},
        )
        assert ui.view_type == "rows_affected"
        assert ui.view_data["count"] == 2
        assert ui.audit["affected_user_ids"] == ["u1", "u2"]
        assert ui.label_key == "ai.tool.user.batch_delete.result"
        assert ui.label_params == {"count": 2}

    def test_default_audit_and_label_are_empty(self):
        ui = UIResult(view_type="plain_json", view_data={"count": 5})
        assert ui.audit == {}
        assert ui.label_key == ""
        assert ui.label_params == {}


class TestToolResultUi:
    def test_success_with_ui(self):
        ui = UIResult(view_type="plain_json", view_data={"count": 5})
        r = ToolResult.success(data={"count": 5}, ui=ui)
        assert r.ok is True
        assert r.data == {"count": 5}
        assert r.ui is not None
        assert r.ui.view_type == "plain_json"

    def test_success_without_ui_returns_none_ui(self):
        """决策 3 修正：ui 可选，向后兼容现有 executor / 第三方 tool。
        builtin tool 强制带 ui 由 lint（Task 9）保证，不靠 ToolResult.success 签名。
        """
        r = ToolResult.success(data={"count": 5})
        assert r.ok is True
        assert r.data == {"count": 5}
        assert r.ui is None

    def test_failure_does_not_require_ui(self):
        r = ToolResult.failure(
            error_code="AI_DATA_SCOPE_VIOLATION",
            error_msg="target not in scope",
        )
        assert r.ok is False
        assert r.ui is None
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd F:/code/hohu/hohu-admin
python -m pytest tests/modules/ai/test_tool_result.py -v
```
Expected: FAIL — `ImportError: cannot import name 'UIResult'`

- [ ] **Step 3: 实现 UIResult + ToolResult.ui**

替换 `app/modules/ai/agents/gateway/result.py` 全文：

```python
"""ToolResult / UIResult — Gateway 执行结果标准化

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.5 / §9.6 +
spec docs/specs/2026-07-16-tool-result-view-design.md §2.1/§2.2。

双层设计（决策 3 修正：ui 可选，lint 强制 builtin tool 填 ui）：
  ToolResult.data — 给 LLM（精简，进 prompt cache，serialize_for_llm 脱敏）
  ToolResult.ui  — 给前端（丰富，不进 LLM context）

业务方写 builtin tool 时**强烈推荐**填 ui（lint 强制，Task 9）：
  return ToolResult.success(
      data={"deleted": 2},
      ui=UIResult(view_type="rows_affected", view_data={"count": 2, "ids": [...]}),
  )

executor 兼容路径（dict / list 返回值，第三方 tool 或老代码）：
  return ToolResult.success(data=safe_data)  # ui=None，前端 fallback plain_json
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UIResult:
    """UI 层结果（spec §2.2）— 给前端按 view_type 路由组件用，不进 LLM prompt。

    view_data 不强校验 schema（决策 10）：后端保持 dict[str, Any] 灵活，
    前端 TS discriminated union 给类型安全。业务方写错只影响自家 tool 渲染。
    """

    view_type: str
    """标准 view_type key（启动校验，必须在 STANDARD_VIEW_TYPES）"""

    view_data: dict[str, Any]
    """view_type 对应组件的 props（如 rows_affected 的 {count, ids}）"""

    audit: dict[str, Any] = field(default_factory=dict)
    """标准化审计字段（affected_user_ids / before_value / after_value 等），
    写入 ai_operation_log.result_summary 供后台审计页反查。决策 4 注：与
    args_summary_fields 正交——前者审计入参，后者审计结果。"""

    label_key: str = ""
    """i18n key（如 ai.tool.user.batch_delete.result）；空字符串表示用默认文案"""

    label_params: dict[str, Any] = field(default_factory=dict)
    """i18n 插值参数（如 {"count": 2} → "已删除 {count} 行"）"""


@dataclass
class ToolResult:
    """Gateway tool 执行结果（统一容器）

    PydanticAI 包装层把 ToolResult.data 转成 LLM 可读的格式返回给 LLM。
    API 层（如 /ai/confirm 端点）也可直接序列化 ToolResult 给前端。
    """

    ok: bool
    """True = 业务成功；False = 业务异常或鉴权拒绝"""

    data: Any = None
    """业务返回值（ok=True 时有效）— 给 LLM 看，serialize_for_llm 脱敏后进 prompt"""

    ui: UIResult | None = None
    """UI 层结果（spec §2.1）— 给前端用，不进 LLM context。
    None（ok=False / executor 兼容路径 / 业务方未声明）→ 前端 fallback plain_json。
    builtin tool 由 lint 强制带 ui（Task 9）。"""

    error_code: str = ""
    """UPPER_SNAKE_CASE 错误码（ok=False 时必填），如 AI_DATA_SCOPE_VIOLATION"""

    error_msg: str = ""
    """给 LLM 看的错误描述（ok=False 时必填），LLM 据此反问用户"""

    meta: dict[str, Any] = field(default_factory=dict)
    """执行元信息（duration_ms / execution_mode / retry_count），不进 SSE，仅日志 / metric"""

    @classmethod
    def success(
        cls,
        data: Any,
        *,
        ui: UIResult | None = None,
        **meta: Any,
    ) -> "ToolResult":
        """构造成功结果

        Args:
            data: 给 LLM 的精简数据（进 prompt cache）
            ui: 给前端 UI 的丰富数据（不进 prompt）；None 时前端 fallback plain_json
            **meta: 执行元信息（duration_ms 等，不进 SSE）
        """
        return cls(ok=True, data=data, ui=ui, meta=meta)

    @classmethod
    def failure(cls, error_code: str, error_msg: str, **meta: Any) -> "ToolResult":
        """构造失败结果

        Args:
            error_code: UPPER_SNAKE_CASE，前端 i18n 映射用（§9.6）
            error_msg: 给 LLM 的友好描述（如"目标用户不在你的可见范围"）
        """
        return cls(ok=False, error_code=error_code, error_msg=error_msg, meta=meta)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
python -m pytest tests/modules/ai/test_tool_result.py -v
```
Expected: 4 passed

- [ ] **Step 5: 跑全量 ai 模块回归确认无破坏（ui optional 不影响现有调用）**

```bash
python -m pytest tests/modules/ai/ -v --tb=short | tail -20
```
Expected: 全绿（现有 `ToolResult.success(data=...)` 仍合法）

- [ ] **Step 6: ruff + commit**

```bash
ruff check app/modules/ai/agents/gateway/result.py tests/modules/ai/test_tool_result.py
ruff format app/modules/ai/agents/gateway/result.py tests/modules/ai/test_tool_result.py
git add app/modules/ai/agents/gateway/result.py tests/modules/ai/test_tool_result.py
git commit -m "feat(ai): add UIResult dataclass and optional ToolResult.ui field"
```

---

## Task 2: AiToolMeta 加 result_view + chip_target 字段 + STANDARD_VIEW_TYPES

**Files:**
- Modify: `app/modules/ai/agents/tools/meta.py`
- Modify: `app/modules/ai/agents/tools/registry.py:148-208`（validate_on_startup 加 result_view 校验）
- Test: `tests/modules/ai/test_tool_registry.py`

- [ ] **Step 1: 写失败测试 — result_view 启动校验**

追加到 `tests/modules/ai/test_tool_registry.py` 末尾：

```python
class TestValidateResultViewOnStartup:
    """spec 2026-07-16-tool-result-view-design.md §2.4：result_view 启动校验。"""

    async def test_invalid_result_view_rejected(self, db_session):
        """meta.result_view 不在 STANDARD_VIEW_TYPES 时启动校验失败。"""
        import pytest

        from app.modules.ai.agents.tools.meta import AiToolMeta, STANDARD_VIEW_TYPES
        from app.modules.ai.agents.tools.registry import ToolRegistryError

        # 构造一个 result_view 非法的 meta
        meta = AiToolMeta(
            name="test.invalid_view",
            agent="user_mgmt",
            summary="t",
            required_perms=(),
            risk="low",
        )
        # 绕过 frozen dataclass 校验，直接改 result_view
        object.__setattr__(meta, "result_view", "invalid_view_type")
        assert "invalid_view_type" not in STANDARD_VIEW_TYPES

        # 复用现有 _build_registry_with_tools helper（参考 TestValidateOnStartup）
        registry = await _build_registry_with_tools({"test.invalid_view": meta}, db_session)
        with pytest.raises(ToolRegistryError, match="invalid_view_type"):
            await registry.validate_on_startup(db_session)

    def test_default_result_view_is_plain_json(self):
        """未声明 result_view 时默认 'plain_json'。"""
        from app.modules.ai.agents.tools.meta import AiToolMeta, STANDARD_VIEW_TYPES

        meta = AiToolMeta(
            name="test.default",
            agent="user_mgmt",
            summary="t",
            required_perms=(),
            risk="low",
        )
        assert meta.result_view == "plain_json"
        assert "plain_json" in STANDARD_VIEW_TYPES

    def test_standard_view_types_has_five_members(self):
        from app.modules.ai.agents.tools.meta import STANDARD_VIEW_TYPES

        assert STANDARD_VIEW_TYPES == frozenset(
            {"rows_affected", "data_list", "stats_chart", "detail_card", "plain_json"}
        )
```

注：若 `_build_registry_with_tools` helper 不存在，参考 test_tool_registry.py 已有的 fixture 模式新建。具体参考同文件 `TestValidateOnStartup` 类。

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest tests/modules/ai/test_tool_registry.py::TestValidateResultViewOnStartup -v
```
Expected: FAIL — `AttributeError: 'AiToolMeta' object has no attribute 'result_view'` 或 `ImportError: cannot import name 'STANDARD_VIEW_TYPES'`

- [ ] **Step 3: 加 result_view + chip_target 字段到 meta.py**

修改 `app/modules/ai/agents/tools/meta.py`：

**3a.** 在 `query_cache_module` 字段块（line 92-95）**之前**插入新字段块：

```python
    # ============ Tool Result View（v1.6+ SR-13） ============
    result_view: str = "plain_json"
    """spec 2026-07-16 §2.4: 标准 view_type key，决定前端按哪个组件渲染 result。
    必须在 STANDARD_VIEW_TYPES 内（启动校验）：
      - rows_affected: 写操作影响行数（user.batch_delete）
      - data_list: 列表查询（role.list / dept.list）
      - stats_chart: 统计图表（user.stats）
      - detail_card: 单实体详情（job.update_cron 返回值）
      - plain_json: fallback（user.count 这种纯数字 + chip 跳转）
    默认 'plain_json' 向后兼容老 tool。"""

    chip_target: str | None = None
    """spec 2026-07-16 §3 决策 2: chip 跳转目标路径（如 '/system/user'）。
    readonly tool 声明 chip_target 后：
      - 后端写 ai:query_cache hash 时 module 字段填此路径
      - 前端 tool_call_started 事件携带 chipTarget，不再硬编码 CHIP_TARGETS map
    None = 不显示 chip（写 tool / stats tool / detail tool 都不需要 chip）。
    替代旧字段 query_cache_module（保留为 alias，新代码用 chip_target）。"""
```

**3b.** 在文件末尾（`SENSITIVE_INPUT_BLOCKLIST` 后）追加：

```python
# spec 2026-07-16-tool-result-view-design.md §2.3: 标准 view_type registry
# 启动校验：meta.result_view 必须在此集合内（registry.validate_on_startup）
STANDARD_VIEW_TYPES: frozenset[str] = frozenset(
    {
        "rows_affected",
        "data_list",
        "stats_chart",
        "detail_card",
        "plain_json",
    }
)
```

- [ ] **Step 4: registry.validate_on_startup 加 result_view 校验**

修改 `app/modules/ai/agents/tools/registry.py` 的 `validate_on_startup` 方法，在第 4 步 `missing_dry_run` 校验后追加第 5 步：

```python
        # 5. spec 2026-07-16 §2.4: result_view 必须在 STANDARD_VIEW_TYPES
        from app.modules.ai.agents.tools.meta import STANDARD_VIEW_TYPES  # noqa: PLC0415

        invalid_views = {
            t.meta.name: t.meta.result_view
            for t in self._tools.values()
            if t.meta.result_view not in STANDARD_VIEW_TYPES
        }
        if invalid_views:
            raise ToolRegistryError(
                f"Tools declare invalid result_view (must be in {sorted(STANDARD_VIEW_TYPES)}): "
                f"{invalid_views}."
            )
```

- [ ] **Step 5: 运行测试验证通过**

```bash
python -m pytest tests/modules/ai/test_tool_registry.py -v
```
Expected: 全绿

- [ ] **Step 6: ruff + commit**

```bash
ruff check app/modules/ai/agents/tools/meta.py app/modules/ai/agents/tools/registry.py tests/modules/ai/test_tool_registry.py
ruff format app/modules/ai/agents/tools/meta.py app/modules/ai/agents/tools/registry.py tests/modules/ai/test_tool_registry.py
git add app/modules/ai/agents/tools/meta.py app/modules/ai/agents/tools/registry.py tests/modules/ai/test_tool_registry.py
git commit -m "feat(ai): add result_view + chip_target fields and STANDARD_VIEW_TYPES"
```

---

## Task 3: SSE 事件扩展 + executor 双路径分支

**Files:**
- Modify: `app/modules/ai/agents/hitl/events.py`
- Modify: `app/modules/ai/agents/gateway/executor.py:437-452`（emit 块）+ `:795-805`（_run_tool_fn isinstance 分支）+ `:803`（query_cache_module fallback）+ `:134-156`（_infer_affected_rows 加 ui_audit 参数）
- Test: `tests/modules/ai/test_gateway.py`

- [ ] **Step 1: 写失败测试 — ui 序列化 + chip_target + executor isinstance 分支**

追加到 `tests/modules/ai/test_gateway.py`：

```python
class TestSseSerializesUiAndChipTarget:
    """spec 2026-07-16 §2.5: SSE 协议扩展（ui + chipTarget 字段）。"""

    def test_tool_call_result_serializes_ui(self):
        import json

        from app.modules.ai.agents.gateway.result import UIResult
        from app.modules.ai.agents.hitl.events import (
            ToolCallResultEvent,
            event_to_sse_data,
        )

        ui = UIResult(
            view_type="rows_affected",
            view_data={"count": 2, "ids": ["u1", "u2"]},
            audit={"affected_user_ids": ["u1", "u2"]},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": 2},
        )
        event = ToolCallResultEvent(
            tool="user.batch_delete",
            tool_call_id="tc_test1",
            ok=True,
            duration_ms=230,
            result={"deleted": 2},
            ui=ui,
            affected_rows=2,
        )
        payload = json.loads(event_to_sse_data(event))

        assert payload["ui"]["viewType"] == "rows_affected"
        assert payload["ui"]["viewData"]["count"] == 2
        assert payload["ui"]["audit"]["affected_user_ids"] == ["u1", "u2"]
        assert payload["ui"]["labelKey"] == "ai.tool.user.batch_delete.result"
        assert payload["ui"]["labelParams"] == {"count": 2}

    def test_tool_call_started_serializes_chip_target(self):
        import json

        from app.modules.ai.agents.hitl.events import (
            ToolCallStartedEvent,
            event_to_sse_data,
        )

        event = ToolCallStartedEvent(
            tool="user.count",
            tool_call_id="tc_test2",
            summary="count users",
            args={},
            risk="low",
            trace_id="trace_xxx",
            chip_target="/system/user",
        )
        payload = json.loads(event_to_sse_data(event))
        assert payload["chipTarget"] == "/system/user"

    def test_tool_call_result_without_ui_omits_field(self):
        """ui=None 时序列化后 ui 字段不出现（_compact_json 移除 None）。"""
        import json

        from app.modules.ai.agents.hitl.events import (
            ToolCallResultEvent,
            event_to_sse_data,
        )

        event = ToolCallResultEvent(
            tool="user.batch_delete",
            tool_call_id="tc_test3",
            ok=False,
            duration_ms=10,
            error_code="AI_DATA_SCOPE_VIOLATION",
            error_msg="target not in scope",
        )
        payload = json.loads(event_to_sse_data(event))
        assert "ui" not in payload


class TestExecutorIsinstanceBranch:
    """spec 2026-07-16 §3 决策 11: executor 双路径分支。"""

    async def test_executor_preserves_tool_result_when_business_returns_it(self, db_session):
        """业务方返回 ToolResult 时 executor 不 double-wrap，保留 ui。"""
        from unittest.mock import AsyncMock, MagicMock

        from app.modules.ai.agents.gateway.executor import _run_tool_fn
        from app.modules.ai.agents.gateway.result import ToolResult, UIResult
        from app.modules.ai.agents.tools.registry import RegisteredTool

        # 构造业务函数返回 ToolResult.success(data=..., ui=...)
        async def fake_fn(ctx, **kwargs):
            return ToolResult.success(
                data={"deleted": 2},
                ui=UIResult(
                    view_type="rows_affected",
                    view_data={"count": 2, "ids": ["u1", "u2"]},
                    audit={"affected_user_ids": ["u1", "u2"]},
                ),
            )

        meta = MagicMock()
        meta.name = "test.tool"
        meta.sensitive_output = ()
        meta.readonly = False
        meta.chip_target = None
        meta.query_cache_module = None

        registered = RegisteredTool(meta=meta, fn=fake_fn, dry_run_fn=None)
        # ... 构造 deps / redis_client / log 等（参考现有 test_gateway fixture）
        result = await _run_tool_fn(
            registered=registered,
            args={},
            deps=...,  # fixture
            redis_client=...,  # fixture
            user_id=1,
            tool_call_id="tc_x",
            started_at=...,
            log_id=...,
            args_hash="h",
            # ... 其它必需 kwargs
        )

        assert isinstance(result, ToolResult)
        assert result.data == {"deleted": 2}
        assert result.ui is not None
        assert result.ui.view_type == "rows_affected"

    async def test_executor_wraps_dict_return_fallback(self, db_session):
        """业务方返回 dict 时 executor fallback 包装为 ui=None 的 ToolResult。"""
        # ... 类似上面，但 fake_fn 返回 {"deleted": 2}
        # 断言 result.data == {"deleted": 2}, result.ui is None
        ...
```

注：`executor _run_tool_fn` 内部签名复杂，参考 `tests/modules/ai/test_gateway.py` 现有 fixture 补齐 kwargs。

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest tests/modules/ai/test_gateway.py::TestSseSerializesUiAndChipTarget -v
python -m pytest tests/modules/ai/test_gateway.py::TestExecutorIsinstanceBranch -v
```
Expected: FAIL — `TypeError: ToolCallResultEvent.__init__() got an unexpected keyword argument 'ui'` / `'chip_target'`

- [ ] **Step 3: 修改 events.py 加 ui / chip_target 字段**

修改 `app/modules/ai/agents/hitl/events.py`：

**3a.** 顶部 import 加 `TYPE_CHECKING`：

```python
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.modules.ai.agents.gateway.result import UIResult
```

**3b.** `ToolCallStartedEvent`（line 25-41）加 chip_target 字段：

```python
@dataclass(frozen=True)
class ToolCallStartedEvent:
    """tool 开始执行事件

    chip_target（v1.6+ SR-13）: readonly tool 的 chip 跳转路径（声明式，
    替代前端 CHIP_TARGETS map）。None 表示无 chip。
    """

    tool: str
    tool_call_id: str
    summary: str
    args: dict[str, Any]
    risk: Literal["low", "high", "destructive"]
    trace_id: str
    chip_target: str | None = None
    type: Literal["tool_call_started"] = "tool_call_started"
```

**3c.** `ToolCallResultEvent`（line 44-62）加 ui 字段：

```python
@dataclass(frozen=True)
class ToolCallResultEvent:
    """tool 执行结束事件

    ui（v1.6+ SR-13）: UI 层结果，前端按 ui.view_type 路由标准组件。
    None（ok=False / 业务方未填 / executor fallback）→ 前端 fallback 到 plain_json。
    不进 LLM context（executor 内 strip）。
    """

    tool: str
    tool_call_id: str
    ok: bool
    duration_ms: int
    result: Any = None
    affected_rows: int | None = None
    error_code: str | None = None
    error_msg: str | None = None
    ui: "UIResult | None" = None
    type: Literal["tool_call_result"] = "tool_call_result"
```

**3d.** `event_to_sse_data` 函数内序列化 ui / chip_target：

- `ToolCallStartedEvent` 分支的 payload dict 加：`"chipTarget": event.chip_target,`
- `ToolCallResultEvent` 分支的 payload dict 加：`"ui": _ui_to_dict(event.ui),`

文件末尾加 helper（在 `_dry_run_to_dict` 后）：

```python
def _ui_to_dict(ui: "UIResult | None") -> dict[str, Any] | None:
    """UIResult 转 camelCase dict（None 字段交给 _compact_json 移除）"""
    if ui is None:
        return None
    return {
        "viewType": ui.view_type,
        "viewData": ui.view_data,
        "audit": ui.audit,
        "labelKey": ui.label_key,
        "labelParams": ui.label_params,
    }
```

- [ ] **Step 4: 修改 executor.py — isinstance 双路径 + emit 填 ui/chip_target + affected_rows 优先 ui.audit**

修改 `app/modules/ai/agents/gateway/executor.py`：

**4a.** `_infer_affected_rows`（line 134-155）加 `ui_audit` 参数：

```python
def _infer_affected_rows(
    *,
    dry_run_count: int | None,
    result_data: Any,
    ui_audit: dict[str, Any] | None = None,
) -> int | None:
    """从 dry_run_count / ui.audit / result_data 推断影响行数（决策 6）

    优先级：
      1. dry_run_count（HITL 路径精确算出）
      2. ui.audit.affected_count（业务方显式声明，最权威运行时值）
      3. result_data dict 含 _AFFECTED_ROW_KEYS 任一字段
      4. result_data list → len
      5. None
    """
    if dry_run_count is not None:
        return dry_run_count
    if ui_audit:
        for key in _AFFECTED_ROW_KEYS:
            val = ui_audit.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                return val
    if isinstance(result_data, dict):
        for key in _AFFECTED_ROW_KEYS:
            val = result_data.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                return val
    if isinstance(result_data, list):
        return len(result_data)
    return None
```

**4b.** `_run_tool_fn` 内 tool_fn 调用块（line 795-805）改为 isinstance 双路径：

```python
                tool_ctx = build_tool_context(deps, tool_db, meta)
                # L3 单 tool 超时包装
                raw = await with_l3_timeout(tool_fn(tool_ctx, **args))
                # 决策 11: isinstance 双路径分支
                if isinstance(raw, ToolResult):
                    # 业务方已构造完整 ToolResult（builtin tool 新风格）
                    # 仍要脱敏 data 字段（ui 不脱敏，不进 LLM）
                    raw.data = serialize_for_llm(meta.sensitive_output, raw.data)
                    result = raw
                else:
                    # 业务方返回裸 dict / list / 标量（第三方 tool / 老代码 / fallback）
                    safe_data = serialize_for_llm(meta.sensitive_output, raw)
                    result = ToolResult.success(data=safe_data)  # ui=None，前端 fallback
                # spec §6.5: 成功路径清零失败计数
                await clear_failures(redis_client, user_id, meta.name, args_hash)
                # spec §8.7: readonly tool 写 query_cache 给 chip 跳转用
                # 决策 8: chip_target 优先，query_cache_module 兼容 alias
                cache_module = meta.chip_target or meta.query_cache_module
                if meta.readonly and cache_module and deps.trace_id:
                    _safe_write_query_cache(meta, args, deps, user_id, module=cache_module)
                return result
```

注：`_safe_write_query_cache` 现有签名第 5 个位置参数已是 `module`（参考 executor.py:892）；如有不同需调整。

**4c.** emit tool_call_result 块（line 437-452）填 ui + 优先 ui.audit 推断 affected_rows：

```python
    await _emit(
        deps,
        ToolCallResultEvent(
            tool=meta.name,
            tool_call_id=tool_call_id,
            ok=result.ok,
            duration_ms=duration_ms,
            result=result.data if result.ok else None,
            ui=result.ui if result.ok else None,
            affected_rows=_infer_affected_rows(
                dry_run_count=dry_run_count,
                result_data=result.data if result.ok else None,
                ui_audit=result.ui.audit if result.ok and result.ui else None,
            ),
            error_code=result.error_code if not result.ok else None,
            error_msg=result.error_msg if not result.ok else None,
        ),
    )
```

**4d.** emit tool_call_started 块（搜 `ToolCallStartedEvent` 找位置）加 `chip_target=meta.chip_target`：

```python
    await _emit(
        deps,
        ToolCallStartedEvent(
            tool=meta.name,
            tool_call_id=tool_call_id,
            summary=meta.summary,
            args=args,
            risk=meta.risk,
            trace_id=deps.trace_id,
            chip_target=meta.chip_target,
        ),
    )
```

**4e.** 顶部 import 加 `ToolResult`：

```python
from app.modules.ai.agents.gateway.result import ToolResult
```

- [ ] **Step 5: 运行测试验证通过**

```bash
python -m pytest tests/modules/ai/test_gateway.py::TestSseSerializesUiAndChipTarget tests/modules/ai/test_gateway.py::TestExecutorIsinstanceBranch -v
```
Expected: 全绿

- [ ] **Step 6: 跑全量 ai 回归**

```bash
python -m pytest tests/modules/ai/ -v --tb=short | tail -20
```
Expected: 全绿（isinstance 双路径不破坏现有 dict 返回值的 tool）

- [ ] **Step 7: ruff + commit**

```bash
ruff check app/modules/ai/agents/hitl/events.py app/modules/ai/agents/gateway/executor.py tests/modules/ai/test_gateway.py
ruff format app/modules/ai/agents/hitl/events.py app/modules/ai/agents/gateway/executor.py tests/modules/ai/test_gateway.py
git add app/modules/ai/agents/hitl/events.py app/modules/ai/agents/gateway/executor.py tests/modules/ai/test_gateway.py
git commit -m "feat(ai): serialize UIResult + chipTarget, add executor isinstance branch"
```

---

## Task 4: 迁移 user.count / user.stats / user.distinct（user 模块统计类）

**Files:**
- Modify: `app/modules/system/ai_tools.py:38-160`
- Modify: `tests/modules/ai/test_system_ai_tools.py:104-160`（断言适配 ToolResult）

- [ ] **Step 1: 改 test_system_ai_tools.py 断言**

修改 `tests/modules/ai/test_system_ai_tools.py` 中所有 `TestUserCount` / `TestUserStats` / `TestUserDistinct` 类的断言：

旧风格（line 113-114 等）：
```python
result = await user_count(ctx, filters=None)
assert result == {"count": 3}
```

新风格：
```python
result = await user_count(ctx, filters=None)
assert result.data == {"count": 3}
assert result.ui is not None
assert result.ui.view_type == "plain_json"
assert result.ui.view_data["count"] == 3
```

`user.stats` 断言改为：
```python
result = await user_stats(ctx, group_by="user_gender", filters=None)
assert result.data["groups"] == [{"group": "1", "count": 2}, {"group": "2", "count": 1}]
assert result.ui.view_type == "stats_chart"
assert result.ui.view_data["rows"] == result.data["groups"]
```

`user.distinct` 断言改为：
```python
result = await user_distinct(ctx, field="user_gender")
assert result.data["values"] == ["1", "2"]
assert result.ui.view_type == "plain_json"
```

具体要改的断言行：grep `result == {"count"` / `result == \[` / `result == {"groups"` 找到全部位置。

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest tests/modules/ai/test_system_ai_tools.py -v
```
Expected: FAIL — `AssertionError: assert {"count": 3} == {"count": 3, "ui": None}`（result 是 ToolResult 不是 dict）

- [ ] **Step 3: 改 user_count 函数**

修改 `app/modules/system/ai_tools.py:38-72`：

```python
@ai_tool(
    AiToolMeta(
        name="user.count",
        agent="user_mgmt",
        summary=(
            "Total user count → {'count': N}. For 'how many' / 'total'. "
            "NOT user.stats or user.distinct."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status", "user_gender"),
        chip_target="/system/user",  # 替代 query_cache_module
    )
)
async def user_count(ctx: AiToolContext, filters: dict[str, Any] | None = None) -> ToolResult:
    """统计满足条件的用户数量，仅返回数字

    filters:
        status: '1' (启用) / '0' (禁用)
        user_gender: '0' (未知) / '1' (男) / '2' (女)
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    stmt = select(func.count(User.user_id)).where(*ctx.data_scope.filters)
    for key, value in filters.items():
        stmt = stmt.where(getattr(User, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.user.count.result",
            label_params={"count": count},
        ),
    )
```

- [ ] **Step 4: 改 user_stats 函数**

修改 `app/modules/system/ai_tools.py:78-123`：

```python
@ai_tool(
    AiToolMeta(
        name="user.stats",
        agent="user_mgmt",
        summary=(
            "User distribution → [{group, count}]. For breakdown. "
            "NOT user.count or user.distinct."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status", "user_gender"),
        allowed_group_by=("user_gender", "status"),
        max_groups=20,
        result_view="stats_chart",  # 显式声明（启动校验）
    )
)
async def user_stats(
    ctx: AiToolContext,
    group_by: str | None = None,
    filters: dict[str, Any] | None = None,
) -> ToolResult:
    """按维度分组统计用户数量，返回 [{group, count}]"""
    group_by = validate_group_by_in_whitelist(ctx.tool_meta, group_by)
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    col = getattr(User, group_by)
    stmt = (
        select(col, func.count(User.user_id))
        .where(*ctx.data_scope.filters)
        .group_by(col)
        .order_by(func.count(User.user_id).desc())
        .limit(ctx.tool_meta.max_groups)
    )
    for key, value in filters.items():
        stmt = stmt.where(getattr(User, key) == str(value))

    rows = (await ctx.db.execute(stmt)).all()
    groups = [
        {"group": str(g) if g is not None else "null", "count": c}
        for g, c in rows
    ]
    return ToolResult.success(
        data={"groups": groups},
        ui=UIResult(
            view_type="stats_chart",
            view_data={"rows": groups},
            audit={"total": sum(g["count"] for g in groups)},
            label_key="ai.tool.user.stats.result",
        ),
    )
```

- [ ] **Step 5: 改 user_distinct 函数**

修改 `app/modules/system/ai_tools.py:129-160`：

```python
@ai_tool(
    AiToolMeta(
        name="user.distinct",
        agent="user_mgmt",
        summary=(
            "List distinct field values → ['1','0']. For 'which values'. "
            "NOT user.count or user.stats."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_group_by=("user_gender", "status"),
        max_groups=50,
        chip_target="/system/user",  # 替代 query_cache_module
    )
)
async def user_distinct(ctx: AiToolContext, field: str) -> ToolResult:
    """枚举用户某字段的去重值

    field: user_gender / status（复用 allowed_group_by 作白名单）
    """
    field = validate_field_in_whitelist(ctx.tool_meta, field)

    col = getattr(User, field)
    stmt = (
        select(col)
        .where(*ctx.data_scope.filters)
        .distinct()
        .limit(ctx.tool_meta.max_groups)
    )
    rows = (await ctx.db.execute(stmt)).scalars().all()
    values = [str(v) if v is not None else "null" for v in rows]
    return ToolResult.success(
        data={"values": values},
        ui=UIResult(
            view_type="plain_json",
            view_data={"values": values},
            audit={"count": len(values)},
            label_key="ai.tool.user.distinct.result",
            label_params={"count": len(values)},
        ),
    )
```

- [ ] **Step 6: 文件顶部加 import**

修改 `app/modules/system/ai_tools.py` line 1-15 附近，加：

```python
from app.modules.ai.agents.gateway.result import ToolResult, UIResult
```

- [ ] **Step 7: 运行测试验证通过**

```bash
python -m pytest tests/modules/ai/test_system_ai_tools.py::TestUserCount tests/modules/ai/test_system_ai_tools.py::TestUserStats tests/modules/ai/test_system_ai_tools.py::TestUserDistinct -v
```
Expected: 全绿

- [ ] **Step 8: ruff + commit**

```bash
ruff check app/modules/system/ai_tools.py tests/modules/ai/test_system_ai_tools.py
ruff format app/modules/system/ai_tools.py tests/modules/ai/test_system_ai_tools.py
git add app/modules/system/ai_tools.py tests/modules/ai/test_system_ai_tools.py
git commit -m "feat(ai): migrate user.count/stats/distinct to ToolResult.success with UIResult"
```

---

## Task 5: 迁移 role.count / dept.count（count 类，plain_json）

**Files:**
- Modify: `app/modules/system/ai_tools.py:163-228`

- [ ] **Step 1: 改 test_system_ai_tools.py 中 TestRoleCount / TestDeptCount 断言**

参考 Task 4 Step 1 模式：`assert result == {"count": N}` → `assert result.data == {"count": N}` + `assert result.ui.view_type == "plain_json"`。

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest tests/modules/ai/test_system_ai_tools.py::TestRoleCount tests/modules/ai/test_system_ai_tools.py::TestDeptCount -v
```
Expected: FAIL

- [ ] **Step 3: 改 role_count 函数**

修改 `app/modules/system/ai_tools.py:166-195`：

```python
@ai_tool(
    AiToolMeta(
        name="role.count",
        agent="role_mgmt",
        summary=(
            "Total role count → {'count': N}. For 'how many roles'. "
            "Status filter: '1' enabled / '2' disabled."
        ),
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/role",  # 替代 query_cache_module
    )
)
async def role_count(ctx: AiToolContext, filters: dict[str, Any] | None = None) -> ToolResult:
    """统计角色数量，仅返回数字"""
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    stmt = select(func.count(Role.role_id))
    for key, value in filters.items():
        stmt = stmt.where(getattr(Role, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.role.count.result",
            label_params={"count": count},
        ),
    )
```

- [ ] **Step 4: 改 dept_count 函数**

修改 `app/modules/system/ai_tools.py:201-227`：

```python
@ai_tool(
    AiToolMeta(
        name="dept.count",
        agent="dept_mgmt",
        summary="Total department count → {'count': N}. For 'how many departments'.",
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/dept",  # 替代 query_cache_module
    )
)
async def dept_count(ctx: AiToolContext, filters: dict[str, Any] | None = None) -> ToolResult:
    """统计部门数量，仅返回数字"""
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    stmt = select(func.count(Dept.dept_id))
    for key, value in filters.items():
        stmt = stmt.where(getattr(Dept, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.dept.count.result",
            label_params={"count": count},
        ),
    )
```

- [ ] **Step 5: 运行测试验证通过 + ruff + commit**

```bash
python -m pytest tests/modules/ai/test_system_ai_tools.py::TestRoleCount tests/modules/ai/test_system_ai_tools.py::TestDeptCount -v
ruff check app/modules/system/ai_tools.py
ruff format app/modules/system/ai_tools.py
git add app/modules/system/ai_tools.py
git commit -m "feat(ai): migrate role.count + dept.count to ToolResult.success"
```

---

## Task 6: 迁移 role.list / dept.list（data_list view）

**Files:**
- Modify: `app/modules/system/ai_tools.py:230-356`
- Modify: `tests/modules/ai/test_system_ai_tools.py::TestRoleList / TestDeptList`

- [ ] **Step 1: 改 list 类断言**

`role.list` 返回结构变化：

旧：
```python
result = await role_list(ctx, filters=None, limit=10)
assert result == {
    "total": 3,
    "limit": 10,
    "records": [{"id": "1", "name": "Admin", "code": "admin", "status": "1"}],
}
```

新：
```python
result = await role_list(ctx, filters=None, limit=10)
assert result.data == {
    "total": 3,
    "limit": 10,
    "sample": [...],  # 前 3 条精简（给 LLM）
}
assert result.ui.view_type == "data_list"
assert result.ui.view_data["columns"] == [
    {"key": "id", "label": "ID"},
    {"key": "name", "label": "名称"},
    {"key": "code", "label": "编码"},
    {"key": "status", "label": "状态"},
]
assert len(result.ui.view_data["rows"]) == 3
```

- [ ] **Step 2: 改 role_list 函数**

修改 `app/modules/system/ai_tools.py:244-299`：

```python
@ai_tool(
    AiToolMeta(
        name="role.list",
        agent="role_mgmt",
        summary=(
            "List roles → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use role.count for count-only."
        ),
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/role",
        result_view="data_list",
    )
)
async def role_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """列出角色，返回前 N 条精简字段

    LLM 看 data.{total, limit, sample[3]}（精简，进 prompt cache）；
    前端看 ui.view_data.{columns, rows}（全量 limit 条，渲染 table）。
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
    safe_limit = _coerce_list_limit(limit)

    base = select(Role)
    for key, value in filters.items():
        base = base.where(getattr(Role, key) == str(value))

    total = await ctx.db.scalar(select(func.count()).select_from(base.subquery()))

    rows = (
        (await ctx.db.execute(base.order_by(Role.role_id.asc()).limit(safe_limit)))
        .scalars()
        .all()
    )

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "名称"},
        {"key": "code", "label": "编码"},
        {"key": "status", "label": "状态"},
    ]
    records = [
        {
            "id": str(r.role_id),
            "name": r.role_name,
            "code": r.role_code,
            "status": r.status,
        }
        for r in rows
    ]
    return ToolResult.success(
        data={
            "total": int(total or 0),
            "limit": safe_limit,
            "sample": records[:3],  # 给 LLM 看前 3 条
        },
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": int(total or 0)},
            label_key="ai.tool.role.list.result",
            label_params={"count": int(total or 0)},
        ),
    )
```

- [ ] **Step 3: 改 dept_list 函数**

修改 `app/modules/system/ai_tools.py:302-356`，同 role_list 模式（columns 多一个 parent_id）：

```python
@ai_tool(
    AiToolMeta(
        name="dept.list",
        agent="dept_mgmt",
        summary=(
            "List depts → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use dept.count for count-only."
        ),
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/dept",
        result_view="data_list",
    )
)
async def dept_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """列出部门，返回前 N 条精简字段"""
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
    safe_limit = _coerce_list_limit(limit)

    base = select(Dept)
    for key, value in filters.items():
        base = base.where(getattr(Dept, key) == str(value))

    total = await ctx.db.scalar(select(func.count()).select_from(base.subquery()))

    rows = (
        (await ctx.db.execute(base.order_by(Dept.dept_id.asc()).limit(safe_limit)))
        .scalars()
        .all()
    )

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "名称"},
        {"key": "parent_id", "label": "父部门 ID"},
        {"key": "status", "label": "状态"},
    ]
    records = [
        {
            "id": str(d.dept_id),
            "name": d.dept_name,
            "parent_id": str(d.parent_id) if d.parent_id else None,
            "status": d.status,
        }
        for d in rows
    ]
    return ToolResult.success(
        data={
            "total": int(total or 0),
            "limit": safe_limit,
            "sample": records[:3],
        },
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": int(total or 0)},
            label_key="ai.tool.dept.list.result",
            label_params={"count": int(total or 0)},
        ),
    )
```

- [ ] **Step 4: 运行测试 + ruff + commit**

```bash
python -m pytest tests/modules/ai/test_system_ai_tools.py::TestRoleList tests/modules/ai/test_system_ai_tools.py::TestDeptList -v
ruff check app/modules/system/ai_tools.py tests/modules/ai/test_system_ai_tools.py
ruff format app/modules/system/ai_tools.py tests/modules/ai/test_system_ai_tools.py
git add app/modules/system/ai_tools.py tests/modules/ai/test_system_ai_tools.py
git commit -m "feat(ai): migrate role.list + dept.list to data_list view with sample-for-LLM"
```

---

## Task 7: 迁移 user.batch_delete（rows_affected view）

**Files:**
- Modify: `app/modules/system/ai_tools.py:398-459`
- Modify: `tests/modules/system/test_ai_tools_user_batch_delete.py`

- [ ] **Step 1: 改 test_ai_tools_user_batch_delete.py 断言**

旧：
```python
result = await user_batch_delete(ctx, user_ids=[1001, 1002])
assert result == {"deleted": 2, "user_ids": ["1001", "1002"]}
```

新：
```python
result = await user_batch_delete(ctx, user_ids=[1001, 1002])
assert result.data == {"deleted": 2}
assert result.ui.view_type == "rows_affected"
assert result.ui.view_data["count"] == 2
assert result.ui.view_data["ids"] == ["1001", "1002"]
assert result.ui.audit["affected_user_ids"] == ["1001", "1002"]
```

- [ ] **Step 2: 改 user_batch_delete 函数**

修改 `app/modules/system/ai_tools.py:412-459`：

```python
@ai_tool(
    AiToolMeta(
        name="user.batch_delete",
        agent="user_mgmt",
        summary=(
            "Delete users by IDs / names / phones → {'deleted': N}. "
            "HITL confirms; ambiguous deletes all."
        ),
        required_perms=("system:user:delete",),
        risk="destructive",
        hitl_always=True,
        dry_run_supported=True,
        result_view="rows_affected",
    )
)
async def user_batch_delete(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None = None,
    user_names: list[str] | None = None,
    phones: list[str] | None = None,
) -> ToolResult:
    """Delete users by their identifiers.

    Call this tool immediately when the user requests deletion — the HITL
    (Human-In-The-Loop) confirmation drawer is shown to the user automatically
    by the backend; you should NOT ask the user to confirm via chat text.

    Args:
        user_ids: Snowflake int64 IDs
        user_names: user_name exact matches
        phones: phone exact matches
    """
    users = await _resolve_users(
        ctx, user_ids=user_ids, user_names=user_names, phones=phones
    )
    if not users:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            "未找到匹配用户（字段值不匹配 / 不在可见范围 / 已删除）",
            error_code="AI_BATCH_DELETE_NO_MATCH",
        )

    resolved_ids = [u.user_id for u in users]
    await ensure_targets_in_scope(ctx, user_ids=resolved_ids)

    from app.modules.system.service.user_service import user_service  # noqa: PLC0415

    count = await user_service.batch_delete_users(
        ctx.db, resolved_ids, current_user_id=ctx.user.user_id
    )
    str_ids = [str(i) for i in resolved_ids]
    return ToolResult.success(
        data={"deleted": count},
        ui=UIResult(
            view_type="rows_affected",
            view_data={"count": count, "ids": str_ids},
            audit={"affected_user_ids": str_ids},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": count},
        ),
    )
```

- [ ] **Step 3: 运行测试 + ruff + commit**

```bash
python -m pytest tests/modules/system/test_ai_tools_user_batch_delete.py -v
ruff check app/modules/system/ai_tools.py tests/modules/system/test_ai_tools_user_batch_delete.py
ruff format app/modules/system/ai_tools.py tests/modules/system/test_ai_tools_user_batch_delete.py
git add app/modules/system/ai_tools.py tests/modules/system/test_ai_tools_user_batch_delete.py
git commit -m "feat(ai): migrate user.batch_delete to rows_affected view with affected_user_ids audit"
```

---

## Task 8: 迁移 job.update_cron + file.parse（detail_card + plain_json）

**Files:**
- Modify: `app/modules/job/ai_tools.py`
- Modify: `app/modules/ai/agents/tools/file_tools.py`
- Modify: `tests/modules/job/test_*` 或新建（如有 job.update_cron 测试）
- Modify: `tests/modules/ai/test_file_tool.py`

- [ ] **Step 1: 改 job.update_cron 函数**

修改 `app/modules/job/ai_tools.py:23-55`：

```python
"""job 模块的 AI Tool — spec §11.3"""

# ruff: noqa: PLC0415  inline import 避免循环

from typing import Any

from app.modules.ai.agents.gateway.result import ToolResult, UIResult
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext
from app.modules.job.schemas.job import JobAiUpdate
from app.modules.job.service.job_service import job_service


@ai_tool(
    AiToolMeta(
        name="job.update_cron",
        agent="job_mgmt",
        summary="Update job cron expression → {'ok': true}. HITL required.",
        required_perms=("system:job:edit",),
        risk="high",
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
    )
)
async def job_update_cron(
    ctx: AiToolContext, *, job_id: int, cron_expression: str
) -> ToolResult:
    """更新任务 cron 表达式（白名单字段，spec §11.3）

    Args:
        job_id: 任务 ID
        cron_expression: 新 cron 表达式（如 '*/5 * * * *'）
    """
    # 先读旧 cron（dry_run 阶段已展示，这里再次读用于 audit before/after）
    old_job = await job_service.get_by_id(ctx.db, job_id)
    old_cron = old_job.cron_expression or ""

    data = JobAiUpdate(job_id=job_id, cron_expression=cron_expression)
    job = await job_service.update_for_ai(
        ctx.db, data, current_user=str(ctx.user.user_id)
    )
    new_cron = job.cron_expression or ""
    job_id_str = str(job.job_id)

    return ToolResult.success(
        data={"ok": True, "job_id": job_id_str, "new_cron": new_cron},
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": "定时任务 cron 已更新",
                "fields": [
                    {"label": "任务 ID", "value": job_id_str},
                    {"label": "新 cron", "value": new_cron},
                ],
            },
            audit={"job_id": job_id_str, "before": old_cron, "after": new_cron},
            label_key="ai.tool.job.update_cron.result",
        ),
    )
```

- [ ] **Step 2: 改 file_parse 函数**

修改 `app/modules/ai/agents/tools/file_tools.py:35-92`：

```python
@ai_tool(
    AiToolMeta(
        name="file.parse",
        agent=SHARED_AGENT_CODE,
        required_perms=(),
        risk="low",
        default_enabled=False,
        accepts_file=_accepted_mime_types(),
        summary=(
            "Parse uploaded Excel/CSV → {rows, columns, preview[3]}. "
            "Pass file_id. Raw bytes never enter LLM."
        ),
        readonly=True,
        result_view="plain_json",
    )
)
async def file_parse(
    ctx: AiToolContext,
    file_id: str,
    hint: str = "",  # noqa: ARG001  审计可见，不参与解析逻辑
) -> ToolResult:
    """解析用户上传的文件，返回结构化摘要"""
    from dataclasses import asdict  # noqa: PLC0415

    try:
        file_id_int = int(file_id)
    except (TypeError, ValueError) as e:
        raise BusinessRuleException(
            f"file_id 格式无效: {file_id!r}",
            error_code="AI_FILE_ID_INVALID",
        ) from e

    stmt = select(File).where(
        File.file_id == file_id_int,
        File.del_flag == "0",
    )
    file_record = (await ctx.db.execute(stmt)).scalars().first()
    if file_record is None:
        raise BusinessRuleException(
            f"文件不存在: file_id={file_id}",
            error_code="AI_FILE_NOT_FOUND",
        )

    result: FileParseResult = await parse_file(
        file_path=Path(file_record.file_path),
        mime_type=file_record.mime_type or "",
    )
    parsed = asdict(result)
    rows_count = int(parsed.get("rows") or 0)
    columns = list(parsed.get("columns") or [])
    preview = list(parsed.get("preview") or [])[:3]

    return ToolResult.success(
        data={"rows": rows_count, "columns": columns, "preview": preview},
        ui=UIResult(
            view_type="plain_json",
            view_data={
                "rows": rows_count,
                "columns": columns,
                "preview": preview,
            },
            audit={"rows_parsed": rows_count},
            label_key="ai.tool.file.parse.result",
            label_params={"rows": rows_count},
        ),
    )
```

文件顶部 import 加：

```python
from app.modules.ai.agents.gateway.result import ToolResult, UIResult
```

- [ ] **Step 3: 改测试断言**

`tests/modules/ai/test_file_tool.py` 内断言由 `result = await file_parse(...)` + `assert result["rows"] == N` 改为 `result.data["rows"] == N` + `result.ui.view_type == "plain_json"`。

`tests/modules/job/` 内若有 job.update_cron 测试，同样改。

- [ ] **Step 4: 运行全量回归**

```bash
python -m pytest tests/modules/ai/ tests/modules/job/ tests/modules/system/ -v --tb=short | tail -30
```
Expected: 全绿

- [ ] **Step 5: ruff + commit**

```bash
ruff check app/modules/job/ai_tools.py app/modules/ai/agents/tools/file_tools.py
ruff format app/modules/job/ai_tools.py app/modules/ai/agents/tools/file_tools.py
git add app/modules/job/ai_tools.py app/modules/ai/agents/tools/file_tools.py tests/
git commit -m "feat(ai): migrate job.update_cron (detail_card) + file.parse (plain_json)"
```

---

## Task 9: lint 脚本 — builtin tool 函数强制带 ui=

**Files:**
- Create: `scripts/check_ai_tools_ui.py`
- Modify: `.pre-commit-config.yaml` 或 `pyproject.toml`（pre-commit hooks）
- Test: `tests/scripts/test_check_ai_tools_ui.py` 或 `tests/modules/ai/test_lint_ui.py`

- [ ] **Step 1: 写失败测试 — lint 检测到缺 ui 的 ToolResult.success**

```python
# tests/scripts/test_check_ai_tools_ui.py
"""spec 决策 3 修正：lint 强制 builtin tool 函数返回 ToolResult.success 时带 ui=。"""

import ast

import pytest

from scripts.check_ai_tools_ui import (
    CheckAiToolsUiError,
    check_function_for_missing_ui,
)


class TestCheckFunctionForMissingUi:
    def test_detects_success_without_ui(self):
        """ToolResult.success(data=...) 不带 ui → 报错。"""
        code = '''
async def user_count(ctx):
    return ToolResult.success(data={"count": 5})
'''
        tree = ast.parse(code)
        fn_node = tree.body[0]  # AsyncFunctionDef
        with pytest.raises(CheckAiToolsUiError, match="missing ui="):
            check_function_for_missing_ui(fn_node, file="test.py")

    def test_passes_success_with_ui(self):
        """ToolResult.success(data=..., ui=UIResult(...)) → 通过。"""
        code = '''
async def user_count(ctx):
    return ToolResult.success(
        data={"count": 5},
        ui=UIResult(view_type="plain_json", view_data={"count": 5}),
    )
'''
        tree = ast.parse(code)
        fn_node = tree.body[0]
        check_function_for_missing_ui(fn_node, file="test.py")  # no exception

    def test_ignores_non_toolresult_success(self):
        """ToolResult.failure / 其他函数 不查 ui。"""
        code = '''
async def user_count(ctx):
    return ToolResult.failure("AI_X", "msg")
'''
        tree = ast.parse(code)
        fn_node = tree.body[0]
        check_function_for_missing_ui(fn_node, file="test.py")  # no exception

    def test_ignores_dict_return(self):
        """dict 返回值（executor 兼容路径）不查 ui。"""
        code = '''
async def user_count(ctx):
    return {"count": 5}
'''
        tree = ast.parse(code)
        fn_node = tree.body[0]
        check_function_for_missing_ui(fn_node, file="test.py")
```

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest tests/scripts/test_check_ai_tools_ui.py -v
```
Expected: FAIL — `ImportError: No module named 'scripts.check_ai_tools_ui'`

- [ ] **Step 3: 实现 lint 脚本**

新建 `scripts/check_ai_tools_ui.py`：

```python
"""spec 2026-07-16 决策 3 修正：lint 强制 builtin tool 函数返回 ToolResult.success 时带 ui=。

builtin tool 函数（@ai_tool 装饰）若返回 ToolResult.success 必须显式传 ui=，
否则 break 决策 3（业务方应当 data + ui 都填）。

检查规则（AST 静态分析）：
  - 找到 @ai_tool 装饰的 async def 函数
  - 遍历函数体，找到所有 `return ToolResult.success(...` 语句
  - 检查 keyword args 是否含 `ui=`
  - 缺失 → 报错

不查：
  - 非装饰函数（业务方可能写 helper 函数）
  - ToolResult.failure(...) 调用（错误结果不需要 ui）
  - 裸 dict / list 返回（executor 兼容路径）

用法：
    python scripts/check_ai_tools_ui.py app/modules/

集成 pre-commit：
    - .pre-commit-config.yaml 加 hook，stage: commit
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


class CheckAiToolsUiError(Exception):
    """ToolResult.success 调用缺 ui= 参数"""


# @ai_tool 装饰器识别（按名字，不导入）
_TOOL_DECORATOR_NAMES = {"ai_tool"}


def _has_ai_tool_decorator(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """检测函数是否被 @ai_tool(...) 装饰"""
    for dec in fn.decorator_list:
        # @ai_tool(...) → ast.Call(func=ast.Name(id='ai_tool'))
        # @ai_tool → ast.Name(id='ai_tool')
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id in _TOOL_DECORATOR_NAMES:
            return True
    return False


def _is_tool_result_success_call(node: ast.AST) -> bool:
    """检测 return ToolResult.success(...) 语句"""
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    # ToolResult.success
    if isinstance(func, ast.Attribute) and func.attr == "success":
        if isinstance(func.value, ast.Name) and func.value.id == "ToolResult":
            return True
    return False


def check_function_for_missing_ui(
    fn: ast.AsyncFunctionDef | ast.FunctionDef,
    *,
    file: str,
) -> None:
    """检查单个 @ai_tool 函数内所有 ToolResult.success 调用是否带 ui=

    Raises:
        CheckAiToolsUiError: 若任一 ToolResult.success 缺 ui=
    """
    for node in ast.walk(fn):
        if not _is_tool_result_success_call(node):
            continue
        call: ast.Call = node.value  # type: ignore[assignment]
        kw_names = {kw.arg for kw in call.keywords if kw.arg is not None}
        if "ui" not in kw_names:
            raise CheckAiToolsUiError(
                f"{file}:{fn.lineno}: @ai_tool function '{fn.name}' "
                f"calls ToolResult.success without ui=. "
                f"决策 3 要求 builtin tool 同时填 data + ui。"
            )


def check_file(path: Path) -> list[CheckAiToolsUiError]:
    """检查单个 Python 文件，返回所有错误（不抛，批量收集）"""
    errors: list[CheckAiToolsUiError] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return errors  # 静默跳过（其它 lint 会报）

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not _has_ai_tool_decorator(node):
            continue
        try:
            check_function_for_missing_ui(node, file=str(path))
        except CheckAiToolsUiError as e:
            errors.append(e)
    return errors


def main(argv: list[str]) -> int:
    """CLI 入口：扫描给定路径下所有 .py 文件"""
    paths = [Path(a) for a in argv[1:]] or [Path("app/modules")]
    py_files: list[Path] = []
    for p in paths:
        if p.is_dir():
            py_files.extend(p.rglob("*.py"))
        elif p.is_file() and p.suffix == ".py":
            py_files.append(p)

    all_errors: list[CheckAiToolsUiError] = []
    for f in py_files:
        all_errors.extend(check_file(f))

    if all_errors:
        for e in all_errors:
            print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: 运行测试验证通过**

```bash
python -m pytest tests/scripts/test_check_ai_tools_ui.py -v
```
Expected: 4 passed

- [ ] **Step 5: 跑 lint 验证 10 个 builtin tool 全部合规**

```bash
python scripts/check_ai_tools_ui.py app/modules/system/ai_tools.py app/modules/job/ai_tools.py app/modules/ai/agents/tools/file_tools.py
```
Expected: 退出码 0（Task 4-8 已迁移所有 builtin tool 带 ui）

- [ ] **Step 6: 集成 pre-commit**

查看 `.pre-commit-config.yaml` 现有结构（参考 `check_ai_tools.py` 集成方式），加新 hook：

```yaml
- id: check-ai-tools-ui
  name: check-ai-tools-ui
  entry: python scripts/check_ai_tools_ui.py
  language: system
  pass_filenames: true
  files: ^app/modules/.*ai_tools.*\.py$|^app/modules/ai/agents/tools/.*\.py$
```

- [ ] **Step 7: 跑 pre-commit 验证不 break 已有提交**

```bash
git commit --allow-empty -m "test: pre-commit hook for check-ai-tools-ui"  # 应失败因为没真改动
# 或者直接跑：
pre-commit run check-ai-tools-ui --all-files
```
Expected: 全绿

- [ ] **Step 8: ruff + commit**

```bash
ruff check scripts/check_ai_tools_ui.py tests/scripts/test_check_ai_tools_ui.py
ruff format scripts/check_ai_tools_ui.py tests/scripts/test_check_ai_tools_ui.py
git add scripts/check_ai_tools_ui.py tests/scripts/test_check_ai_tools_ui.py .pre-commit-config.yaml
git commit -m "feat(ai): add check_ai_tools_ui lint for mandatory ui= in builtin tools"
```

---

## Task 10: 前端类型定义 — UIResult discriminated union

**Files:**
- Modify: `src/typings/api/ai.d.ts:190-217`

- [ ] **Step 1: 加 UIResult + 事件字段**

修改 `src/typings/api/ai.d.ts`，在 `ToolCallStartedEvent` 之前插入：

```typescript
    /** spec 2026-07-16 §2.3: 5 种标准 view_type */
    type ViewType = 'rows_affected' | 'data_list' | 'stats_chart' | 'detail_card' | 'plain_json';

    /** rows_affected view_data schema */
    type RowsAffectedViewData = {
      count: number;
      ids?: string[];
    };

    /** data_list view_data schema */
    type DataListViewData = {
      columns: Array<{ key: string; label: string }>;
      rows: Array<Record<string, unknown>>;
    };

    /** stats_chart view_data schema */
    type StatsChartViewData = {
      rows: Array<{ group: string; count: number }>;
    };

    /** detail_card view_data schema */
    type DetailCardViewData = {
      title: string;
      fields: Array<{ label: string; value: string }>;
    };

    /** plain_json view_data schema（自由 dict） */
    type PlainJsonViewData = Record<string, unknown>;

    /** UIResult（spec §2.2）— 前端按 viewType 路由标准组件 */
    type UIResult = {
      viewType: ViewType;
      viewData:
        | RowsAffectedViewData
        | DataListViewData
        | StatsChartViewData
        | DetailCardViewData
        | PlainJsonViewData;
      audit?: Record<string, unknown>;
      labelKey?: string;
      labelParams?: Record<string, unknown>;
    };
```

修改 `ToolCallStartedEvent`（加 `chipTarget?`）和 `ToolCallResultEvent`（加 `ui?`）：

```typescript
    /** spec §8.1: tool_call_started 事件 */
    type ToolCallStartedEvent = {
      type: 'tool_call_started';
      tool: string;
      toolCallId: string;
      summary: string;
      args: Record<string, any>;
      risk: 'low' | 'high' | 'destructive';
      traceId: string;
      /** v1.6+ SR-13: chip 跳转目标（声明式，替代前端 CHIP_TARGETS map） */
      chipTarget?: string | null;
    };

    /** spec §8.1: tool_call_result 事件 */
    type ToolCallResultEvent = {
      type: 'tool_call_result';
      tool: string;
      toolCallId: string;
      ok: boolean;
      durationMs: number;
      result?: any;
      affectedRows?: number | null;
      errorCode?: string;
      errorMsg?: string;
      /** v1.6+ SR-13: UI 层结果，前端按 ui.viewType 路由标准组件 */
      ui?: UIResult;
    };
```

- [ ] **Step 2: typecheck + commit**

```bash
cd F:/code/hohu/hohu-admin-web
pnpm typecheck
git add src/typings/api/ai.d.ts
git commit -m "feat(ai): add UIResult discriminated union + chipTarget/ui to SSE events"
```

---

## Task 11: 前端组件库 — 5 个 view 组件 + registry

**Files:**
- Create: `src/views/ai/chat/modules/tool-views/RowsAffectedView.vue`
- Create: `src/views/ai/chat/modules/tool-views/DataListView.vue`
- Create: `src/views/ai/chat/modules/tool-views/StatsChartView.vue`
- Create: `src/views/ai/chat/modules/tool-views/DetailCardView.vue`
- Create: `src/views/ai/chat/modules/tool-views/PlainJsonView.vue`
- Create: `src/views/ai/chat/modules/tool-views/index.ts`

- [ ] **Step 1: 创建 RowsAffectedView.vue**

```vue
<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

const props = defineProps<{
  data: Api.Ai.UIResult;
}>();

const { t } = useI18n();

const viewData = computed(() => props.data.viewData as Api.Ai.RowsAffectedViewData);
const label = computed(() => {
  if (props.data.labelKey) {
    return t(props.data.labelKey, { count: viewData.value.count, ...props.data.labelParams });
  }
  return `已影响 ${viewData.value.count} 行`;
});
</script>

<template>
  <div class="rows-affected-view">
    <div class="count-badge">{{ viewData.count }}</div>
    <div class="label">{{ label }}</div>
    <details v-if="viewData.ids && viewData.ids.length > 0" class="ids-detail">
      <summary>查看 ID 列表（{{ viewData.ids.length }}）</summary>
      <pre>{{ viewData.ids.join(', ') }}</pre>
    </details>
  </div>
</template>

<style scoped>
.rows-affected-view {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
}
.count-badge {
  background: #10b981;
  color: #fff;
  border-radius: 50%;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
}
.label {
  font-size: 13px;
}
.ids-detail summary {
  cursor: pointer;
  font-size: 12px;
  color: #6b7280;
}
.ids-detail pre {
  background: #f3f4f6;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
  max-height: 100px;
  overflow: auto;
}
</style>
```

- [ ] **Step 2: 创建 DataListView.vue**

```vue
<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  data: Api.Ai.UIResult;
}>();

const viewData = computed(() => props.data.viewData as Api.Ai.DataListViewData);
</script>

<template>
  <div class="data-list-view">
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="col in viewData.columns" :key="col.key">{{ col.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, idx) in viewData.rows" :key="idx">
          <td v-for="col in viewData.columns" :key="col.key">
            <code>{{ row[col.key] ?? '' }}</code>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.data-table th,
.data-table td {
  border: 1px solid var(--n-border-color, #e5e7eb);
  padding: 4px 8px;
  text-align: left;
}
.data-table th {
  background: var(--n-color-target, #f9fafb);
  font-weight: 500;
}
</style>
```

- [ ] **Step 3: 创建 StatsChartView.vue（迁移现有 ChatToolStatsTabs 逻辑）**

参考 `src/views/ai/chat/modules/chat-tool-stats-tabs.vue` 全文（260 行），把 props 从 `data: StatsGroup[]` 改为 `data: Api.Ai.UIResult`，从 `viewData.rows` 读数据：

```vue
<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  data: Api.Ai.UIResult;
}>();

const viewData = computed(() => props.data.viewData as Api.Ai.StatsChartViewData);

// === 复用现有 chat-tool-stats-tabs.vue 的 sort / tab 切换 / bar 渲染逻辑 ===
// 当前 ChatToolStatsTabs 已实现：3 tab（chart / table / raw）+ bar 图 + sort
// 这里只把入口数据从 props.data 改为 viewData.value.rows

type StatsGroup = { group: string; count: number };
const groups = computed<StatsGroup[]>(() => viewData.value.rows);

const totalCount = computed(() =>
  groups.value.reduce((sum, g) => sum + g.count, 0)
);

const sortedGroups = computed(() =>
  [...groups.value].sort((a, b) => b.count - a.count)
);

const activeTab = ref<'chart' | 'table' | 'raw'>('chart');
// ... 现有 ChatToolStatsTabs 的 tab 切换 + bar 渲染逻辑 ...
</script>

<template>
  <!-- 现有 ChatToolStatsTabs template 完整迁移 -->
  <div class="stats-tabs">
    <!-- tabs + bar chart + table + raw json -->
  </div>
</template>

<style scoped>
/* 复制现有 ChatToolStatsTabs 全部样式 */
</style>
```

**实施细节**：实施时打开 `chat-tool-stats-tabs.vue` 全文，复制 `<template>` + `<style>` 块到 StatsChartView.vue，仅改 `<script setup>` 顶部数据来源。

- [ ] **Step 4: 创建 DetailCardView.vue**

```vue
<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';

const props = defineProps<{
  data: Api.Ai.UIResult;
}>();

const { t } = useI18n();
const viewData = computed(() => props.data.viewData as Api.Ai.DetailCardViewData);
const title = computed(() => {
  if (props.data.labelKey) {
    return t(props.data.labelKey, props.data.labelParams);
  }
  return viewData.value.title;
});
</script>

<template>
  <div class="detail-card-view">
    <div class="title">{{ title }}</div>
    <div class="field-grid">
      <template v-for="(f, idx) in viewData.fields" :key="idx">
        <div class="label">{{ f.label }}</div>
        <code class="value">{{ f.value }}</code>
      </template>
    </div>
  </div>
</template>

<style scoped>
.detail-card-view {
  padding: 8px 0;
}
.title {
  font-weight: 500;
  margin-bottom: 8px;
  font-size: 13px;
}
.field-grid {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  font-size: 12px;
}
.label {
  color: #6b7280;
}
.value {
  font-size: 13px;
}
</style>
```

- [ ] **Step 5: 创建 PlainJsonView.vue**

```vue
<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  data: Api.Ai.UIResult;
}>();

const jsonStr = computed(() => {
  try {
    return JSON.stringify(props.data.viewData, null, 2);
  } catch {
    return String(props.data.viewData);
  }
});
</script>

<template>
  <pre class="tool-pre">{{ jsonStr }}</pre>
</template>

<style scoped>
.tool-pre {
  background: #1f2937;
  color: #f3f4f6;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
  font-family: 'JetBrains Mono', 'Cascadia Code', monospace;
  overflow-x: auto;
  margin: 0;
}
</style>
```

- [ ] **Step 6: 创建 index.ts（registry）**

```typescript
import type { Component } from 'vue';
import RowsAffectedView from './RowsAffectedView.vue';
import DataListView from './DataListView.vue';
import StatsChartView from './StatsChartView.vue';
import DetailCardView from './DetailCardView.vue';
import PlainJsonView from './PlainJsonView.vue';

const TOOL_VIEW_REGISTRY: Record<Api.Ai.ViewType, Component> = {
  rows_affected: RowsAffectedView,
  data_list: DataListView,
  stats_chart: StatsChartView,
  detail_card: DetailCardView,
  plain_json: PlainJsonView
};

/** spec 2026-07-16 §3 决策: 按 viewType 路由标准组件；未知 viewType fallback 到 PlainJsonView */
export function resolveToolView(viewType: Api.Ai.ViewType | undefined | null): Component {
  if (!viewType || !(viewType in TOOL_VIEW_REGISTRY)) {
    return PlainJsonView;
  }
  return TOOL_VIEW_REGISTRY[viewType];
}

export { PlainJsonView };
```

- [ ] **Step 7: typecheck + lint + commit**

```bash
cd F:/code/hohu/hohu-admin-web
pnpm typecheck
pnpm lint
pnpm fmt
git add src/views/ai/chat/modules/tool-views/
git commit -m "feat(ai): add 5 view components + resolveToolView registry"
```

---

## Task 12: 改造 chat-tool-call.vue + 删除 chat-tool-stats-tabs.vue

**Files:**
- Modify: `src/views/ai/chat/modules/chat-tool-call.vue`
- Delete: `src/views/ai/chat/modules/chat-tool-stats-tabs.vue`

- [ ] **Step 1: 改 chat-tool-call.vue `<script setup>`**

替换 `chat-tool-call.vue` 的 `<script setup>` 块。具体改动：

**1a. 顶部 import 改造**（line 1-15）：

```typescript
<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { PlainJsonView, resolveToolView } from './tool-views';
import type { Component } from 'vue';
```

删除：`import ChatToolStatsTabs from './chat-tool-stats-tabs.vue';`（已迁移到 StatsChartView，由 resolveToolView 路由）

**1b. 删除三个硬编码 computed**：

删除以下块（line 113-153）：
- `const resultJson = computed(...)` 整段（line 113-120）
- `const statsData = computed<StatsGroup[] | null>(...)` 整段（line 122-134）
- `const CHIP_TARGETS: Record<string, string> = {...}` 整段（line 137-145）
- `const chipTarget = computed<string | null>(...)` 整段（line 147-153）

**1c. 新增 view 组件解析**：

在 `toolDesc` computed 后追加：

```typescript
// spec 2026-07-16 §3: 按 result.ui.viewType 路由标准组件
const viewComponent = computed<Component>(() => {
  if (!props.result?.ok) return PlainJsonView;
  if (!props.result.ui) return PlainJsonView;
  return resolveToolView(props.result.ui.viewType);
});

const viewProps = computed<Api.Ai.UIResult | null>(() => {
  if (!props.result?.ok || !props.result.ui) return null;
  return props.result.ui;
});

// spec 2026-07-16 §3 决策 2: chip 从 started.chipTarget 读（不再硬编码 CHIP_TARGETS）
const chipHref = computed<string | null>(() => {
  if (!props.result?.ok) return null;
  if (!props.started.traceId) return null;
  if (!props.started.chipTarget) return null;
  return `${props.started.chipTarget}?ai_query_id=${encodeURIComponent(props.started.traceId)}`;
});
```

删除原 `chipHref` computed（line 155-158，使用 CHIP_TARGETS 的旧版本）。

**1d. 兜底 plain JSON 渲染**（result.ok=True 但 ui=None 时，老 tool 未迁移）：

```typescript
const fallbackJson = computed(() => {
  if (!props.result?.ok || props.result.ui) return '';
  try {
    return JSON.stringify(props.result.result, null, 2);
  } catch {
    return String(props.result.result);
  }
});
```

- [ ] **Step 2: 改 chat-tool-call.vue `<template>`**

替换 template 中 result 渲染部分（line 245-258）：

```vue
<!-- spec 2026-07-16 §3: 按 viewType 路由标准组件 -->
<div v-if="result && result.ok && viewProps" class="tool-section">
  <div class="tool-section-title">
    数据视图
    <span class="hint">· {{ viewProps.viewType }}</span>
  </div>
  <component :is="viewComponent" :data="viewProps" />
</div>
<!-- 兜底：result.ok 但 ui=None（老 tool 未迁移 / executor fallback 包装） -->
<div v-else-if="result && result.ok && fallbackJson" class="tool-section">
  <div class="tool-section-title">结果摘要</div>
  <pre class="tool-pre">{{ fallbackJson }}</pre>
</div>
```

删除现有 chip 块（line 266-270）替换为：

```vue
<!-- spec 2026-07-16 §3 决策 2: chip 从 started.chipTarget 读 -->
<div v-if="chipHref" class="chip-row">
  <a class="chip-link" :href="chipHref">📊 查看完整数据 →</a>
  <span class="chip-hint">跳转到模块页（已带筛选回放）</span>
</div>
```

- [ ] **Step 3: 删除 chat-tool-stats-tabs.vue**

```bash
rm F:/code/hohu/hohu-admin-web/src/views/ai/chat/modules/chat-tool-stats-tabs.vue
```

逻辑已全部迁移到 `tool-views/StatsChartView.vue`。

- [ ] **Step 4: typecheck + lint**

```bash
pnpm typecheck
pnpm lint
pnpm fmt
```

- [ ] **Step 5: 启动 dev server 手动验证 3 种 view**

```bash
pnpm dev
```

打开 `http://localhost:9527/ai/chat`，分别触发：
1. `user.count` → 看到 PlainJsonView（数字）+ chip 跳转
2. `user.stats` → 看到 StatsChartView（tab + bar 图）
3. `user.batch_delete`（HITL）→ 看到 RowsAffectedView（绿色 count badge + ID list）
4. `role.list` → 看到 DataListView（table）+ chip 跳转
5. `job.update_cron`（HITL）→ 看到 DetailCardView

确认每种 view 渲染正确。

- [ ] **Step 6: commit**

```bash
git add src/views/ai/chat/modules/chat-tool-call.vue
git rm src/views/ai/chat/modules/chat-tool-stats-tabs.vue
git commit -m "feat(ai): render tool result via <component :is> + read chipTarget from SSE"
```

---

## Task 13: i18n keys — tool result labels

**Files:**
- Modify: `src/locales/langs/zh-cn/ai.ts`
- Modify: `src/locales/langs/en-us/ai.ts`

- [ ] **Step 1: 加 zh-cn labels**

在 `src/locales/langs/zh-cn/ai.ts` 合适位置（如 `ai:` 命名空间内）追加：

```typescript
tool: {
  user: {
    count: { result: '共 {count} 个用户' },
    stats: { result: '按维度统计用户分布' },
    distinct: { result: '共 {count} 个不同值' },
    batch_delete: { result: '已删除 {count} 个用户' },
  },
  role: {
    count: { result: '共 {count} 个角色' },
    list: { result: '共 {count} 个角色' },
  },
  dept: {
    count: { result: '共 {count} 个部门' },
    list: { result: '共 {count} 个部门' },
  },
  job: {
    update_cron: { result: '定时任务 cron 已更新' },
  },
  file: {
    parse: { result: '已解析 {rows} 行' },
  },
},
```

- [ ] **Step 2: 加 en-us labels**

在 `src/locales/langs/en-us/ai.ts` 同结构追加：

```typescript
tool: {
  user: {
    count: { result: '{count} users in total' },
    stats: { result: 'User distribution by dimension' },
    distinct: { result: '{count} distinct values' },
    batch_delete: { result: 'Deleted {count} users' },
  },
  role: {
    count: { result: '{count} roles in total' },
    list: { result: '{count} roles' },
  },
  dept: {
    count: { result: '{count} departments in total' },
    list: { result: '{count} departments' },
  },
  job: {
    update_cron: { result: 'Job cron expression updated' },
  },
  file: {
    parse: { result: 'Parsed {rows} rows' },
  },
},
```

- [ ] **Step 3: typecheck + commit**

```bash
pnpm typecheck
git add src/locales/langs/zh-cn/ai.ts src/locales/langs/en-us/ai.ts
git commit -m "feat(ai): add tool result i18n label keys (zh-cn + en-us)"
```

---

## Task 14: 文档回写 — spec + CLAUDE.md + 主 spec §14

**Files:**
- Modify: `docs/specs/2026-07-16-tool-result-view-design.md:3`（状态改 ✅ + 加 Ship 记录块）
- Modify: `docs/specs/2026-07-02-ai-tool-gateway-design.md:2237`（§14 SR-13 改 ✅）
- Modify: `CLAUDE.md`（Common Pitfalls 加 #14）
- Modify: `hohu-admin-web/CLAUDE.md`（AI 章节）

- [ ] **Step 1: spec 头部状态改 ✅ + 加 Ship 记录块**

修改 `docs/specs/2026-07-16-tool-result-view-design.md` 头部（line 1-10）：

```markdown
# Tool Result View Registry（分层 result + 标准 view type） — v1.6+

**Status**: ✅ Plan 已完成（2026-07-26）
**Created**: 2026-07-16
**Completed**: 2026-07-26
**Owner**: hohu core team
**Depends on**: §8.1 SSE 协议（已落地）/ §5.1 AiToolMeta（已落地）
**Related**: [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 / §14 / §22 SR-13

---

## Ship 记录（2026-07-26）

- 后端 9 commit（Task 1-9）+ 前端 5 commit（Task 10-14）
- 测试：Task 1-9 加 ~20 个新单测；全量 1150+ pytest 绿；前端 typecheck 绿
- 10 个 builtin tool 全部迁移到 ToolResult.success(data=..., ui=...)

### Ship-time 决策记录（决策 5-11）

5. **view_type 5 种**（spec §2.3 收窄）：移除 redirect_chip（基于 tool 性质非 result）和 confirmation_summary（时态早于 result）。
6. **chip_target 声明式**（替代 query_cache_module + 前端 CHIP_TARGETS map），旧字段保留 alias。
7. **ToolResult.success(data, *, ui=None)** ui 可选 + lint 强制 builtin tool 函数带 ui（决策 3 修正，避免 break 现有 executor / 测试 / 第三方 tool）。
8. **一次性全迁移 10 个 builtin tool**（user.count/stats/distinct + role.count/list + dept.count/list + user.batch_delete + job.update_cron + file.parse）。
9. **affected_rows 优先级**：dry_run_count > ui.audit.affected_count > _infer_affected_rows 推断。
10. **ui 字段不进 LLM context**（executor isinstance 双路径，business 返回 ToolResult 时仅脱敏 data）。
11. **TS discriminated union 给 view_data 强类型；后端不强校验 view_data schema**。

### 与原 spec 的偏差

| # | spec 原计划 | 实施 | 原因 |
|---|---|---|---|
| 1 | §2.3 view_type 7 种 | 5 种（移除 chip / confirmation） | 范畴错误（chip 基于 tool 性质，confirmation 时态早于 result） |
| 2 | §2.1 ToolResult.data + ui 都必填 | ui 可选 + lint 强制 | 避免 break 现有 executor.py:805 fallback + test_events.py + 第三方 tool；lint 等价约束 builtin tool |
| 3 | §3 Phase 1-3 渐进迁移 | 一次性全迁移 10 tool | 用户决策：避免长期 fallback plain_json 没压力 |
```

- [ ] **Step 2: 主 spec §14 SR-13 标 ✅**

修改 `docs/specs/2026-07-02-ai-tool-gateway-design.md:2237`：

```markdown
| ✅ **分层 tool result + view type registry — 已完成 2026-07-26**（spec [`2026-07-16-tool-result-view-design.md`](./2026-07-16-tool-result-view-design.md) / SR-13）| TOB 开源协作：业务方加 tool 不应改前端代码 | 实施 detail 见 `2026-07-16-tool-result-view-design.md` Ship 记录块 |
```

- [ ] **Step 3: hohu-admin/CLAUDE.md 加 Common Pitfalls #14**

修改 `hohu-admin/CLAUDE.md`，在 #13 后追加：

```markdown
14. **AI builtin tool 函数必须返回 `ToolResult.success(data=..., ui=...)`** — 决策 3：`data` 给 LLM（精简，进 prompt cache，经 `serialize_for_llm` 脱敏），`ui` 给前端（`UIResult(view_type, view_data, audit, label_key, label_params)`，不进 LLM context）。`view_type` 必须在 `STANDARD_VIEW_TYPES`（启动校验）：`rows_affected` / `data_list` / `stats_chart` / `detail_card` / `plain_json`。readonly tool 加 `chip_target="/system/xxx"` 声明式 chip 跳转（替代旧 `query_cache_module`，后者保留 alias）。**lint 强制 builtin tool 函数返回 ToolResult.success 时带 ui=**（`scripts/check_ai_tools_ui.py`，pre-commit 集成）。详见 `docs/specs/2026-07-16-tool-result-view-design.md`。
```

- [ ] **Step 4: hohu-admin-web/CLAUDE.md AI 章节加 view 渲染说明**

修改 `hohu-admin-web/CLAUDE.md` AI 模块章节，加：

```markdown
- **Tool Result View:** `chat-tool-call.vue` 用 `<component :is="resolveToolView(result.ui.viewType)" :data="result.ui" />` 渲染，按 `viewType` 路由到 `tool-views/` 目录下 5 个标准组件之一（rows_affected / data_list / stats_chart / detail_card / plain_json）。新增 tool 时后端声明 `meta.result_view` + `meta.chip_target`，前端无需改动。chip 跳转从 `started.chipTarget` 读（声明式，已删 CHIP_TARGETS 硬编码 map）。
```

- [ ] **Step 5: 后端 commit + 前端 commit**

```bash
cd F:/code/hohu/hohu-admin
git add docs/specs/2026-07-16-tool-result-view-design.md docs/specs/2026-07-02-ai-tool-gateway-design.md CLAUDE.md
git commit -m "docs(ai): mark Tool Result View Registry spec as completed (SR-13)"

cd F:/code/hohu/hohu-admin-web
git add CLAUDE.md
git commit -m "docs(ai): document tool result view component pattern"
```

---

## Self-Review（重写后）

### Spec 覆盖

- ✅ §1 问题描述 → Task 1-13 全部解决（消除 3 处硬编码：CHIP_TARGETS / statsData / resultJson by tool name）
- ✅ §2.1 ToolResult 双层 → Task 1（修正：ui 可选）
- ✅ §2.2 UIResult 结构 → Task 1
- ✅ §2.3 标准 view_type 5 种 → Task 11（5 个 view 组件）
- ✅ §2.4 AiToolMeta.result_view → Task 2
- ✅ §2.5 SSE 协议扩展 → Task 3
- ✅ §3 Phase 1 后端基础 → Task 1-3
- ✅ §3 Phase 2 前端组件库 → Task 10-12
- ✅ §3 Phase 3 迁移现有 tool → Task 4-8（10 个 builtin tool 全覆盖）
- ✅ 决策 3 修正（lint 强制）→ Task 9（新加 lint 脚本，spec 原计划没写）
- ⚠️ §3 Phase 4 审计加强 — 范围外（plan 末尾标注，留下个 spec）
- ✅ §4 范围外 — 不做
- ✅ §5 开放问题全部回答（决策 5-11）

### Placeholder 扫描

- ❌ "TBD" / "TODO" / "appropriate error handling" — 无
- ❌ "Similar to Task N" — 无（Task 5-8 每个 tool 函数完整代码）
- ❌ "迁移现有 ChatToolStatsTabs" — Task 11 Step 3 已展开（实施时复制 template/style）
- ✅ Task 11 Step 3 的"复制现有 ChatToolStatsTabs"是合理的（260 行 vue 不便在 plan 内展开），并明确说明复制什么

### 类型一致性

- `UIResult` dataclass（Task 1）↔ TS UIResult（Task 10）：view_type / view_data / audit / label_key / label_params 一致 ✓
- `ToolResult.success(data, *, ui=None, **meta)`（Task 1）↔ 10 个 tool 函数返回值（Task 4-8）：ui 用 keyword arg ✓
- `STANDARD_VIEW_TYPES`（Task 2）↔ TS ViewType union（Task 10）↔ TOOL_VIEW_REGISTRY keys（Task 11）：5 种一致 ✓
- `event_to_sse_data` 字段（Task 3）↔ TS 事件（Task 10）：viewType/viewData/audit/labelKey/labelParams + chipTarget 一致 ✓
- `executor._run_tool_fn` isinstance 双路径（Task 3）↔ 10 tool 函数返回 ToolResult（Task 4-8）：业务方返回 ToolResult 走 isinstance 分支 ✓
- `_infer_affected_rows(ui_audit=...)` 签名（Task 3）↔ emit 调用（Task 3 Step 4c）：参数名一致 ✓

### 决策 3 修正后的 break range 评估

| 调用点 | 是否 break | 原因 |
|---|---|---|
| `executor.py:805` `ToolResult.success(data=safe_data)` | 不 break | ui=None optional，executor 双路径分支保留 fallback |
| `test_events.py:356/367/387` | 不 break | 测试中 ToolResult.success 不带 ui 仍合法 |
| 10 个 builtin tool 函数 | 不 break（但 lint 要求） | dict 返回值走 executor fallback；新风格返回 ToolResult.success 必须带 ui（Task 9 lint 强制） |

---

## 执行选择

**Plan complete and saved to `docs/superpowers/plans/2026-07-26-tool-result-view-registry.md`. Two execution options:**

**1. Subagent-Driven（推荐）** — 我分派 fresh subagent 逐 task 实施，每 task 后两阶段 review（spec 合规 + 代码质量）。Task 4-8 的 10 个 tool 迁移量适合 subagent 隔离上下文。

**2. Inline Execution** — 本 session 内批量执行，checkpoint review。Task 9 lint 脚本和 Task 11 前端组件库可以批量推进。

**Which approach?**
