# Chat Tool Card Embed in Message（工具卡片嵌入消息流） — v1.2

**Status**: 📝 Ready for Plan（Safety 前置已完成；ADR-0002 confirmation projection 与消息归属/耐久性待 §6）
**Created**: 2026-08-05
**Updated**: 2026-08-07
**Owner**: hohu core team
**Depends on**:
- [`2026-07-16-tool-result-view-design.md`](./2026-07-16-tool-result-view-design.md)（Tool Result View Registry 已 ship，5 种 view_type + DetailCardView 承载 downloadUrl）
- [`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md) §10 Task 33（AI 对话内下载按钮已落地，但渲染位置是当前要重构的「末尾卡片列表」）
- BUG-FE-18 修复（`ai_message.tool_calls` JSON 已可反序列化；本 spec 将历史消息恢复路径从 `streamEvents` 迁回 `message.toolCalls`）
**Related**:
- [`../adr/0001-ai-safety-consistency-before-deferred-execution.md`](../adr/0001-ai-safety-consistency-before-deferred-execution.md)（执行事实、消息投影与当前同步边界）
- [`../adr/0002-gateway-owned-confirmation-flow.md`](../adr/0002-gateway-owned-confirmation-flow.md)（结构化 confirmation 与 PreparedAction）
- [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 / §8.8（本 spec 不新增事件类型；定义客户端投影与恢复）
- [`2026-08-06-ai-message-edit-semantics.md`](./2026-08-06-ai-message-edit-semantics.md)（active message 投影与编辑/重新生成安全语义）

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

当前流结束后的真实链路不是 `message_save` SSE：前端收到 `done` / 连接关闭后，先把 `streamingText` 追加为临时 assistant 消息，再调用 `fetchGetConversationDetail` 用后端持久消息替换本地临时消息。后端当前又在 emit `done` **之后**才保存并 commit assistant 消息，因此 detail refresh 存在读不到刚完成消息的竞态。

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

**单 message 内多个 tool 顺序**：按 `tool_call_started` 的发生顺序，不按 `tool_call_result` 的完成顺序。后端在收到 started 时分配递增 ordinal / 有序槽位，result 到达时按 `tool_call_id` 回填；持久化前按 started ordinal 输出 `tool_calls` JSON。并行工具即使后启动的先完成，也不能在历史卡片中越过先启动的工具。

**反例**:
- 在 tool_call_started SSE 事件里加 `message_id` 字段 → SSE 协议改动 + 后端 executor 要先建 assistant message 再调 tool（当前 assistant message 在 run 结束时才持久化）；改动面大。
- 前端用 traceId 直接决定卡片归属 → traceId 是一次 ChatCommand/run 的观测与幂等收口键，不是持久 message owner；最终归属仍由 `message.tool_calls` 表达。traceId 只用于确认 detail handoff 命中了本轮持久消息，不能替代 message 行。
- 按 toolCallId 时间排序 → toolCallId 是 UUID 无序。
- 在 `tool_call_result` 到达时直接 append → 并行工具按完成速度排序，reload 后会改变用户在 streaming 阶段看到的因果顺序。

**回归**: 调整 `chat.py::_record_tool_event` 的内存收集方式，但不改变 `ai_message.tool_calls` JSON 对外结构；前端 `aiStore.currentMessages[].toolCalls` 直接使用后端数组顺序；新增 `messageToolCards(message)` helper 从 message.toolCalls 构造 ChatToolCall 所需的 `{started, result, isPending, pendingExpiresAt}` 元组。

### 2.3 **流式期间：嵌入到 streaming message 气泡下方**

SSE 流式过程中，当前 assistant 消息还没 persist（没 messageId），但用户需要立刻看到 tool 卡片（HITL pending / 执行进度）。

**做法**：

1. 发起请求前，维护中的 Web 客户端生成不可预测且稳定的 `traceId`（`tr_` + 32 hex）并放入 ChatCommand；后端校验格式、conversation owner 和 run-key 冲突后将它作为本轮 `trace_id`。因此即使响应 body 尚未收到，客户端也已持有 detail reconciliation key。旧客户端未传时后端可生成并通过 `X-AI-Trace-ID` 暴露，但本期 Web 路径不得依赖该兼容分支。
2. `streaming`：`streamEvents` 收集并作为**唯一渲染源**，卡片显示在 streaming message 气泡下方。
3. 正常结束前，后端通过共享 finalizer 保存 assistant 消息并 commit（详见决策 2.10），然后 emit 既有 `done` 类型的 durability ack：`{type:"done", traceId, messageId, persistence:"committed", projection:"updated"}`。协议中不存在也不新增 `message_save` 事件；只为 `done` 增加可选字段，不新增事件类型。
4. `awaiting_sync`：收到 terminal `done` / 连接关闭后，把本轮文本及工具卡快照放入本地临时 assistant message；tool-only 轮次也生成临时 message group，但不显示空文字气泡。此时卡片只从 temp message snapshot 渲染，`streamEvents` 可留作不可见 recovery buffer，**不得再次参与 DOM 渲染**。
5. 前端调用 `fetchGetConversationDetail`，`MessageOut` 必须返回 `traceId`。有 committed done 时优先以 `done.messageId + traceId` 匹配；若 DB 已 commit 但连接在 ack 前断开，则用请求前已知的 `traceId + pendingCommand + expected tool_call_ids` 匹配 active assistant。匹配消息覆盖本轮预期 tool IDs（纯文本轮允许空集合）后，才原子替换本地临时 projection、清 recovery buffer 并进入 `persisted`。
6. `projection` 表示本 ChatCommand 是否已改变 durable active history，不等同于“assistant 是否保存成功”。`projection="unchanged"` 是 regenerate 非成功或 mutation 前拒绝/clarification 的显式结果：detail 仍包含原 active history，前端撤销本轮临时 projection；regenerate 恢复旧回答，不要求找到新 trace 的 active assistant。
7. `ai_error + done(persistence="failed")` 不得把 `streamCompleted` 置为成功，也禁止自动重跑 tool。若 send/edit 的 source/replacement user mutation 已 commit，则返回 `projection="updated", messageId=null`；detail 必须按 request trace 找到 active user/replacement 后收敛，并展示“回复未持久化”，不得恢复 edit 的旧 suffix。regenerate 或 mutation 前失败返回 `projection="unchanged"`。其他情况下 detail refresh 失败或缺少该 projection 所要求的 durable message 时进入 `stale`：保留 temp snapshot 与不可见 buffer，阻止下一 ChatCommand。

**反例**:
- 流式期间也直接写 message.toolCalls → 后端 message 还没 INSERT，没 messageId 关联不到；中间状态（pendingConfirmation）也没有持久化载体。
- 流式期间不显示卡片，结束一次性显示 → 用户看不到 HITL 倒计时 / 执行进度，违反 spec §12 HITL pending 设计。
- 收到 `done` 就无条件清空 `streamEvents` → detail refresh 失败或读到旧快照时，工具卡无任何可恢复来源。
- 连接关闭后同时从 temp message 和 streamEvents 渲染 → 同一 tool_call 出现两张卡，状态更新顺序也会漂移。
- 用不存在的 `message_save` 事件作为交接点 → 实现与既有 SSE 协议脱节，并掩盖“done 早于 commit”的竞态。
- 只从 terminal done 取得 run key → commit 已成功但 ack 丢失时，客户端无法在 detail 中识别本轮消息并永久 stale。

**回归**: `chat-main.vue` / aiStore 实现 `streaming → awaiting_sync → persisted | stale` 单一渲染状态机；连接关闭后 streamEvents 只作 recovery buffer。Web 在请求前生成 traceId，detail 返回 traceId；有 ack 走 `messageId/traceId` 快路径，无 ack 走 request traceId + tool IDs 接管；`projection=unchanged` 收敛到旧 regenerate 回答。失败保留 temp projection 并阻止下一 command。后端正常完成顺序改为 finalizer save/merge → commit → durability `done`。

### 2.4 **reload 后位置：跳过 streamEvents 直接按 message.toolCalls 嵌入渲染**

现状（BUG-FE-18）：reload 时把 message.toolCalls 反序列化成 streamEvents，再统一在末尾渲染。

新做法：**不再反序列化进 streamEvents**；每条 active message 渲染时直接从自己的 toolCalls 构造卡片数据。`streamEvents` 只表示当前流；流关闭后若为接管失败保留，也只是不可见 recovery buffer，渲染权已经交给 temp message snapshot。

**反例**:
- 保留反序列化进 streamEvents + 加 messageId 关联 → 双源（message.toolCalls + streamEvents）同步逻辑复杂；streamEvents 本质是 streaming-time buffer，reload 时不该被填充。
- reload 后仍然堆末尾 → 不解决痛点 1/3。

**回归**: `store/ai/index.ts::selectConversation` 删除 BUG-FE-18 引入的 `restoredEvents` 重建 streamEvents 逻辑（约 30 行）；`currentMessages` 本身已经含 toolCalls（`fetchGetConversationDetail` 返回），无需额外处理。配合消息编辑 spec，常规 conversation detail 只返回 active messages；被软删除的 assistant message 不进入渲染循环，其 `tool_calls` 卡片自然一并隐藏，审计数据仍留在数据库。

### 2.5 **HITL pending 卡片：流式时属于 transient assistant group，reload 后由 PreparedAction 恢复**

HITL confirmation_required 事件发生在 tool 执行**前**，此时：
- 当前 assistant message 尚未 persist
- pending 卡片需要内联倒计时 bar（spec §12 场景 4/5）

**做法**：流式期间 pending 卡片仍在 transient assistant group 下；Gateway action 进入 `pending_confirmation` 后，conversation detail 的 `pendingActions` 成为刷新恢复来源。reload 时若该 trace 尚无持久 assistant message，`displayMessages` 在 source user message 后派生一个 transient pending assistant group，展示 action.presentation 与倒计时；它不是伪造的 `AiMessage`，终态 detail 接管后自动消失。

**反例**: pending 卡片嵌入上一条 assistant → 归属错误；只存在 Pinia `pendingConfirmation` → 刷新/切换会话后丢失；为 pending 预先 INSERT 一个假 assistant message → 终态 finalizer 需要替换/合并第二种消息模型，扩大双写风险。

**回归**: store 以 `pendingActionsById` 为持久投影缓存，SSE event 与 detail 都按 actionId upsert；streaming、reload、SSE 接管三条路径最多渲染一张 pending 卡。terminal detail 出现 execution toolCallId 后移除 transient group。

### 2.6 **空 toolCalls 不渲染占位**

`message.toolCalls === undefined || length === 0` 时不渲染任何 tool 卡片容器（连边框都没有）。

**反例**: 渲染空容器 + 「无工具调用」占位 → 视觉噪音；大多数 assistant 消息不调 tool。

**回归**: `messageToolCards(message)` 返回空数组时 chat-main 跳过 v-for。

### 2.7 **Task 33 下载按钮 chip-row 不动**

决策 33.6 已经把下载按钮放在卡片底部 chip-row 常显，嵌入重构后这个设计仍然成立（卡片位置变了，卡片内部布局不变）。

**回归**: `chat-tool-call.vue` 的 downloadAction / handleDownload / chip-row 模板 + 样式**零改动**；前端布局只改 `chat-main.vue` + aiStore，`chat-message.vue` 继续保持纯气泡组件；后端只调整本轮工具收集与消息持久化时序。

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

### 2.10 **所有 terminal SSE 路径共用幂等 finalizer；`done` 是显式耐久性回执**

后端保存 assistant message 的一般条件从“`collected_text` 非空”改为“`collected_text` 非空 **或** `collected_tool_calls` 非空”。`chat.py` 在线原流、confirm/action terminal、pending 超时/启动清理必须调用同一组 `finalize_assistant_turn` / `finalize_pending_terminal` 收口服务，不能各自 emit result + done。finalizer 显式接收 ChatCommand `action` 与 `run_outcome`；需要生成消息时，以 `(conversation_id, original_trace_id)` 的 assistant partial unique index 幂等 insert/merge，写 `parent_message_id=source_user_message_id`，tool_calls 按 `tool_call_id` 去重并保持 started ordinal；已有非空文本不被离线 tool-only finalizer 覆盖。

`PreparedAction` 持久保存原 `trace_id`、`source_user_message_id`、conversation/user/tenant/agent、prepare/execute toolCallId、安全 presentation、conversation run-guard ownership 和 ChatCommand 收口上下文；Redis PendingPayload 只缓存 waiter/通知所需 actionId，不能成为授权事实源。tenant 只能来自认证中间件/服务端 resolver。SSE 断开不能释放仍 pending 的 conversation guard；进入 pending 时 lease 延长至 `expires_at + 60s`。confirm terminal、action TTL、rejected/failed、启动清理与正常在线流都必须经共享 finalizer，且在 action/operation/message commit 后按 owner token 释放 guard。

active projection 必须按 action/outcome 分支：

- `send` / `edit`：有 terminal tool result 时，approved/rejected/expired/failed 均可形成可 reload 的 active tool-only assistant；edit 的 replacement user 已提交，失败不恢复旧 suffix。
- `regenerate + success`：只有新回答（含成功 tool-only 结果）持久化成功时，才在同一事务插入 active assistant、令旧 assistant inactive 并建立 supersedes。
- `regenerate + rejected|expired|failed`：旧 assistant 保持 active，不创建新的 active assistant，不建立 supersedes；执行事实只留在 operation log。done 使用 `persistence="committed", projection="unchanged", messageId=null`，前端 detail 后保留旧回答。若未来要展示失败尝试，应走独立审计投影，不得混入常规 active history。

产生新 active assistant 的正常结束顺序固定为：收集 terminal 事件 → finalizer save/merge → `db.commit()` → emit `done(traceId, messageId, persistence="committed", projection="updated")`。因此客户端收到 committed ack 后读取 detail，必须能看到本轮已提交消息；send/edit 的纯工具结果和 HITL resume 也有 message owner。regenerate 非成功终态则先 commit operation terminal fact，再发 `projection="unchanged"`，不得伪造新 message owner。

assistant 保存或 commit 失败时，后端 rollback assistant transaction 并 emit `ai_error(AI_MESSAGE_PERSIST_FAILED)`，再发 `done(traceId, messageId=null, persistence="failed", projection=...)`；send/edit 若 source/replacement user 已在先前短事务 commit，则 projection 必须为 `updated`，regenerate 则为 `unchanged`。该 done 只表示错误流终止，不得触发成功完成或自动重放，但前端仍要按 projection 刷新真实 active history。路由 clarification、pre-mutation reject 等 active history 未改变的短路路径使用 `persistence="not_applicable", projection="unchanged"`。`projection` 取值仅为 `updated | unchanged`；事件类型集合不变，`done` 新字段必须向后兼容可选解析。

**反例**:
- 先 emit `done`、再 commit → 客户端收到终止信号后立即 refresh，可能读到旧快照；卡片在临时态与持久态之间闪烁或消失。
- 只在 `collected_text` 非空时保存 → agent 只返回工具结果时没有 assistant message 行，`tool_calls` 无处挂载，reload 后永久丢失。
- `resume.py` 直接 result + done 而不 finalizer → HITL 断流接管后的成功/拒绝卡片 reload 永久丢失。
- resume 重新生成 trace 或缺 source user → operation log、assistant parent 与原 user message 断链。
- 原流和 resume 各自 INSERT assistant → 竞态下同一 run 双消息；只靠应用层先查后插挡不住并发。
- commit 失败仍按正常成功路径清临时态 → UI 呈现“完成”，但审计链和历史消息均不存在。
- edit replacement 已 commit，但 assistant 保存失败却回 projection unchanged → 前端恢复已失活旧 suffix，与数据库 durable active history 分叉。
- regenerate rejected/expired/failed 仍写 active tool-only assistant并 supersede → 一次失败尝试会错误隐藏原本可用的旧回答。
- pending 只靠普通短 lease 或 SSE finally 释放 guard → 用户确认窗口尚未结束时另一标签可启动新 run；旧 tool 随后恢复到已变化上下文。

**回归**: 调整 `chat.py::sse_with_save`、confirm/action terminal、pending timeout/startup cleanup、conversation guard 与共享 finalizer；在线流可直接合并 collected cards，离线终态可从 PreparedAction + operation 重建 prepare/execute 两张卡，并按 toolCallId 去重。测试分别断言 send/edit terminal tool-only 可落 active、regenerate 仅 success supersede、原 trace/source 保留、并发 finalizer 仅一条 assistant、commit 先于 committed done、所有 pending terminal 按 owner token 释放 guard。SSE **事件类型集合**不增加；`confirmation_required` 字段和 conversation detail 增加 action projection。

### 2.11 **长对话性能：本期先测量，不引入 `v-memo` 或虚拟滚动**

本期目标是归属、耐久性与接管一致性，不在没有基准数据时加入渲染短路。原建议 `v-memo="[msg.messageId, msg.toolCalls?.length]"` 只观察长度，同长度内容替换、下载状态更新或 inactive 投影变化时可能错误复用旧 DOM。先用包含 100 条消息 / 200 张折叠卡片的基准场景记录 streaming 更新和滚动性能；只有数据表明历史 message-group patch 是主要瓶颈，才另开性能 Plan 选择完整依赖的 memo、组件拆分或虚拟滚动。

**反例**:
- 本期直接加只依赖 `toolCalls.length` 的 `v-memo` → 以潜在 stale UI 换取未经证实的收益，并扩大本次一致性重构的排障面。
- 立即上虚拟滚动 → 不定高卡片、滚动锚点和 streaming 自动滚底会显著扩大范围，当前没有 1000+ 消息的真实需求证据。
- 把性能问题与正确性修复捆绑为同一验收门槛 → 容易让工具卡归属与耐久性收尾被非必要优化阻塞。

**回归**: Phase 1 不添加 `v-memo`、不引入虚拟列表依赖；保留历史卡片默认折叠和 StatsChartView 折叠时不初始化 ECharts 的既有优化。基准数据达到单独性能 Plan 的触发条件后再决策，不回改本 spec 的正确性契约。

### 2.12 **确认 UI 只由结构化 action 状态触发，不解析 LLM 文本**

`ChatConfirmationDrawer` 的打开条件固定为 store 中存在当前会话、当前 owner 的有效 `pending_confirmation` action。Markdown 中出现“确认”“是否执行”、表格或按钮样式文本都只是 assistant content，不创建 pending、不打开抽屉，也不能调用 approve API。

**反例**: regex 扫描“请确认”自动弹窗 → 多语言、否定句和普通解释误触发，且文本没有冻结参数/授权对象；让业务卡片自行 open drawer → 每种 tool 都要复制 action 生命周期。

**回归**: 文本-only“请确认”不弹窗；`confirmation_required` 即使没有任何 assistant text 也弹窗；event/detail 重复到达按 actionId 去重。

### 2.13 **confirmation presentation 是安全 DTO，抽屉不展示 raw args**

抽屉和 pending 卡片只消费 Gateway `ConfirmationPresentation {title, summary?, fields[], warnings?}`。fields 按服务端顺序显示；tone 只控制颜色，不允许 HTML。前端不得回退展示 event args、preview token、fileId、内部路径、snapshot 或 frozen args。批准/拒绝请求仅提交 confirmationId 与 action。

**反例**: 通用抽屉默认 `JSON.stringify(args)` → 泄露内部 capability/PII，也让用户误以为可在浏览器修改参数；前端从 result 自己拼摘要 → Web/App/Desktop 对同一 action 展示不同。

**回归**: typings 不再要求 confirmation args/dryRun；恶意 HTML 按纯文本渲染；approve/reject request 类型无索引签名、不能附加业务字段。

### 2.14 **store 支持多个 pending action，主抽屉一次只聚焦一个**

状态从单值 `pendingConfirmation` 改为 `pendingActionsById: Map<actionId, PendingAction>`。消息流可以同时保留多个 pending 卡片（例如多个会话切换后恢复），当前会话主抽屉按 `createdAt/actionId` 稳定顺序聚焦最早有效 action；用户关闭抽屉只隐藏 UI，不 reject action，点击任一 pending 卡片可重新打开。approve/reject 成功或 detail 已无该 action时才移除。

**反例**: 新 confirmation 覆盖单值旧 confirmation → 旧 action 仍可执行但 UI 不可达；切换会话直接 clear → 返回后无法批准；关闭 Drawer 等价 reject → 误取消业务操作。

**回归**: 两 action upsert/排序/聚焦、跨会话隔离、关闭再打开、terminal 移除、detail authoritative reconciliation 全覆盖。

### 2.15 **prepared flow 的 preview、pending、execute 卡片共享一个 message group**

prepared flow 使用 `sourceToolCallId` 表示 LLM 发起的 preview，用 `toolCallId` 表示 Gateway 生成的 execute operation。流式时按 preview started/result → pending execute 顺序渲染；reload pending 时由 action.presentation 重建组合 pending 卡；批准终态后 finalizer 将 preview 与 execute 卡按 started ordinal 合并到同一 assistant message。不得把 preview 卡留在临时流、execute 卡挂到另一条消息。

**反例**: 只持久 execute result → 用户无法追溯批准时看过什么；preview 和 execute 各建一条 assistant → 一次回答被拆成两个 message owner，edit/regenerate 因果范围错误。

**回归**: 在线/断流/reload 三条路径最终得到同一 message group、相同 toolCallId 顺序和 presentation；重复 finalizer 不重复卡。

---

## 3. 数据流 / 时序图

### 3.1 流式期间

```
POST /ai/chat body.traceId=tr_1       ← 前端发请求前已生成并保存
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
后端：
ToolCallStarted(tc_1) 分配 ordinal=0
ToolCallResult(tc_1) 回填 ordinal=0
save_assistant_message(content="导出成功...", tool_calls=[tc_1])
db.commit()
SSE: done(traceId=tr_1, messageId=m_42, persistence="committed", projection="updated")
                                  ← 显式耐久性回执

前端连接关闭：
  → 生成 temp assistant message（文本 + 本轮工具卡快照）
  → fetchGetConversationDetail(conversation_id)
  → 响应包含 done 指定的 m_42 / tr_1 且覆盖预期 tool_call_id
  → currentMessages = detail.messages
  → streamEvents = []             ← 仅在持久消息成功接管后清空

若 commit 后、done ack 前断线：
  → detail.messages[].traceId == 请求前已知 tr_1
  → 用 tr_1 + expected tool_call_ids 完成同一接管，不依赖 done

前端渲染：
[messages list 包含 m_42]
  ChatMessage m_42 "导出成功..."
  ChatToolCall tc_1 (from m_42.toolCalls)   ← 嵌入 m_42 下方
```

### 3.3 Tool-only 与接管失败

```
tool-only：
collected_text = "", collected_tool_calls = [tc_1]
  → 后端仍保存 assistant message + commit + done
  → 前端创建无文字气泡的 temp assistant group，显示 tc_1 卡片

detail refresh 失败 / 未包含本轮消息：
  → streamEvents 保留为不可见 recovery buffer，不参与第二次渲染
  → 保留 temp assistant message.toolCalls 快照并标记 stale
  → 卡片继续位于该临时消息下，可重试 refresh
  → detail 收敛前阻止该 conversation 的下一条 ChatCommand
```

### 3.4 Reload 会话

```
fetchGetConversationDetail → currentMessages (含每条 message.toolCalls)
不再走 streamEvents 反序列化
每条 assistant message 直接从自己的 toolCalls 渲染嵌入卡片
常规 detail 只返回 active messages；inactive assistant 及卡片自然不渲染
```

### 3.5 HITL action terminal / 可选 SSE resume

```
PreparedAction(trace=tr_1, sourceUserMessageId=u_1, guardOwner=g_1, action, presentation/tool snapshots)
  → confirm handler 以 action 恢复原 trace/source；在线/热接管 SSE 只接收投影通知
  → approved / rejected / expired / failed terminal result
  → send/edit: finalize tool-only assistant → commit → projection=updated
  → regenerate success: 新 assistant + 原子 supersede → commit → projection=updated
  → regenerate rejected/expired/failed: 只 commit operation fact → projection=unchanged
  → terminal commit 后 compare-and-delete guardOwner=g_1

原流与 resume 竞态：
  → assistant run partial unique index + merge finalizer
  → 最终最多一条 assistant；tool_call_id 不重复，非空文本优先保留
```

### 3.6 prepared import + reload

```text
SSE: tool_call_started(user.import_preview, tc_preview)
SSE: tool_call_result(tc_preview, preview summary)
SSE: confirmation_required(actionId=a_1, sourceToolCallId=tc_preview, toolCallId=tc_execute)
  -> pendingActionsById[a_1] upsert + Drawer 自动打开

刷新页面：
GET /ai/conversation/c_1
  -> messages=[source user ...], pendingActions=[a_1]
  -> source user 后派生 transient pending assistant group
  -> 展示 action.presentation，不解析旧 Markdown，不需要原 SSE

approve：
POST /ai/confirm {confirmationId, action:"approve"}
  -> Gateway inline execute + finalizer commit assistant(toolCalls=[tc_preview, tc_execute])
  -> detail 接管；pendingActions 不再含 a_1；transient group 消失
```

---

## 4. 影响面

### 4.1 改动文件

| 文件 | 改动 |
|---|---|
| `hohu-admin-web/src/views/ai/chat/modules/chat-main.vue` | 渲染循环改：每条 assistant ChatMessage 后嵌入对应 ChatToolCall；streaming ChatMessage 下方渲染 streamEvents 衍生卡片；删除「整个列表末尾的 tool-call-list」块 |
| `hohu-admin-web/src/store/modules/ai/index.ts` | 请求前生成 traceId；新增 `pendingActionsById`、message groups 和 stream handoff state；SSE/detail 按 actionId upsert，terminal authoritative reconciliation |
| `hohu-admin-web/src/typings/api/ai.d.ts` / `src/service/api/ai.ts` | ConfirmationPresentation/PendingAction/detail typings；confirm request 仅 confirmationId/action |
| `hohu-admin-web/src/views/ai/chat/modules/chat-confirmation-drawer.vue` | 从 presentation 渲染，不展示 raw args；支持关闭后由 pending card 重开 |
| `hohu-admin/app/modules/ai/models/prepared_action.py` / schemas | 持久 action 与 conversation detail pendingActions DTO，Gateway spec §4.7/§8.8 为 SoT |
| `hohu-admin/app/modules/ai/schemas/chat.py` / `schemas/message.py` | ChatCommand 接收并校验稳定 traceId；MessageOut 返回 traceId，供 ack 丢失后的 detail reconciliation |
| `hohu-admin/app/modules/ai/api/chat.py` | `tool_calls` 按 started ordinal 收集；所有 terminal 路径调用共享 finalizer；commit 后发送带 trace/message/persistence/projection 的 done ack |
| `hohu-admin/app/modules/ai/api/resume.py` | HITL resume 按 action/outcome 收口；send/edit terminal 可持久化 tool-only，regenerate 非成功保持旧 active；commit 后按 token 释放 guard |
| `hohu-admin/app/modules/ai/agents/hitl/manager.py` | Redis 只做 waiter/通知缓存；按 actionId 回源 DB，不再以 PendingPayload 作为授权事实 |
| `hohu-admin/app/modules/ai/service/chat_service.py`（或独立 message service） | `finalize_assistant_turn`：run 级幂等 insert/merge、action/outcome projection、parent/source 绑定、tool_call_id 去重 |
| `hohu-admin-web/src/views/ai/chat/modules/__tests__/chat-tool-call-embed.spec.ts`（新） | vitest 覆盖嵌入渲染逻辑 |
| `hohu-admin/tests/modules/ai/test_chat_tool_persistence.py`（新或并入现有 chat 测试） | pytest 覆盖 started 顺序、tool-only 持久化和 `done` 耐久性顺序 |

### 4.2 不改

- 后端 SSE **事件类型集合**（不新增 `message_save` 或第二通道；扩展既有 `confirmation_required` 字段，并为 `done` 增加可选 durability/projection）
- `ai_message.tool_calls` JSON 对外结构 / 列类型（assistant run 唯一索引与 parent 语义由消息编辑 spec 的同一 migration 负责）
- `chat-message.vue`（保持纯气泡组件，不增加 toolCards prop / slot）
- 5 种 view_type 组件（DetailCardView / RowsAffectedView 等）
- `ChatClarification`（supervisor routing 候选卡片，独立机制）

### 4.3 测试调整

- 现有 `use-export-flow.spec.ts` 等 28 个 vitest 不动（渲染层重构不影响 composable 逻辑）
- e2e `test/e2e/ai-chat.spec.ts`（如存在）需调整 selector 期望位置
- 新增 chat-main 嵌入渲染测试
- 新增后端 tool-call / resume terminal 持久化、run 幂等与 SSE 结束顺序测试

---

## 5. 测试矩阵

### 5.1 前端 vitest

| 测试 | 验证点 |
|---|---|
| `test_embed_cards_under_assistant_message` | 2 条 assistant 消息各带 1 个 tool_call → 卡片渲染在各自消息下（不堆末尾） |
| `test_embed_empty_tool_calls_no_placeholder` | assistant 消息无 toolCalls → 不渲染任何卡片容器 |
| `test_embed_multiple_tool_calls_in_one_message` | 单条 assistant 消息带 3 个 tool_calls → 3 个卡片按数组顺序渲染在该消息下 |
| `test_streaming_cards_below_streaming_bubble` | 流式期间 streamingText 非空 + streamEvents 有 2 个事件 → 卡片渲染在 streaming 气泡下 |
| `test_detail_refresh_success_hands_off_by_done_ack` | committed done 后 detail 返回指定 messageId/traceId 且 tool_call_id 完整 → 替换 temp message 并清空 recovery buffer |
| `test_commit_then_ack_lost_hands_off_by_request_trace` | 请求前已知 traceId；DB commit 后连接在 done 前断开 → detail 按 traceId + tool IDs 接管，不进入永久 stale |
| `test_detail_refresh_failure_enters_stale_single_render_source` | detail 失败/缺本轮 assistant → 只渲染 temp snapshot；streamEvents 不二次渲染，stale 阻止下一 command |
| `test_tool_only_turn_creates_transient_message_group` | streamingText 为空但本轮有已完成工具卡 → 创建 temp assistant group 并显示卡片，不显示空文字气泡 |
| `test_reload_renders_cards_in_place` | selectConversation 后 3 条 assistant 消息各自带卡片，无末尾堆叠 |
| `test_pending_hitl_card_during_streaming` | confirmation_required 时 pending 卡片渲染在 streaming 气泡下（带倒计时 bar） |
| `test_inactive_assistant_not_rendered_with_cards` | detail active 投影不含被软删除 assistant → 对应 tool_calls 卡片自然不出现 |
| `test_ai_error_then_failed_done_reconciles_by_projection` | `ai_error + done(persistence=failed)` → 不标成功/不重跑；send/edit updated 收敛已提交 user，regenerate unchanged 保留旧回答 |
| `test_edit_assistant_persist_failure_keeps_replacement_projection` | edit replacement 已 commit、assistant save 失败 → detail 保留 replacement/旧 suffix inactive，显示回复失败 |
| `test_regenerate_unchanged_projection_restores_old_answer` | regenerate rejected/expired/failed + projection=unchanged → 撤销 temp projection，detail 保留旧 active assistant |
| `test_text_confirmation_does_not_open_drawer` | Markdown“请确认”无 action → 不弹抽屉 |
| `test_event_and_detail_upsert_pending_by_action_id` | SSE/detail 重复 action → 单卡、单抽屉 |
| `test_reload_derives_transient_pending_group_after_source_user` | reload 有 pendingActions、无 assistant → 正确派生 transient group |
| `test_multiple_pending_actions_are_isolated_and_stably_focused` | 多 action/多会话不覆盖，关闭 Drawer 不 reject |
| `test_drawer_uses_presentation_and_confirm_request_has_no_args` | 不展示 raw args/token；请求体只有 confirmationId/action |

### 5.2 后端 pytest

| 测试 | 验证点 |
|---|---|
| `test_tool_calls_persist_in_started_order` | tc_1 先 started、tc_2 后 started，但 tc_2 先 result → JSON 仍为 `[tc_1, tc_2]` |
| `test_tool_only_assistant_message_is_persisted` | 文本为空、tool_calls 非空 → assistant message 落库并持有卡片 |
| `test_commit_happens_before_committed_done` | committed done 发出前消息已 finalizer + commit，ack 含正确 traceId/messageId |
| `test_persist_failure_emits_action_aware_projection` | assistant save/commit 失败 → rollback + `AI_MESSAGE_PERSIST_FAILED` + persistence=failed；send/edit projection=updated，regenerate=unchanged |
| `test_send_edit_resume_terminal_results_persist_tool_only_assistant` | send/edit 的 approved/rejected/expired/failed 以原 trace/source/command context 落 tool-only assistant后再 updated done |
| `test_regenerate_only_success_supersedes_old_assistant` | regenerate success 原子 supersede；rejected/expired/failed 不建 active assistant，返回 unchanged，旧 assistant 保持 active |
| `test_pending_terminal_paths_finalize_and_release_owned_guard` | confirm、action TTL 与 startup cleanup 均 commit terminal fact后按 owner token 释放 guard；非 owner 不得释放 |
| `test_confirm_rejects_tenant_mismatch_without_client_override` | PreparedAction tenant 来自服务端；当前 tenant 不匹配则拒绝，客户端 tenantId 无法覆盖 |
| `test_online_and_confirm_finalize_once` | 在线流与 confirm terminal 并发 finalizer → partial unique + merge 后仅一条 assistant、tool_call_id 不重复、非空文本保留 |
| `test_client_trace_is_validated_persisted_and_returned` | 请求 traceId 写 user/assistant/operation，MessageOut 返回；格式错误/跨 owner 冲突稳定拒绝 |
| `test_conversation_detail_returns_owned_active_pending_actions` | 仅 owner/tenant、未过期、source active action；DTO 不含 frozen args/snapshot/token |
| `test_offline_prepared_finalizer_rebuilds_preview_and_execute_cards_once` | 原 SSE 不在时从 action/operation 重建同一 message group，toolCallId 去重 |

### 5.3 E2E（Playwright）

| 场景 | 验证 |
|---|---|
| 多轮对话嵌入 | 问「导出用户」→ 问「导出角色」→ 问「统计用户」→ reload → 3 个卡片分别在各自消息下 |
| 流式期间视觉 | 问「导出用户」→ streaming 期间卡片立即出现在 streaming 气泡下 |
| HITL 流程 | 触发 HITL 工具 → pending 卡片在 streaming 气泡下倒计时 → 确认后 result 卡片替换位置不变 |
| Task 33 回归 | user.export 完成 → 卡片在消息下 → 下载按钮可见可点 |
| Tool-only + reload | 模拟仅返回工具结果的轮次 → 卡片有 assistant owner → reload 后仍在原消息位置 |
| HITL 断流 + reload | pending 后断流 → detail 恢复 action → approve/reject → reload 卡片仍归原 source user 对应 assistant；regenerate 失败仍显示旧回答 |
| 用户导入 prepared flow | “导入用户”→ preview 卡 + 自动抽屉 → approve → execute 卡；全程无第二次 LLM execute 调用 |
| 用户导入纯预览 | “只看看可导入哪些用户”→ preview 卡，无抽屉、无 pending action |

---

## 6. Plan / Task 状态块

### Task 35a confirmation slice（P0，与 Phase 1 基础交错）

- [ ] Task 35a-W1：同步 Gateway event/detail/confirm DTO typings；store 从单值 `pendingConfirmation` 迁为 `pendingActionsById`，SSE/detail 按 actionId reconcile
- [ ] Task 35a-W2：Drawer 只渲染 ConfirmationPresentation，approve/reject 只传 confirmationId/action；文本“请确认”永不触发 UI
- [ ] Task 35a-W3（依赖下方 Task 0-2）：streaming/reload 派生 transient pending assistant group；prepared preview/pending/execute 最终归同一 message group
- [ ] Task 35a-W4：完成 §5 新增 vitest/pytest/E2E；Task 35a 未绿前不进入 Task 36

### Phase 1（消息归属与耐久性收尾，本期）

- [x] Safety 前置 **✅ 已完成（2026-08-07）**：Task 35 已把 operation effect metadata、file_id ACL/trusted tenant、HITL tenant 复核和私有 artifact 边界收口；edit/regenerate UI/store Safety Gate 已关闭。该完成项只保证后续 finalizer 可依赖可信执行事实，不代表 message projection/handoff 已实现。
  - S.1 **执行安全前置与卡片耐久实现分开验收** — Task 35 修复 execution fact 的可信度，本 spec Phase 1 仍负责 message owner、terminal commit 和 handoff。**反例**: metadata/file ACL 通过就把工具卡 spec 标完成 → reload/HITL resume 仍可能丢卡或双写。**回归**: Safety 测试与 §5 durability/projection 测试保持两组独立门禁；Task 0-7 状态不因 Safety Gate 自动变更。
- [ ] Task 0（共享基础）：先落消息编辑 spec Task 1、2、2b：assistant run partial unique index、source/trace/parent 语义、客户端稳定 traceId 以及 conversation run guard/terminal cleanup；暂不开放 edit/regenerate
- [ ] Task 1（Red）：新增后端失败测试，覆盖 started 顺序、send/edit 与 regenerate 分支、chat/confirm/action TTL/startup cleanup、原 trace/source、owned guard、并发 finalizer、commit 先于 committed `done`、ack 丢失与持久化失败错误流
- [ ] Task 2（Green）：实现 action/outcome-aware `finalize_assistant_turn` 与 terminal cleanup；修改 chat.py、confirm/action service 与 Redis waiter；为既有 done 增加可选 traceId/messageId/persistence/projection，保持事件类型集合和 `tool_calls` JSON schema 不变
- [ ] Task 3（Red/Green）：修改 aiStore — 请求前生成 traceId；删除 restoredEvents；新增 `messageToolCards` 与 handoff state；streaming 用 events，关闭后只渲染 temp snapshot；detail 按 ack 或 request trace 完整接管才清 buffer，unchanged 恢复旧回答，失败 stale + 阻止下一 command
- [ ] Task 4：修改 chat-main.vue — 每条 assistant 消息后嵌入对应卡片；streaming / transient group 下渲染本轮卡片；tool-only 不显示空文字气泡；删除末尾 tool-call-list
- [ ] Task 5：vitest 覆盖 §5.1，pytest 覆盖 §5.2；回归现有 AI store、chat、resume、HITL、export 测试
- [ ] Task 6：E2E 覆盖 §5.3（含 reload、HITL resume、下载、tool-only）并回归现有 ai-chat e2e
- [ ] Task 7：后端 `ruff check . && ruff format . && pytest`、前端 `pnpm lint && pnpm typecheck && pnpm test` 通过；将本状态块改 ✅ 并补 ship-time 决策

### Phase 2（推迟）

- [ ] 其他模块（role / dept / job）AI 工具调用嵌入复用 — 无需改动，本次重构完成后自动生效
- [ ] 「重新执行」/「查看完整参数」等卡片级 action — 等真实用户反馈
- [ ] 移动端嵌入布局适配（如需要）
- [ ] 基于 100 messages / 200 cards 基准数据评估 memo / 组件拆分 / 虚拟滚动；没有数据不实现

---

## 7. 范围外

- 新增或重命名 SSE 事件类型（不需要；扩展既有 `confirmation_required` payload 和 `done` durability/projection 字段）
- 修改 `ai_message.tool_calls` JSON schema 或为工具卡单独新增数据库字段（不需要；trace/source/active 等共享字段由消息编辑 spec migration 负责）
- view_type registry 扩张（不需要）
- 逐行勾选、多级审批、审批意见/SLA 等业务审核 UI（仍走 reviewRef/业务页面）
- 消息编辑 / 重新生成的副作用校验与软删除实现（由 `2026-08-06-ai-message-edit-semantics.md` 负责）；本 spec 只约定常规 active 投影不返回 inactive assistant，因此其卡片自然隐藏

---

## 8. 开放问题

无。原有问题和 ADR-0002 新增问题均已有决定：
- ChatMessage prop vs wrapper → 决策 2.8（chat-main wrapper）
- pending 混合渲染顺序 → 决策 2.9（严格时间顺序）
- 正常流结束与前端接管 → 决策 2.3 / 2.10（commit 后 done；detail 成功才接管）
- HITL resume reload 丢卡与双写 → 决策 2.10（共享幂等 finalizer + 原 trace/source）
- pending run guard 生命周期 → 决策 2.10（pending TTL + owner token + 全 terminal cleanup）
- regenerate 失败是否替换旧回答 → 决策 2.10（仅 success 更新 active；否则 projection unchanged）
- commit 后 done 丢失 → 决策 2.3（请求前 traceId + MessageOut.traceId reconciliation）
- 临时卡双源与 stale history → 决策 2.3（分阶段单一渲染源 + detail 收敛门禁）
- 长对话性能 → 决策 2.11（本期 measure-first，不加 v-memo / 虚拟滚动）
- confirmation 触发源 → 决策 2.12（只认结构化 action）
- 参数展示与批准请求 → 决策 2.13（presentation + ID-only command）
- 多 pending/reload → 决策 2.14（Map + detail recovery）
- preview/execute 归属 → 决策 2.15（同一 message group）

---

## 9. 关联

- 用户导入业务 binding 由 [`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md) Task 35a 定义；本 spec 只消费通用 action/presentation
- 演进自 BUG-FE-18 修复（streamEvents 反序列化）；本 spec 完成度高后该修复路径退役
- 与 [`2026-08-06-ai-message-edit-semantics.md`](./2026-08-06-ai-message-edit-semantics.md) 共用“message 是卡片 owner”的边界：active message 负责常规 UI 投影，operation log 负责执行事实与审计
- 与 [`../adr/0001-ai-safety-consistency-before-deferred-execution.md`](../adr/0001-ai-safety-consistency-before-deferred-execution.md) 一致：本期只收紧同步 SSE 的耐久与接管，不引入 deferred 或第二实时通道
- 与 [`../adr/0002-gateway-owned-confirmation-flow.md`](../adr/0002-gateway-owned-confirmation-flow.md) 一致：Gateway 产生确认事实，客户端只投影并提交 ID-only 决策
- 主流标杆参考：ChatGPT / Claude.ai / 文心 / 通义 的 tool 卡片嵌入位置
