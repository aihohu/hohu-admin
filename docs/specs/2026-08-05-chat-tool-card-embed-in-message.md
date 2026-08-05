# Chat Tool Card Embed in Message（工具卡片嵌入消息流） — v1.0

**Status**: 📝 Ready for Plan（决策 2.1-2.10 已闭环；待 Plan mode 拆 Task 1-5 实现细节）
**Created**: 2026-08-05
**Owner**: hohu core team
**Depends on**:
- [`2026-07-16-tool-result-view-design.md`](./2026-07-16-tool-result-view-design.md)（Tool Result View Registry 已 ship，5 种 view_type + DetailCardView 承载 downloadUrl）
- [`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md) §10 Task 33（AI 对话内下载按钮已落地，但渲染位置是当前要重构的「末尾卡片列表」）
- BUG-FE-18 修复（`ai_message.tool_calls` JSON 反序列化 → streamEvents 已工作，本 spec 改渲染位置不改反序列化逻辑）
**Related**:
- [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 SSE 协议（本 spec 不改协议）

---

## 1. Context

### 1.1 现状

`chat-main.vue:303-315` 把所有 tool 卡片渲染在**消息列表末尾**，与消息流平行：

```
[ChatMessage v-for="msg in currentMessages"]   ← 历史消息气泡
[ChatMessage streaming]                         ← 当前流式气泡
[ChatClarification]
[ChatToolCall v-for in toolCallCards]           ← 所有卡片堆这里
[Thinking indicator]
```

`toolCallCards` computed 从 `aiStore.streamEvents` 过滤 `tool_call_started` + 配对 `tool_call_result`。

`streamEvents` 生命周期：
- SSE `tool_call_started` / `tool_call_result` / `confirmation_required` → push
- `sendMessage` 开始新流 → 清空（`store/ai/index.ts:290`）
- `selectConversation` reload → 从 `message.toolCalls` JSON 反序列化重建（BUG-FE-18 修复）

### 1.2 痛点（TOB 开源视角）

1. **审计链断裂** —— 用户周一让 AI 导出用户表，周五合规问「这份数据谁导的、为什么、filter 是什么」。当前 reload 后所有卡片堆末尾，**哪次导出对应哪条 assistant 消息完全看不出来**，违反 spec §2.31「audit chain joinable」精神。

2. **发新消息旧卡片消失** —— 用户连续问「导出用户」+「导出角色」，第二次 sendMessage 清空 streamEvents → 第一次的 user.export 卡片 + 下载按钮**立刻消失**，用户无法对比两次操作。

3. **reload 后多轮卡片堆叠** —— 5 轮对话各调了 1 个 tool，reload 后 5 个卡片全部堆在末尾，用户分不清谁是谁。

4. **违反主流标杆** —— ChatGPT（Code Interpreter / 浏览）、Claude.ai、文心、通义**全部嵌入式**：tool 卡片跟着产生它的那条 assistant 消息。企业用户已形成「一次回答 = 一段文字 + 0..N 个工具调用」的心智模型，开源用户会拿这个标杆要求我们。

5. **Task 33 下载按钮位置是过渡形态** —— 决策 33.6 把下载按钮放在卡片底部 chip-row「常显」，是因为卡片本身就在末尾；嵌入消息流后这个设计仍然成立，但用户能找到正确的那次导出。

### 1.3 触发

2026-08-05 用户实测 Task 33 下载按钮时发现「发新对话卡片消失，只在最后一个对话显示」，确认是 UX 设计问题不是 bug。讨论后判断：当前设计是 MVP 简化，不是终态；TOB 企业级开源必须嵌入式。

---

## 2. 关键设计决策

### 2.1 **嵌入位置：assistant 消息气泡正下方，与文字同级**

每条 assistant 消息可能产生 0..N 个 tool 调用。tool 卡片渲染在该消息气泡**正下方**，视觉上属于这条消息。

```
[user 消息 1: "导出用户"]
[assistant 消息 1: "导出成功..."]
  └─ [user.export 工具卡片 + ⬇ 下载]   ← 跟消息 1 绑定
[user 消息 2: "再导一次"]
[assistant 消息 2: "导出成功..."]
  └─ [user.export 工具卡片 + ⬇ 下载]   ← 跟消息 2 绑定
```

**反例**:
- 渲染在消息气泡**内部**（文字下方同一气泡内）→ 气泡宽度受限（max-w-800px），detail_card 字段 grid + 下载按钮换行难看；与 ChatMessage 组件耦合过深，重构成本高。
- 渲染在消息气泡**上方** → 视觉顺序违反「AI 先说再做」直觉；HITL pending 卡片（时态早于消息）无处安放。
- 渲染在**侧边栏** → 宽度不够，移动端不可用。

**回归**: `chat-main.vue` 渲染循环改：每条 assistant ChatMessage 后紧跟对应的 ChatToolCall 列表（v-for）；`chat-message.vue` 不动（气泡渲染照旧）。

### 2.2 **关联键：assistant message.messageId ↔ tool_call 的隐式归属**

后端 `ai_message.tool_calls` JSON 数组已经持久化在 message 行上（BUG-FE-18 修复已建立反序列化）。嵌入只需在前端按「当前 message 是不是 assistant + 有没有 toolCalls」渲染。

**单 message 内多个 tool 顺序**：按 `tool_calls` JSON 数组原始顺序（后端 `_persist_tool_calls` 按 SSE 事件顺序 append，天然时序正确）。

**反例**:
- 在 tool_call_started SSE 事件里加 `message_id` 字段 → SSE 协议改动 + 后端 executor 要先建 message 再调 tool（当前是先流 tool 事件再 message_save）；改动面大。
- 前端用 traceId 关联 → traceId 是单次工具调用维度，同一 message 多个 tool 调用各自 traceId 不同，无法聚到 message 级。
- 按 toolCallId 时间排序 → toolCallId 是 UUID 无序。

**回归**: 后端 `_persist_tool_calls` 不动；前端 `aiStore.currentMessages[].toolCalls` 已在 BUG-FE-18 反序列化阶段填充；新增 `messageToolCards(message)` helper 从 message.toolCalls 构造 ChatToolCall 所需的 `{started, result, isPending, pendingExpiresAt}` 元组。

### 2.3 **流式期间：嵌入到 streaming message 气泡下方**

SSE 流式过程中，当前 assistant 消息还没 persist（没 messageId），但用户需要立刻看到 tool 卡片（HITL pending / 执行进度）。

**做法**：streaming 期间，`streamEvents` 照旧收集 → 在 streaming message 气泡**下方**渲染 toolCallCards（与现状渲染位置**视觉上**一致，因为 streaming message 就在末尾）。流式结束 `message_save` 事件触发 `currentMessages` append → streamEvents 清空 → 新 persist 的 message 自带 toolCalls，嵌入渲染接管。

**反例**:
- 流式期间也直接写 message.toolCalls → 后端 message 还没 INSERT，没 messageId 关联不到；中间状态（pendingConfirmation）也没有持久化载体。
- 流式期间不显示卡片，结束一次性显示 → 用户看不到 HITL 倒计时 / 执行进度，违反 spec §12 HITL pending 设计。

**回归**: `chat-main.vue` streaming ChatMessage 块下方渲染 `streamEvents` 衍生的 toolCallCards（现状逻辑保留，仅视觉位置从「整个列表末尾」移到「streaming 气泡下方」—— 因为 streaming 气泡本来就在末尾，视觉无差）；`message_save` 事件处理函数加 `streamEvents.value = []` 清空。

### 2.4 **reload 后位置：跳过 streamEvents 直接按 message.toolCalls 嵌入渲染**

现状（BUG-FE-18）：reload 时把 message.toolCalls 反序列化成 streamEvents，再统一在末尾渲染。

新做法：**不再反序列化进 streamEvents**；每条 message 渲染时直接从自己的 toolCalls 构造卡片数据。

**反例**:
- 保留反序列化进 streamEvents + 加 messageId 关联 → 双源（message.toolCalls + streamEvents）同步逻辑复杂；streamEvents 本质是 streaming-time buffer，reload 时不该被填充。
- reload 后仍然堆末尾 → 不解决痛点 1/3。

**回归**: `store/ai/index.ts::selectConversation` 删除 BUG-FE-18 引入的 `restoredEvents` 重建 streamEvents 逻辑（30 行）；`currentMessages` 本身已经含 toolCalls（`fetchGetConversation` 返回），无需额外处理。

### 2.5 **HITL pending 卡片：仍在 streaming message 下方（流式期间特例）**

HITL confirmation_required 事件发生在 tool 执行**前**，此时：
- 当前 assistant message 尚未 persist
- pending 卡片需要内联倒计时 bar（spec §12 场景 4/5）

**做法**：pending 卡片渲染逻辑不变，位置跟流式期间的 tool 卡片一致 —— streaming message 下方。

**反例**: pending 卡片也嵌入到「上一条」assistant 消息 → 时态错乱（pending 属于即将产生的消息，不是上一条）；reload 后 pending 卡片找不到挂载点（pending 状态本来就不会持久化，spec §8.3 续传恢复走另一路径）。

**回归**: `pendingConfirmation` 状态不动；`ChatToolCall` 的 `isPending` / `pendingExpiresAt` prop 透传逻辑不动。

### 2.6 **空 toolCalls 不渲染占位**

`message.toolCalls === undefined || length === 0` 时不渲染任何 tool 卡片容器（连边框都没有）。

**反例**: 渲染空容器 + 「无工具调用」占位 → 视觉噪音；大多数 assistant 消息不调 tool。

**回归**: `messageToolCards(message)` 返回空数组时 chat-main 跳过 v-for。

### 2.7 **Task 33 下载按钮 chip-row 不动**

决策 33.6 已经把下载按钮放在卡片底部 chip-row 常显，嵌入重构后这个设计仍然成立（卡片位置变了，卡片内部布局不变）。

**回归**: `chat-tool-call.vue` 的 downloadAction / handleDownload / chip-row 模板 + 样式**零改动**；本 spec 只动 chat-main.vue + chat-message.vue（如需加 prop 透传）+ aiStore。

### 2.8 **嵌入实现走 chat-main wrapper，不污染 ChatMessage**

每条 assistant 消息在 chat-main 渲染循环里用 wrapper（`<div class="message-group">` 或 `<template v-for>`）包 `<ChatMessage>` + `<ChatToolCall v-for>`，布局职责在 chat-main。ChatMessage 保持纯气泡组件（Markdown / 头像 / 操作按钮），不接 `toolCards` prop、不知道 tool 概念。

**反例**:
- ChatMessage 接 `toolCards` prop 内部渲染 → ChatMessage 从「纯气泡」变「气泡+卡片容器」，职责膨胀；user/system 消息类型没卡片也得传 prop；单测需要 mock toolCards 变复杂；气泡组件应该知道「怎么渲染一条消息」，不该知道「tool 是什么」。
- 抽独立 `<MessageGroup>` 组件包 ChatMessage + 卡片 → 当前只包两个组件，多一层抽象没有收益（YAGNI）；未来要加消息级 action（重生成 / 分享 / 复制）时再抽。

**回归**: `chat-main.vue` 渲染循环改：`<div v-for="msg in currentMessages" class="message-group">` 包 ChatMessage + 该消息对应的 ChatToolCall 列表；`chat-message.vue` props 不加 toolCards；wrapper div 提供 CSS 钩子（卡片左边距与气泡对齐，视觉归组）。

### 2.9 **pending 卡片与已完成卡片严格按时间顺序，pending 不置顶**

流式期间混合渲染（已完成 + pending）按 streamEvents push 顺序（旧→新），pending 卡片 inline 在时序位置。HITL 主提醒走 ChatConfirmationDrawer（强制弹出抽屉），卡片内联倒计时只是辅助提示。

**反例**:
- pending 卡片置顶 → 破坏因果链（用户看不到「AI 先做了 X → 然后做了 Y → 现在要做 Z 需确认」的推进顺序）；与 reload 后顺序不一致（reload 后 pending 已不存在，顺序又是时间序）；用户在两个状态间需要重建心智模型。
- pending 卡片独立渲染到 streaming bubble 上方 → 视觉层级混乱（卡片既属于 tool_call 又被抽离原位置）；流式结束 pending 转 result 时位置跳变（从顶部跳到时序位置），用户注意力被打断。

**回归**: `chat-main.vue` 渲染 streamEvents 衍生卡片时保持原数组顺序（现状已经是，零改动）；reload 后从 message.toolCalls 渲染也是数组顺序（决策 2.2）；两个路径顺序逻辑一致；ChatConfirmationDrawer 仍是 HITL 主入口，不依赖卡片位置。

### 2.10 **长对话性能：v-memo 单行优化，不上虚拟滚动**

100+ 消息场景用 Vue 3.2+ `v-memo` 指令包 message-group，消息内容不变时跳过重渲染。不上虚拟滚动。

**反例**:
- 不优化直接渲染 → 100 条消息 × 平均 2 个卡片 × 每卡片 ~15 DOM 节点 ≈ 3000 DOM 节点，配合 ECharts / ResizeObserver 重渲染成本高；流式期间 streamingText 每次更新触发整个列表重 patch。
- 上虚拟滚动（vue-virtual-scroller） → TOB 管理后台 AI 对话典型 < 50 条消息，虚拟滚动要处理不定高 + 滚动锚点 + streaming 时自动滚底，实现成本 1-2 天 vs 实际收益几乎为零；真到 1000+ 消息再考虑。
- 历史卡片默认折叠 → **已是现状**（`chat-tool-call.vue:96 expanded = ref(false)`），不算新方案；且折叠只减少视觉噪音，DOM 与 Vue 组件实例仍然存在，重渲染成本没省。

**回归**: `chat-main.vue` 在 message-group wrapper 上加 `v-memo="[msg.messageId, msg.toolCalls?.length]"`；StatsChartView 已经做了「折叠时 ResizeObserver 不初始化 ECharts」（spec 2026-07-16 §3 决策 11 反例提到的 bug 修复），折叠卡片成本已经可控；v-memo 一行覆盖剩余重渲染。

---

## 3. 数据流 / 时序图

### 3.1 流式期间

```
SSE: tool_call_started(tc_1)        → streamEvents.push({type:'started', ...})
SSE: tool_call_result(tc_1, ok)     → streamEvents.push({type:'result', ...})
SSE: assistant_token("导出")        → streamingText += "导出"
SSE: assistant_token("成功")        → streamingText += "成功"
... (streaming 持续)

前端渲染：
[messages list]
[streaming bubble: "导出成功..."]
[ChatToolCall tc_1 (from streamEvents)]   ← streaming 气泡下方
```

### 3.2 流式结束

```
SSE: message_save(message_id=m_42, tool_calls=[tc_1])
  → currentMessages.push(assistant message m_42 with toolCalls)
  → streamingText = ""
  → streamEvents = []            ← 本 spec 新增（旧：保留到下次 sendMessage）

前端渲染：
[messages list 包含 m_42]
  ChatMessage m_42 "导出成功..."
  ChatToolCall tc_1 (from m_42.toolCalls)   ← 嵌入 m_42 下方
```

### 3.3 下次 sendMessage

```
streamEvents 已被 message_save 清空，无需再清
streamingText 重置
新 streaming 周期开始
```

### 3.4 Reload 会话

```
fetchGetConversation → currentMessages (含每条 message.toolCalls)
不再走 streamEvents 反序列化
每条 assistant message 直接从自己的 toolCalls 渲染嵌入卡片
```

---

## 4. 影响面

### 4.1 改动文件

| 文件 | 改动 |
|---|---|
| `hohu-admin-web/src/views/ai/chat/modules/chat-main.vue` | 渲染循环改：每条 assistant ChatMessage 后嵌入对应 ChatToolCall；streaming ChatMessage 下方渲染 streamEvents 衍生卡片；删除「整个列表末尾的 tool-call-list」块 |
| `hohu-admin-web/src/store/modules/ai/index.ts` | 删除 `selectConversation` 里的 BUG-FE-18 restoredEvents 重建 streamEvents 逻辑；`message_save` 事件处理加 `streamEvents.value = []`；新增 `messageToolCards(message)` helper（或 export 为纯函数） |
| `hohu-admin-web/src/views/ai/chat/modules/chat-message.vue` | 可能需要在气泡下方加 slot 或 wrapper 让 chat-main 嵌入卡片（实现细节，Task 拆时定） |
| `hohu-admin-web/src/views/ai/chat/modules/__tests__/chat-tool-call-embed.spec.ts`（新） | vitest 覆盖嵌入渲染逻辑 |

### 4.2 不改

- 后端 SSE 协议（事件结构 / 序列化不变）
- 后端 `_persist_tool_calls` / `ai_message.tool_calls` JSON 结构
- `chat-tool-call.vue` 内部（视图 + chip-row + 下载按钮全保留）
- 5 种 view_type 组件（DetailCardView / RowsAffectedView 等）
- HITL 抽屉（`chat-confirmation-drawer.vue`）
- `ChatClarification`（supervisor routing 候选卡片，独立机制）

### 4.3 测试调整

- 现有 `use-export-flow.spec.ts` 等 28 个 vitest 不动（渲染层重构不影响 composable 逻辑）
- e2e `test/e2e/ai-chat.spec.ts`（如存在）需调整 selector 期望位置
- 新增 chat-main 嵌入渲染测试

---

## 5. 测试矩阵

### 5.1 vitest 单测

| 测试 | 验证点 |
|---|---|
| `test_embed_cards_under_assistant_message` | 2 条 assistant 消息各带 1 个 tool_call → 卡片渲染在各自消息下（不堆末尾） |
| `test_embed_empty_tool_calls_no_placeholder` | assistant 消息无 toolCalls → 不渲染任何卡片容器 |
| `test_embed_multiple_tool_calls_in_one_message` | 单条 assistant 消息带 3 个 tool_calls → 3 个卡片按数组顺序渲染在该消息下 |
| `test_streaming_cards_below_streaming_bubble` | 流式期间 streamingText 非空 + streamEvents 有 2 个事件 → 卡片渲染在 streaming 气泡下 |
| `test_message_save_clears_stream_events` | message_save 事件后 streamEvents 清空，新 persist 消息的 toolCalls 接管渲染 |
| `test_reload_renders_cards_in_place` | selectConversation 后 3 条 assistant 消息各自带卡片，无末尾堆叠 |
| `test_pending_hitl_card_during_streaming` | confirmation_required 时 pending 卡片渲染在 streaming 气泡下（带倒计时 bar） |

### 5.2 E2E（Playwright）

| 场景 | 验证 |
|---|---|
| 多轮对话嵌入 | 问「导出用户」→ 问「导出角色」→ 问「统计用户」→ reload → 3 个卡片分别在各自消息下 |
| 流式期间视觉 | 问「导出用户」→ streaming 期间卡片立即出现在 streaming 气泡下 |
| HITL 流程 | 触发 HITL 工具 → pending 卡片在 streaming 气泡下倒计时 → 确认后 result 卡片替换位置不变 |
| Task 33 回归 | user.export 完成 → 卡片在消息下 → 下载按钮可见可点 |

---

## 6. Plan / Task 状态块

### Phase 1（前端渲染重构，本期）

- [ ] Task 1：aiStore 改 — `message_save` 清空 streamEvents + 删除 BUG-FE-18 restoredEvents 重建 + 新增 `messageToolCards` helper（纯函数，便于单测）
- [ ] Task 2：chat-main.vue 渲染循环改 — 每条 assistant 消息后嵌入对应卡片；streaming 气泡下渲染 streamEvents 衍生卡片；删除末尾 tool-call-list 块
- [ ] Task 3：vitest 覆盖 §5.1 7 个测试
- [ ] Task 4：E2E 覆盖 §5.2 4 个场景 + 回归现有 ai-chat e2e
- [ ] Task 5：本 spec §6 状态块改 ✅ + 决策补齐 ship-time 新增的

### Phase 2（推迟）

- [ ] 其他模块（role / dept / job）AI 工具调用嵌入复用 — 无需改动，本次重构完成后自动生效
- [ ] 「重新执行」/「查看完整参数」等卡片级 action — 等真实用户反馈
- [ ] 移动端嵌入布局适配（如需要）

---

## 7. 范围外

- 后端 SSE 协议改动（不需要）
- 后端 `_persist_tool_calls` 改动（不需要）
- HITL 抽屉流程改动（独立机制）
- view_type registry 扩张（不需要）
- 消息编辑 / 重新生成对 tool 卡片的影响（沿用现状：regenerate 清空当前 message 重跑）

---

## 8. 开放问题

无。原 3 个开放问题已在 2026-08-05 brainstorming 后闭环：
- ChatMessage prop vs wrapper → 决策 2.8（chat-main wrapper）
- pending 混合渲染顺序 → 决策 2.9（严格时间顺序）
- 长对话性能 → 决策 2.10（v-memo，不上虚拟滚动）

---

## 9. 关联

- 本 spec 不动 [`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md)（职责分离；Task 33 决策 33.6 的 chip-row 设计仍然成立）
- 演进自 BUG-FE-18 修复（streamEvents 反序列化）；本 spec 完成度高后该修复路径退役
- 主流标杆参考：ChatGPT / Claude.ai / 文心 / 通义 的 tool 卡片嵌入位置
