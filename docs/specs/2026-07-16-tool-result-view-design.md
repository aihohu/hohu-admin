# Tool Result View Registry（分层 result + 标准 view type） — v1.6+

**Status**: ✅ Plan 已完成（2026-07-28）
**Completed**: 2026-07-28
**Created**: 2026-07-16
**Owner**: hohu core team
**Depends on**: §8.1 SSE 协议（已落地）/ §5.1 AiToolMeta（已落地）
**Related**: [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 / §14 / §22 SR-13

---

## Ship 记录（2026-07-28）

- 后端 9 commit（Task 1-9）+ 前端 5 commit（Task 10-14）
- 测试：Task 1-9 加 ~25 个新单测；全量 1150+ pytest 绿；前端 typecheck 绿
- 10 个 builtin tool 全部迁移到 ToolResult.success(data=..., ui=...)
- 新增 lint `scripts/check_ai_tools_ui.py`（pre-commit 集成）强制 builtin tool 带 ui=

### Ship-time 决策记录

> **决策 1-4** 见 plan [`docs/superpowers/plans/2026-07-26-tool-result-view-registry.md`](../superpowers/plans/2026-07-26-tool-result-view-registry.md) §设计决策（view_type 5 种 / chip_target 声明式 / `ToolResult.success(data, *, ui=None)` ui 可选 + lint / 一次性全迁移 10 tool）。下方 5-11 是 ship 时新加或细化的决策，按 CLAUDE.md 硬规则 #1 补 `**反例**` + `**回归**`。

5. **view_type 5 种**（`rows_affected` / `data_list` / `stats_chart` / `detail_card` / `plain_json`） — 收窄原 spec v1 §2.3 的 7 种。
   **反例**: 原 spec 含 `redirect_chip`（chip 跳转由 `AiToolMeta.chip_target` 表达，基于 tool 性质非 result）和 `confirmation_summary`（HITL dry_run 阶段产物，时态早于 result）—— 范畴错误。
   **回归**: spec §2.3 表已更新为 5 种（偏差 #1，2026-07-28）；`STANDARD_VIEW_TYPES` frozenset 启动校验拒绝未知 view_type（Task 2 `validate_on_startup`）；前端 `TOOL_VIEW_REGISTRY` keys 与之一一对应（Task 11）。

6. **`AiToolMeta.chip_target` 声明式**（替代 `query_cache_module` + 前端 `CHIP_TARGETS` 硬编码 map） — readonly tool 的 chip 跳转路径由后端 meta 声明，前端从 `tool_call_started.chipTarget` 读。
   **反例**: 前端硬编码 `CHIP_TARGETS: Record<tool_name, path>`，每加一个 readonly tool 都要改前端代码 + 发版，破坏 TOB 开源协作"零侵入"原则。
   **回归**: `chat-tool-call.vue` 已删 `CHIP_TARGETS` map（Task 12）；`query_cache_module` 字段保留为 alias（`meta.chip_target or meta.query_cache_module`，executor.py:825）；新加 readonly tool 不带 `chip_target` 时 chip 不显示（行为定义明确）。

7. **`ToolResult.success(data, *, ui: UIResult | None = None)` ui 可选 + lint 强制 builtin tool 函数带 ui=**（决策 3 修正）。
   **反例**: ui 必填 → break `executor.py:818` fallback `ToolResult.success(data=safe_data)`（业务方返回 dict 走兼容路径）+ `test_events.py:356/367/377` 现有 fixture + 第三方 tool 无法预期 UIResult 结构。
   **回归**: `scripts/check_ai_tools_ui.py` lint（AST 静态分析，pre-commit 集成）扫描 `@ai_tool` 装饰函数内所有 `ToolResult.success(...)` 调用，缺 `ui=` 报错（Task 9）；executor isinstance 双路径保留 dict 返回值的 fallback 包装。

8. **一次性全迁移 10 个 builtin tool**（user.count/stats/distinct + role.count/list + dept.count/list + user.batch_delete + job.update_cron + file.parse）。
   **反例**: 渐进迁移（spec §3 Phase 1-3）导致 tool 视觉不一致（一半新 view 一半 plain_json）+ 长期 fallback 没压力，业务方拖延迁移。
   **回归**: `scripts/check_ai_tools_ui.py` pre-commit hook 锁死回退路径（任何 builtin tool 重构掉 ui= 立即报错）；Task 4-8 测试断言全部要求 `result.ui.view_type` 字段。

9. **`affected_rows` 优先级**：`dry_run_count` > `ui.audit.affected_count` > `_infer_affected_rows` 推断。
   **反例**: 双源不一致（dry_run_count vs ui.audit vs result_data dict 推断）→ 卡片显示两个不同数字，用户困惑。
   **回归**: `_infer_affected_rows(*, dry_run_count, result_data, ui_audit=None)` 函数签名强制优先级顺序（executor.py:134-156）；`test_gateway.py::TestInferAffectedRows` 覆盖三档优先级（Task 3）。

10. **`ui` 字段不进 LLM context**（executor isinstance 双路径，business 返回 ToolResult 时仅脱敏 data，ui 不经 `serialize_for_llm`）。
    **反例**: ui 进 prompt → 浪费 token + audit 字段（`affected_user_ids` / `before` / `after`）泄漏给 LLM，违反"LLM 看精简、UI 看丰富"分层原则。
    **回归**: `executor._run_tool_fn` isinstance 分支仅调 `serialize_for_llm(meta.sensitive_output, raw.data)`，ui 字段直通 SSE（executor.py:810-814）；`test_gateway.py::TestSerializeForLlm` 断言 ui 不进 LLM 序列化路径。

11. **TS discriminated union 给 view_data 强类型；后端不强校验 view_data schema**。
    **反例**: 后端 Pydantic 校验 view_data schema → 业务方写错启动失败，过度严苛（数据 shape 多样，强约束挡住迭代）；前端无类型 → view 组件 props 写错运行时崩。
    **回归**: 前端 `ai.d.ts` 用 `RowsAffectedViewData | DataListViewData | StatsChartViewData | DetailCardViewData | PlainJsonViewData` union（Task 10）；后端 `UIResult.view_data: dict[str, Any]` 保持灵活（Task 1），业务方写错只影响自家 tool 渲染，不阻塞服务启动。

12. **Chip 跳转用 `<router-link>` 而非 `<a :href>`**（2026-07-29 hotfix） — chip 链接用 Vue Router SPA 导航，不触发整页刷新；不加 `target="_blank"`，依赖浏览器原生 cmd/ctrl+click 给"想新窗口"的用户。
    **反例**: `<a :href="/system/user?ai_query_id=...">` 触发浏览器整页跳转 → Vue app 重新 bootstrap、Pinia store 全部 reset、chat 历史 / SSE 流 / HITL pending 状态丢失；用户视觉上"页面闪一下"。
    **回归**: `chat-tool-call.vue` chip 行用 `<router-link :to="chipHref">`；`global-tab/index.vue:170-175` 的 `watch(route.fullPath)` 自动把新路由加入 in-app tab 栏，原 chat tab 保留可切回；`user.stats` meta 补 `chip_target="/system/user"`（之前漏声明导致图表 tool 无 chip）。

13. **`user.stats` 也声明 `chip_target="/system/user"`**（2026-07-29 hotfix） — `stats_chart` view_type 的 tool 同样需要 chip 跳转入口，跟 `user.count` / `user.distinct` 对齐。
    **反例**: `user.stats` meta 只声明 `result_view="stats_chart"` 漏 `chip_target` → 图表渲染正常但 chip 不显示，用户问"统计启用的用户按性别分布"后无法跳到详情列表回放筛选。
    **回归**: `app/modules/system/ai_tools.py` `user.stats` meta 加 `chip_target="/system/user"`；`executor.py:832-836` 自动写 query_cache（filters 经 `allowed_filters` 白名单过滤，不含 `group_by` —— list 页无分组概念）；用户点 chip 跳 `/system/user?ai_query_id=<trace_id>` 回放 `status=1` 筛选（性别维度由图表本身承载，list 页只筛选不分组，符合直觉）。

## 1. Context

### 1.1 问题

当前 `tool_call_result.result: Any`（spec §8.1）是 free-form dict，前端 `chat-tool-call.vue` 渲染策略是混合模式：

1. **通用 fallback**（line 243-249）：所有 tool 默认走 `resultJson` 路径 — `JSON.stringify(result, null, 2)` + `<pre>` 黑底等宽字体
2. **by tool name 硬编码特殊渲染**：
   - `user.stats` → `ChatToolStatsTabs`（line 236-242，hard-coded by tool name）
   - readonly tool → `CHIP_TARGETS` map（line 130-136，`user.count → /system/user` 等硬编码）

### 1.2 痛点（TOB 开源视角）

- **协作差**：业务方每加一个 tool，要么接受丑陋 JSON 显示，要么改前端代码加 special case（破坏开源协作的"零侵入"原则）
- **LLM 上下文污染**：result 同时给 LLM 看（进 prompt）和 UI 看（渲染），无法分别优化；审计字段（`affected_user_ids` 等）进 prompt 浪费 token + 注入风险
- **i18n 缺失**：result label 硬编码英文/中文，无法国际化
- **审计无结构**：result 是 free-form，无法统一查询"哪些操作影响了用户 X"
- **类型不安全**：result schema 无校验，business 方随便写

### 1.3 触发

主 spec §22 SR-13 决策（2026-07-16）：v1.6+ 拆 `ToolResult` 双层 + 标准 view type registry。

---

## 2. 关键设计决策（待 brainstorming 细化）

### 2.1 ToolResult 双层

```python
@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any = None                 # LLM 层：精简 dict，进 prompt cache
    error_code: str | None = None
    error_msg: str | None = None
    ui: UIResult | None = None       # UI 层：丰富，不进 LLM prompt
```

### 2.2 UIResult 结构

```python
@dataclass(frozen=True)
class UIResult:
    view_type: str                   # 标准 registry key（启动校验）
    view_data: dict[str, Any]        # view_type 对应组件的 props
    audit: dict[str, Any] | None     # 标准化审计字段（affected_user_ids 等）
    label_key: str | None = None     # i18n key（如 ai.tool.user.batch_delete.result）
    label_params: dict[str, Any] | None = None  # i18n 插值参数
```

### 2.3 标准 view_type registry

> **Note**: 原 spec v1 列 7 种 view_type，ship 时收窄为 5 种（决策 5 / 偏差 #1，2026-07-28）。`redirect_chip`（基于 tool 性质非 result）和 `confirmation_summary`（时态早于 result）范畴错误，已移除。chip 跳转改由 `AiToolMeta.chip_target` 声明式字段表达（决策 6），与 `view_type` 解耦；HITL dry_run 结果仍走现有抽屉，不进 result 渲染流。

| view_type | 用途 | 组件（前端） |
|---|---|---|
| `rows_affected` | 写操作影响行数（删除/更新/创建） | "已删除 2 行" 卡片 + 详情展开 |
| `data_list` | 列表查询（table 渲染） | 通用 table + 列定义 |
| `stats_chart` | 统计图表（user.stats 用） | StatsChartView（迁移自 ChatToolStatsTabs） |
| `detail_card` | 单实体详情（job.update_cron 返回值用） | 字段 grid + 跳转 |
| `plain_json` | fallback（未声明 view_type / 纯数字结果 / executor 兼容路径） | `<pre>{JSON}</pre>`（含 chip 跳转，chip_target 声明式） |

### 2.4 AiToolMeta 声明

```python
@ai_tool(AiToolMeta(
    name="user.batch_delete",
    result_view="rows_affected",   # 启动校验：必须在标准 registry
    ...
))
```

### 2.5 SSE 协议扩展（向后兼容）

`tool_call_result` 事件追加 `ui` 字段：

```json
{
  "type": "tool_call_result",
  "toolCallId": "tc_xxx",
  "ok": true,
  "result": {"deleted": 2, "ok": true},
  "ui": {
    "viewType": "rows_affected",
    "viewData": {"count": 2, "ids": [...]},
    "audit": {"affected_user_ids": [...]},
    "labelKey": "ai.tool.user.batch_delete.result",
    "labelParams": {"count": 2}
  },
  "durationMs": 230,
  "affectedRows": 2
}
```

旧客户端忽略 `ui` 字段，新客户端按 `viewType` 路由。

---

## 3. 实施 outline（待 brainstorming 细化）

### Phase 1（后端基础）
- `app/modules/ai/agents/gateway/result.py` 加 `UIResult` dataclass
- `ToolResult` 加 `ui: UIResult | None` 字段
- `AiToolMeta` 加 `result_view: str = "plain_json"` + 启动校验
- `event_to_sse_data` 序列化 `ui` 到 `tool_call_result.ui`
- `executor.py::serialize_for_llm` strip 掉 `ui` 字段再给 LLM

### Phase 2（前端组件库）
- `src/views/ai/chat/modules/tool-views/` 新目录（5 个组件，对应 5 种 view_type）
  - `RowsAffectedView.vue` / `DataListView.vue` / `StatsChartView.vue` / `DetailCardView.vue` / `PlainJsonView.vue`
- `tool-views/index.ts` 注册 view_type → 组件 + `resolveToolView(viewType)` fallback 到 PlainJsonView
- `chat-tool-call.vue` 改为 `<component :is="viewComponent" :data="result.ui" />` + chip 从 `started.chipTarget` 读（声明式，已删 CHIP_TARGETS 硬编码 map）

### Phase 3（迁移现有 tool）

> **Note**: ship 时一次性迁移全部 10 个 builtin tool（决策 8 / 偏差 #3），未走 Phase 1-3 渐进路径。下方映射对应实际落地的 view_type + chip_target 组合。

- `user.batch_delete` → `rows_affected`
- `user.count` / `role.count` / `dept.count` → `plain_json`（数字结果） + `chip_target="/system/user|role|dept"`（声明式 chip 跳转）
- `user.stats` → `stats_chart`
- `user.distinct` → `plain_json`（数字列表） + `chip_target="/system/user"`
- `role.list` / `dept.list` → `data_list` + `chip_target="/system/role|dept"`
- `job.update_cron` → `detail_card`
- `file.parse` → `plain_json`

### Phase 4（审计加强）
- `audit` 字段写 `ai_operation_log.result_summary`（结构化 JSON）
- 后台审计页按 `affected_user_ids` 等字段反查

---

## 4. 范围外（v2+）

- 业务方注册自定义 view_type + Vue 组件（plugin 机制）
- `view_data` 内 schema 强类型（每个 view_type 独立 TS 类型 + Pydantic 校验）
- 前端 i18n 缺失 key 自动 fallback 到 backend msg
- view_type 版本化（`rows_affected.v2` 等）

---

## 5. 开放问题（待 brainstorming）

- `ui.audit` 字段是否要标准化 schema？还是 keep flexible？
- `view_data` 强类型怎么实现（Pydantic 模型 per view_type？还是 TS 接口？）
- 迁移现有 tool 时如何处理 LLM prompt 变化（result 精简后 LLM 行为可能不同）
- 历史 `ai_message.tool_calls` JSON 是否需要迁移（已有数据的 result 没 `ui` 字段）
