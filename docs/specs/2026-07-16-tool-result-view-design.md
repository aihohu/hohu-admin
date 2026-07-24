# Tool Result View Registry（分层 result + 标准 view type） — v1.6+

**Status**: ⚠️ Plan v1.6+（待实现，2026-07-16 决策登记）
**Created**: 2026-07-16
**Owner**: hohu core team
**Depends on**: §8.1 SSE 协议（已落地）/ §5.1 AiToolMeta（已落地）
**Related**: [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 / §14 / §22 SR-13

---

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

| view_type | 用途 | 组件（前端） |
|---|---|---|
| `rows_affected` | 写操作影响行数（删除/更新/创建） | "已删除 2 行" 卡片 + 详情展开 |
| `data_list` | 列表查询（table 渲染） | 通用 table + 列定义 |
| `stats_chart` | 统计图表（user.stats 用） | ChatToolStatsTabs（已有） |
| `detail_card` | 单实体详情（user.lookup 用） | 字段 grid + 跳转 |
| `redirect_chip` | 跳转 chip（readonly tool 用） | 现有 chip-row（已有） |
| `confirmation_summary` | HITL dry_run 结果 | 现有抽屉（已有） |
| `plain_json` | fallback（未声明 view_type） | 现有 `<pre>{JSON}</pre>` |

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
- `src/views/ai/chat/modules/tool-views/` 新目录
  - `RowsAffectedView.vue` / `DataListView.vue` / `StatsChartView.vue` / `DetailCardView.vue` / `RedirectChipView.vue` / `PlainJsonView.vue`
- `tool-view-registry.ts` 注册 view_type → 组件
- `chat-tool-call.vue` 改为 `<component :is="viewComponent" :data="result.ui" />`

### Phase 3（迁移现有 tool）
- `user.batch_delete` → `rows_affected`
- `user.count` / `role.count` / `dept.count` → `redirect_chip`
- `user.stats` → `stats_chart`
- `user.distinct` → `data_list`
- `job.update_cron` → `detail_card`

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
