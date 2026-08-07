# AI Message Edit Semantics（AI 消息编辑语义） — v1.2

**Status**: 🚧 Safety Gate 与 Task 35a.0 共享 send/confirm 基础已完成（2026-08-07）；PreparedAction binding 及 edit/regenerate 主实现未开始，入口继续关闭
**Created**: 2026-08-06
**Updated**: 2026-08-07
**Owner**: hohu core team
**Depends on**:
- `app/modules/ai/models/message.py`（`AiMessage` 已存在，本 spec 加 active projection / revision 字段）
- `app/modules/ai/service/operation_log_service.py`（`ai_operation_log` 是副作用判断的事实源）
- `app/modules/ai/api/chat.py`（send / edit / regenerate 统一进入单次 ChatCommand）
- 前端 `hohu-admin-web/src/store/modules/ai/index.ts`（当前 `editAndResend` / `regenerate` 是 bug 源头）
**Related**:
- [`../adr/0001-ai-safety-consistency-before-deferred-execution.md`](../adr/0001-ai-safety-consistency-before-deferred-execution.md)（AI 当前版本先收口安全与一致性）
- [`../adr/0002-gateway-owned-confirmation-flow.md`](../adr/0002-gateway-owned-confirmation-flow.md)（PreparedAction 绑定 source message revision）
- [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 / §8.8（本 spec 不新增第二条实时通道）
- [`2026-07-16-tool-result-view-design.md`](./2026-07-16-tool-result-view-design.md)（tool_call result UI 不变）
- [`2026-08-05-chat-tool-card-embed-in-message.md`](./2026-08-05-chat-tool-card-embed-in-message.md)（卡片是展示投影，不是编辑授权事实源）
- [`APP-MARKETPLACE.md`](../APP-MARKETPLACE.md)（决策记录格式标杆）

---

## 1. Context

### 1.1 现状（bug）

前端 `editAndResend`（`store/modules/ai/index.ts:751`）实现了「编辑 user 消息后重发」：

```ts
async function editAndResend(messageIndex: number, newContent: string) {
  if (!newContent.trim() || isStreaming.value) return;
  currentMessages.value = currentMessages.value.slice(0, messageIndex);  // ← 只截断前端
  attachedImages.value = [];
  attachedFiles.value = [];
  currentMessages.value.push({ /* temp user message */ });
  await doStream();
}
```

**前端假装替换，后端实际追加**：

- 前端 `slice(0, messageIndex)` 清空本地数组
- `doStream` 发请求时 `messages: currentMessages.value.map(...)` 只剩新 user 消息
- 但后端 `POST /ai/chat` 的 line 659 `chat_service.save_user_message()` 每次都 INSERT 一条新记录，**不感知**"这是编辑"
- `doStream` finally 拉取 `fetchGetConversationDetail` → 拿到数据库的**完整追加历史**

**用户视角**：发"你好" → AI 回复 → 编辑成"你好，很高兴认识你" → AI 再次回复 → 对话变 4 条（2 user + 2 assistant），而非替换成 2 条。

### 1.2 痛点（hohu 生态视角）

#### 痛点 1：编辑不撤销副作用，反而**重复执行**

TOC AI（ChatGPT/Claude）的编辑是"换个问法探索答案"，答案纯文本无副作用。hohu 生态 AI 不一样：

| 系统 | 编辑后的灾难场景 |
|---|---|
| **CRM** | 用户编辑「给客户打 8 折」→「9 折」，AI 再次调 `update_customer_discount` → 数据库两次写入 |
| **ERP** | 用户编辑「订单改已发货」→「已取消」，AI 重复执行 → 客户收到两封矛盾邮件 |
| **OA** | 用户编辑「批准请假」→「拒绝请假」，审批流已推进 → 系统状态不一致 |
| **IoT** | 用户编辑「关闭设备」→「打开设备」，物理设备开关一次，可能损坏 |

> **核心矛盾**：用户编辑心智是"我错了，想撤销重做"，但 AI 编辑实际是"再执行一次"，副作用不逆。

#### 痛点 2：审计断裂

当前 `editAndResend` 假装截断后，**数据库仍保留旧消息**，但前端 UI 完全不知道。这造成：
- 管理员以为"我编辑了，旧的没了"
- 实际数据库有 4 条消息，合规审计时无法解释
- 如果用方案 B（硬删除 truncate），反过来：管理员无法举证"我当时到底问的什么"

#### 痛点 3：方案选型误区

按 ChatGPT/Claude 标杆选 A（分支树）是 TOC 思维。hohu 生态是 TOB 业务执行类，用户不需要"对比 3 种折扣方案"，需要"快速改正错误 + 留痕可审计"。

### 1.3 触发

2026-08-06 用户实测发现"对话变 2 条"bug。讨论中意识到：**这不是单纯前端 slice 的实现 bug，而是 TOB AI 编辑语义未定义**。需要 spec 化决策：

1. 编辑的副作用边界（哪些消息可编辑，哪些不可）
2. 数据保留策略（硬删 vs 软删）
3. 拒绝编辑时的用户引导

---

## 2. 关键设计决策

### D.1 **执行事实与展示投影分离，并建立 user message 因果键**

`ai_operation_log` 是 tool 执行事实源；`ai_prepared_action` 是准备/批准事实源。编辑 / regenerate 的安全判断必须联合读取二者；`assistant_message.tool_calls` 和前端 pending card 只负责展示，不参与授权。每条 operation log 新增：

- `source_user_message_id`：触发本次 tool 执行的 user message ID，nullable 仅用于兼容迁移前历史数据，并建立索引；
- `readonly_snapshot`：执行当时 `AiToolMeta.readonly` 的不可变快照，默认 `false`（未知按 write 保守处理）。

同时把已有 `trace_id` 真正写入本轮 user / assistant message。`trace_id` 用于观测和跨日志串联，`source_user_message_id` 用于业务因果，二者不互相替代。编辑中间消息时，检查范围必须覆盖**目标消息及被截断后缀内全部 user message**对应的 operation log 和 PreparedAction。

`readonly_snapshot=true` 只有在 tool 确实不产生持久副作用时才成立；Gateway 自身 operation log / 短期 query cache 不算业务 tool 副作用，但业务 batch、导出任务、持久文件和外部写入都算。开放 edit/regenerate 前必须完成内置 tool metadata 审计；已知反例 `user.import_preview` 会写 batch/file/cache，必须先改为 `readonly=False`。旧日志、未知 tool 或未经证明的 metadata 一律按 write 处理，本期不为此引入完整 effect/reversibility 框架。

| operation 状态 | readonly snapshot | 编辑 / regenerate |
|---|---:|---|
| `rejected` / `expired` | 任意 | 允许（业务函数未执行） |
| `success` | `true` | 允许（纯读） |
| `failed` | `true` | 允许（纯读失败无业务写入） |
| `success` | `false` | 拒绝：`AI_EDIT_BLOCKED_HAS_SIDE_EFFECT` |
| `failed` | `false` | 保守拒绝：`AI_EDIT_BLOCKED_OPERATION_UNCERTAIN` |
| `running` / `pending_confirmation` | 任意 | 拒绝：`AI_EDIT_BLOCKED_OPERATION_IN_PROGRESS` |

PreparedAction 补充矩阵：

| action 状态 | 编辑 / regenerate |
|---|---|
| `prepared` / `pending_confirmation` / `approved` / `running` | 拒绝：`AI_EDIT_BLOCKED_ACTION_IN_PROGRESS` |
| `rejected` / `expired` | 允许；execute 未运行 |
| `succeeded` | 按绑定 execute operation 的 readonly snapshot 判定，写操作拒绝 |
| `failed` | 按 execute operation 判定；缺失/未知一律按 write outcome uncertain 拒绝 |

未来 deferred 状态（如 queued）一律按 in-progress 拒绝，但本期不因此引入 ARQ / Worker。

**反例**:
- 只查 `status='success'` → readonly 查询被误拦截，failed write 的部分副作用却被漏放；
- 只查目标消息自己的 operation → 编辑中间消息时漏掉后续轮次已执行的写操作；
- 用 `message.tool_calls[].ok` 推断 → 卡片是 UI 投影，可能尚未持久化或来自旧版本，不能作为安全事实；
- 运行时读取当前 Tool Registry 的 `readonly` → tool 元数据升级后会改写历史语义，必须落 snapshot。
- 把“没有修改主业务表”当 readonly → `user.import_preview` 会重复生成 batch/file，仍是持久副作用；必须按真实 effect 标注。

**回归**: 覆盖 operation + action 双矩阵、后缀多轮检查和 source message 因果关联；action 存在但 operation 尚未创建/终态不一致时 fail-closed。

### D.2 **消息表永不物理删除，使用 active projection + revision lineage**

`AiMessage` 增加 `is_active: bool = True`。edit 在短事务中把目标后缀置 inactive 并插入 replacement user；regenerate 只有新 assistant 成功持久化时才原子切换旧/new assistant，非成功终态不改变 active projection。常规会话查询只返回 active，审计查询显式包含 inactive。

新消息增加 `supersedes_message_id` 指向被替换的原消息：编辑时新 user 指向旧 user，regenerate 时新 assistant 指向旧 assistant。现有 `parent_message_id` 保持因果归属而不复用为修订关系：所有新 assistant（send / edit / regenerate / tool-only / HITL resume）必须令 `parent_message_id = source user message_id`。因此纯文本 assistant 也能可靠找到触发它的 user message；operation log 的 `source_user_message_id` 与 assistant parent 指向同一来源。

**反例**:
- truncate 硬删除 → 丢失 TOB 审计证据；
- 只有 `is_active` 没有 revision 指针 → Phase 2 无法可靠判断“新版本替换了谁”；
- 复用 `parent_message_id` 表示“替换了哪条消息” → 因果归属与修订关系混杂；修订只能写 `supersedes_message_id`。
- 让 assistant 的 parent 长期为 null，再靠相邻位置或 trace 猜 source user → 纯文本 regenerate 无可靠因果键。

**回归**: migration 默认 active；常规查询过滤 inactive；inactive 仍可审计；连续两次编辑形成可追溯 supersedes 链。

### D.3 **编辑粒度：从目标 user message 到 active 会话末尾整体失活**

编辑 user message X 时，在同一 conversation 内锁定并校验 X 为当前用户拥有的 active user message；选出 X 及其后的全部 active message，将其置 `is_active=false`，再插入一条 replacement user message。安全检查的 operation 范围必须与该后缀完全一致。

常规历史按 `(create_time, message_id)` 稳定排序；数据库列名是 `message_id`，不是 `id`。实现可利用 Snowflake ID 的顺序性筛选后缀，但测试必须显式按 `message_id`，不能只依赖可能同值的 `create_time`。

**反例**:
- 只失活 user X，保留其后 assistant → 上下文断裂；
- 只检查 X 的副作用却失活整个后缀 → 安全检查与实际 mutation 范围不一致；
- 允许编辑 inactive / assistant / 其他 conversation 的消息 → 越权或产生第二条 revision 分支。

**回归**: 覆盖编辑中间消息、最后一条 user 消息、inactive target、跨 conversation 和非 owner 拒绝。

### D.4 **并发运行、HITL pending 与双编辑由后端统一阻止**

前端 `isStreaming` 只做即时 UX。后端必须使用 conversation-scoped run guard（复用现有 Redis，owner token + TTL/续期）覆盖 send / edit / regenerate 整个运行，并在 mutation 事务内锁定 conversation/target active message。被截断后缀存在 in-progress operation 或 PreparedAction 时拒绝编辑。普通 streaming lease 由 heartbeat 续期；进入 pending 时将 guard owner、run trace、可信 tenant 和 ChatCommand finalization context 写入 PreparedAction，并把 lease 延长到 action expiry + 60s，不能因 SSE 断开而释放。每个新 ChatCommand 在 Redis guard 前后都查询 DB in-progress action，Redis flush/重启不能绕过；启动可从 action 重建 guard cache。confirm handler 复核当前认证 tenant/source 后成为唯一执行 authority；Redis waiter 只接收 terminal 通知。只有 action/operation/message terminal commit 后才能 compare-and-delete owner token。

terminal cleanup 必须有一个共享入口，覆盖正常在线流、confirm approve/reject、action TTL、Redis waiter 丢失和服务启动清理。即使没有可写 SSE 客户端，也要先把 PreparedAction/operation 标为 terminal、按 ChatCommand action/outcome 执行 finalizer（regenerate 失败保持旧 active），再按 token 释放 conversation guard。guard lease 过期只是防死锁兜底，不得代替领域终态持久化。

这不是 deferred execution 或第二实时通道，只是当前同步 SSE 链路的互斥安全边界。

**反例**:
- 只依赖当前浏览器 `isStreaming` → 另一个标签页或直接 API 可绕过；
- pending 检查后再无锁更新 → confirm 与 edit 竞态，旧操作可能在编辑后获批执行；
- Redis lock 无 owner token → 旧请求 finally 误删新请求的锁。
- SSE 断开就 finally 释放 pending run guard → action 仍可由 detail 恢复批准，另一标签却能先 edit，旧 tool 随后执行到已变化上下文。
- guard TTL 只覆盖单次 tool timeout 而短于 HITL 5 分钟窗口 → pending 尚有效时互斥已消失。
- 只有在线 SSE 路径 finalizer，confirm/TTL/启动清扫只改 action 或删 Redis → action、operation、卡片投影与 guard 终态不一致。

**回归**: 覆盖双标签并发编辑只有一个成功、streaming 期间拒绝、confirm 与 edit 竞态只有一个 CAS 获胜、pending lease 大于确认 TTL，以及 confirm/TTL/startup cleanup 均先持久化 terminal 再由 owner 释放 guard。

### D.5 **编辑能力由后端返回，前端只负责展示与本地临时收紧**

conversation detail 为每条 active user message 返回：

```json
{
  "editPolicy": {
    "canEdit": false,
    "reason": "has_side_effect",
    "executedTools": [{"toolName": "user.import", "summary": "导入 50 个用户"}]
  }
}
```

后端批量计算 suffix policy，避免逐消息 N+1；真正执行命令时必须重新校验。前端可以在 streaming / 本地 pending 状态下进一步禁用，但不能扫描 `toolCalls` 自行放宽服务器结论。拒绝时显示 tooltip、保留“复制原文”，不提供强制编辑。

**反例**:
- 前端按 `toolCalls.ok` 决定 canEdit → projection 缺失时错误放行；
- detail 返回 canEdit 后执行时不重查 → policy 与点击之间发生新操作，形成 TOCTOU；
- 提供“强制编辑并清除副作用” → 历史业务写入无法由消息 UI 撤销。

**回归**: vitest 使用服务端 `editPolicy`；pytest 覆盖同一次 detail 查询批量生成 policy；命令端再次校验。

### D.6 **regenerate 与 edit 本期共用同一 guard，不得留绕过入口**

本期 regenerate 仅接受**最后一条 active assistant message**，并以其 `parent_message_id` 指向的 source user message 为上下文，复用 D.1-D.5 的 owner、run guard、副作用和 revision 规则。通过前置校验后，旧 assistant 在运行期间仍保持 active；仅当新 run 成功持久化 assistant 时，才在同一事务把旧 assistant 置 inactive，使新 assistant 的 `parent_message_id` 继续指向同一 source user、`supersedes_message_id` 指向旧 assistant。非成功终态不切 projection；全过程**不再次 INSERT 已存在的 user message**。若要重跑中间轮次，用户必须走 edit，由 edit 负责整体失活后缀。legacy assistant 的 parent 为 null 时禁止 regenerate，不按邻接或 trace 猜测。

**反例**:
- edit 本期拦截、regenerate 推迟 → 用户可绕过 edit 防线重复执行 write tool；
- regenerate 再保存一次最后 user message → 当前实现的重复 user 记录继续存在；
- 任意 regenerate 中间 assistant 且保留后续消息 → 上下文断裂；
- 两套独立 guard → 策略漂移。

**回归**: write tool 后 regenerate 拒绝；readonly tool 后可 regenerate；成功 regenerate 不新增重复 user message。

### D.7 **一次 ChatCommand 完成 mutation + SSE，禁止“两次请求接力”**

沿用 `POST /ai/chat` SSE 入口，新增 `action: send | edit | regenerate`、可选 `targetMessageId`，以及由维护中 Web 客户端在请求前生成的稳定 `traceId`（`tr_` + 32 hex）。后端在任何 mutation 前验证格式、conversation owner 与 run-key 未冲突，并将其作为本轮 user/assistant/operation 的 `trace_id`；因此 DB commit 成功但 terminal ack 丢失时，前端仍能用请求前已知 key 对 detail 收敛。旧 send 客户端未传时后端可生成并通过 `X-AI-Trace-ID` 暴露，但当前 Web 实现必须主动传值。三种 action 进入同一个 ChatRunService：校验 → run guard → safety/routing → 事务内重验与 active projection mutation → 只写一次必要消息 → 从后端 active history 构造上下文 → stream → 通过共享 finalizer 幂等持久化 assistant。

本期不采用“edit REST API 先 INSERT，再返回 streamUrl 调 `/ai/chat`”的两请求方案。SSE 事件类型集合保持现有协议，不新增 WebSocket、轮询或第二实时通道；按工具卡 spec，只为既有 `done` 扩展可选 `traceId/messageId/persistence/projection` 字段。`projection="updated"` 表示本 ChatCommand 已改变 durable active history（可能只有 send user / edit replacement，`messageId` 仍可为空）；`projection="unchanged"` 表示 active history 未被本轮改变。`persistence` 单独描述预期 assistant/terminal fact 是否成功持久化，两者不得混为一个状态。

前端不直接改写 authoritative `currentMessages`，而是用 `pendingCommand` 生成临时 active projection：edit 隐藏目标后缀并展示 replacement user，regenerate 暂时隐藏待替换 assistant；streaming 卡片/文本挂在该临时 projection 下。正常或错误终止后都必须刷新 detail：成功时原子接管，失败时按后端真实 active projection 收敛。若 refresh 失败，保留临时 projection 并标记 `stale`，阻止同 conversation 的下一条 ChatCommand，只允许重试 detail 或放弃本地临时展示；不得回退显示与后端已失活状态冲突的旧 suffix。

edit 的 mutation 在模型调用前短事务提交：若随后 LLM / assistant 持久化失败，replacement user 仍是 active，旧后缀仍 inactive，done 返回 `persistence="failed", projection="updated", messageId=null`；detail 按 request trace 找到 replacement并显示“已编辑但回复失败”，用户可基于 replacement 重试，不偷偷恢复旧后缀。send 同理保留已提交 source user。regenerate 则维持旧 assistant active，只有 `run_outcome=success` 的新 assistant 持久化成功时才在同一事务切换 active 与 supersedes。rejected/expired/tool failed 不创建新 active assistant、不建立 supersedes，PreparedAction/operation log 保留授权与执行事实，done 返回 `persistence="committed", projection="unchanged", messageId=null`；纯 LLM / assistant persistence 失败返回 `persistence="failed", projection="unchanged"`。regenerate 两类非成功情况 refresh 后都恢复旧回答，所有 action 均不得自动重跑 tool。

**反例**:
- edit endpoint INSERT replacement 后 `doStream()` 又触发 `/ai/chat` INSERT → 双 user message；
- 前端先 slice 再把 history 当权威传给后端 → refresh 后旧数据库历史重新出现；
- edit/regenerate 各复制一份 chat pipeline → routing、safety、持久化逐渐分叉。
- edit 后 detail refresh 失败就继续渲染旧 suffix → UI 与数据库 active projection 分叉，下一条命令基于错误上下文。
- trace 只在后端开始流后生成并仅放 terminal done → commit 后 ack 丢失时客户端无法识别已持久消息。
- regenerate 非成功 tool-only attempt 也写 active + supersedes → 失败请求错误替换旧回答，与用户可见状态相反。

**回归**: API 测试断言每个 command 的消息写入次数与 trace 归属；编辑后 reload 只返回 active replacement；regenerate 仅 success 替换、非成功保持旧 active；DB commit 后 done 丢失仍可按 request traceId 接管；三种 action 共享相同 SSE parser。

### D.8 **迁移前因果不完整的数据默认保守，禁止猜测回填**

`source_user_message_id` 对历史 operation log 允许 null。若 edit 目标后缀为纯文本且无 tool 事实，可编辑；若存在历史 `tool_calls` / operation log，但无法可靠关联 source user message，则返回 `AI_EDIT_BLOCKED_LEGACY_UNCERTAIN`。legacy assistant 的 `parent_message_id` 为 null 时 regenerate 返回 `AI_REGENERATE_BLOCKED_LEGACY_UNCERTAIN`。不按相邻时间、trace 或 JSON 顺序猜测回填。

**反例**:
- 用“最近一条 user message”批量回填 → 并行/失败/旧数据可能错误关联，放行不可逆写操作；
- legacy null 一律允许 → 用迁移缺口绕过安全边界；
- legacy null 一律禁止所有纯文本对话 → 不必要地损伤无工具会话。
- legacy assistant parent null 仍按“前一条 user”猜 regenerate 来源 → 删除/旧数据异常时可能重跑错误上下文。

**回归**: 覆盖 legacy 纯文本 edit 允许、legacy tool-bearing suffix edit 拒绝、legacy parent-null regenerate 拒绝，以及迁移后 operation source ID / assistant parent 非空。

### D.9 **PreparedAction 绑定不可变 source message revision，旧 action 不迁移到新分支**

每个 `PreparedAction.source_user_message_id` 指向当次 ChatCommand 已持久化的不可变 user message 行；由于 edit 创建新 message ID 并以 `supersedes_message_id` 建立 lineage，message ID 本身就是 revision identity，不再增加可变 `revision_number`。confirm 执行前必须验证 source message 仍 `is_active=true`、属于原 conversation/owner/tenant，并且 action trace 与该 run 一致。

编辑目标后缀或 regenerate source 上存在 in-progress action 时，命令端在 mutation 前拒绝，用户必须先显式 reject 或等待 expiry；不得由 edit 静默批准、迁移或修改 action。若 confirm 与 edit 并发，双方都按 `conversation -> source/target message -> PreparedAction` 固定顺序加锁：edit 只有在确认 action 非 in-progress 后才能失活 suffix；confirm 必须在 action 仍 pending 时检查 source active 和 snapshot，再 CAS approved/running。任何观察到 source 已失活的 confirm 都将 action 从 pending 收口为 `expired + AI_PREPARED_ACTION_SOURCE_STALE`，不调用 execute。

terminal action 不迁移到 replacement：rejected/expired 可按 D.1 继续编辑；succeeded/failed 由 execution fact 决定是否阻止。preview-only 没有 PreparedAction，但 `user.import_preview` 自身是 write/non-idempotent operation，会按 D.1 阻止自动 replay/编辑后重跑，不能因“没弹确认”当作 readonly。

**反例**:
- edit 后把旧 action 的 source ID 更新为 replacement → 用户批准的是旧内容/旧附件/旧策略，却在新分支执行；
- edit 自动 reject pending → 另一个标签页正在确认时产生隐式状态改变，审计无法解释是谁拒绝；
- confirm 不按与 edit 相同的锁顺序锁定 source/action → 检查与执行之间 source 可能失活，或双方死锁；
- preview-only 无 action就允许 replay → 重复创建 batch/file/cache。

**回归**: 覆盖 pending action 阻止 edit/regenerate、显式 reject 后允许、confirm/edit 双竞态只有一方推进、source inactive confirm 不执行并写 stale error、replacement 不继承 action，以及 preview-only operation 仍按 write metadata 判定。

---

## 3. 方案对照

| 方案 | 审计保留 | 副作用事实 | 单次写入 | TOB 适配 | 选型 |
|---|---|---|---|---|---|
| **A. 分支树（ChatGPT 风格）** | 完整 | 无 | 可做到 | 差（探索式 ≠ 业务执行） | ❌ |
| **B. Truncate + Insert** | 丢失 | 无 | 可做到 | 差（无审计） | ❌ |
| **C. Soft Delete + Insert** | 完整 | 无 | 可做到 | 中（仍会重复业务操作） | ⚠️ 不够 |
| **D. Active projection + causal operation guard + 单次 ChatCommand** | 完整 | `ai_operation_log` | 是 | 优 | ✅ |

**为何不选 A**：分支树解决的是内容探索，不解决业务副作用。它还引入分支切换、树遍历和“哪条分支是业务现状”的额外认知成本。

**为何 D 优于 C**：soft delete 只解决留痕；source message 因果键、执行元数据快照和单次 ChatCommand 才能同时解决安全判断、双写和 reload 一致性。

---

## 4. 数据模型变更

### 4.1 `ai_message`

```python
class AiMessage(Base):
    # ... 现有字段 ...
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
        comment="当前 active projection；inactive 仅供审计",
    )
    supersedes_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="本消息替换的原 message_id；不复用 parent_message_id",
    )
```

已有 `trace_id` 字段不新增列，但 `save_user_message` / `save_assistant_message` 必须传入本次 ChatCommand 在请求前确定的 trace ID。replacement user 和新 assistant 分别记录自己的 run trace；`MessageOut` 必须暴露 camelCase `traceId`。服务端在 mutation 前检查同一 owner/conversation 下该 trace 未被历史 message/operation 使用，冲突返回 `AI_CHAT_TRACE_CONFLICT`，不得把新请求并入其他 run。

所有新 assistant 必须写 `parent_message_id=source_user_message_id`。同一 run 的原流与 HITL resume 可能竞争收口，因此新增 partial unique index：`(conversation_id, trace_id) WHERE role='assistant' AND trace_id IS NOT NULL`。共享 `finalize_assistant_turn` 以该键幂等 insert/merge：tool_calls 按 `tool_call_id` 去重并保持 started ordinal，非空文本不得被 tool-only resume 覆盖；只有 finalizer commit 成功后才能发 durability `done`。regenerate 非成功终态不创建 active assistant，该 run 由 operation log 作为执行事实，并通过 `projection="unchanged"` 明确 active UI 未变化。

### 4.2 `ai_operation_log`

```python
class AiOperationLog(Base):
    # ... 现有字段 ...
    source_user_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="触发本 operation 的 user message；null 仅兼容历史数据",
    )
    readonly_snapshot: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
        comment="执行时 AiToolMeta.readonly 快照；未知按 write 处理",
    )
```

`save_user_message` 必须返回并 flush `AiMessage`，随后把 `message_id` 注入 `ChatDeps.source_user_message_id`；Gateway `start_operation` 把 source ID 和 readonly snapshot 一起写日志。assistant message 在流结束收口时、正常 `done` 之前持久化并 commit，与工具卡 spec 的耐久性屏障一致。

### 4.3 Migration 与索引

同一 migration 包含：

- `ai_message.is_active BOOLEAN NOT NULL DEFAULT true`；
- `ai_message.supersedes_message_id BIGINT NULL`；
- `ai_operation_log.source_user_message_id BIGINT NULL`；
- `ai_operation_log.readonly_snapshot BOOLEAN NOT NULL DEFAULT false`；
- active-history partial index：`(conversation_id, create_time, message_id) WHERE is_active = true`，同时覆盖过滤与稳定排序；
- assistant run 唯一索引：`UNIQUE (conversation_id, trace_id) WHERE role = 'assistant' AND trace_id IS NOT NULL`；
- operation guard index：`(conversation_id, source_user_message_id, status)`。

不猜测回填 `source_user_message_id`。迁移前日志保持 null，由 D.8 legacy policy 处理。

### 4.4 查询层

常规会话查询：

```python
stmt = (
    select(AiMessage)
    .where(
        AiMessage.conversation_id == conversation_id,
        AiMessage.is_active.is_(True),
    )
    .order_by(AiMessage.create_time.asc(), AiMessage.message_id.asc())
)
```

所有面向普通 UI 的 message lookup（包括 routing feedback）都要验证 active；审计服务必须使用独立、显式的 `include_inactive=True` 路径。conversation detail 一次查询全部相关 operation log，倒序累积每条 user message 的 suffix edit policy，禁止 N+1。

---

## 5. API 契约变更

### 5.1 `POST /ai/chat` 增加 ChatCommand action

`action` 缺省为 `send`，兼容现有客户端。维护中的 Web 客户端对三种 action 都必须在请求前生成 `traceId`；旧 send 客户端缺省时由服务端生成并通过 `X-AI-Trace-ID` 暴露。成功仍返回现有 SSE；edit/regenerate 不再先调用另一个 mutation API。

字段互斥由后端校验并返回 `AI_CHAT_COMMAND_INVALID`：`send` 不得带 target；`edit` 必须带 active user target 与非空 `newContent/newParts`；`regenerate` 必须带最后一条 active assistant target 且不得带新内容。客户端传来的 history 只用于旧 `send` 兼容，edit/regenerate 的上下文一律从数据库 active projection 构造。

```json
// edit
{
  "action": "edit",
  "conversationId": "123",
  "traceId": "tr_0123456789abcdef0123456789abcdef",
  "targetMessageId": "456",
  "newContent": "你好，很高兴认识你",
  "newParts": [{"type": "text", "text": "你好，很高兴认识你"}],
  "modelId": "optional",
  "agentCode": "optional"
}
```

```json
// regenerate
{
  "action": "regenerate",
  "conversationId": "123",
  "traceId": "tr_fedcba9876543210fedcba9876543210",
  "targetMessageId": "789",
  "modelId": "optional",
  "agentCode": "optional"
}
```

命令处理顺序：

1. 验证 traceId 格式与未被占用，再验证 conversation owner、target conversation/role/active；
2. 以 traceId 作为 owner context 获取 conversation run guard，初步计算 suffix operation + PreparedAction policy；
3. 使用新内容（regenerate 使用 source user 内容）完成 safety / routing / agent resolution；若被阻断或需要 clarification，不修改 active projection；
4. 在短事务中按固定顺序锁定 conversation/target/action，重新校验 active 与 operation/action policy；
5. edit：后缀失活 + INSERT 一次 replacement user；regenerate：运行期仅从上下文逻辑排除旧 assistant，不 INSERT user；
6. 从数据库 active history 构造 LLM 上下文，不信任前端 slice 后的历史，随后复用 Gateway / SSE pipeline；
7. edit 在流结束时保存新 assistant；regenerate 仅在 `run_outcome=success` 的新 assistant 持久化成功的同一事务中把旧 assistant 置 inactive 并建立 supersedes 关系，非成功时不建 active assistant且保留旧 assistant；所有已创建 assistant 写正确 parent；
8. finalizer/terminal fact commit 后发带 `projection=updated|unchanged` 的 `done`；无论成功/失败前端都 reload conversation detail。updated + messageId 以新 assistant 接管；updated + messageId null（send/edit 回复失败）以 request trace 对应的 active user/replacement 收敛；regenerate unchanged 恢复旧 assistant；
9. 非 pending terminal 在 commit 后按 owner token 释放 run guard；进入 HITL pending 时把 token/finalization context 绑定 PreparedAction 并延长 lease，原 SSE finally 不释放，交由 confirm/action TTL/startup cleanup 收口后释放。

pre-stream 拒绝返回领域错误，前端不得再发第二次 chat 请求：

| errorCode | 含义 |
|---|---|
| `AI_EDIT_BLOCKED_HAS_SIDE_EFFECT` | 后缀存在成功 write operation |
| `AI_EDIT_BLOCKED_OPERATION_UNCERTAIN` | failed write 无法证明无副作用 |
| `AI_EDIT_BLOCKED_OPERATION_IN_PROGRESS` | streaming/running/HITL pending |
| `AI_EDIT_BLOCKED_ACTION_IN_PROGRESS` | 后缀存在 prepared/pending/approved/running action |
| `AI_EDIT_BLOCKED_LEGACY_UNCERTAIN` | 历史 tool 事实无法建立 source 因果 |
| `AI_REGENERATE_BLOCKED_LEGACY_UNCERTAIN` | 历史 assistant 无 parent，无法可靠定位 source user |
| `AI_MESSAGE_EDIT_CONFLICT` | target 已 inactive 或并发 mutation 冲突 |
| `AI_CHAT_TRACE_CONFLICT` | traceId 格式非法、已被其他 run 使用或 owner/conversation 不匹配 |
| `AI_CHAT_COMMAND_INVALID` | action 与 target/content 字段组合不合法 |

### 5.2 Conversation detail 增加 `editPolicy` 与 `pendingActions`

仅 active user message 返回 policy；assistant 的 regenerate policy 可使用同一结构命名为 `regeneratePolicy`。Snowflake ID 仍序列化为字符串。

```json
{
  "messageId": "456",
  "role": "user",
  "editPolicy": {
    "canEdit": false,
    "reason": "has_side_effect",
    "executedTools": [
      {"toolName": "user.import", "toolCallId": "tc_1", "summary": "导入 50 个用户"}
    ]
  }
}
```

`executedTools` 只返回已脱敏摘要，不返回原始 args。

conversation 顶层同时按 Gateway spec §8.8 返回当前 owner/tenant 的 `pendingActions`。message policy 只给按钮判定，pendingActions 给确认 UI 恢复，二者不可互相推导：

```json
{
  "messages": [],
  "pendingActions": [
    {
      "actionId": "1900000000000000001",
      "confirmationId": "opaque-token",
      "sourceUserMessageId": "456",
      "traceId": "tr_0123...",
      "tool": "user.import_execute",
      "toolCallId": "tc_execute_1",
      "sourceToolCallId": "tc_preview_1",
      "interactionFlow": "prepared",
      "presentation": {"title": "导入用户", "fields": []},
      "expiresAt": "2026-08-07T14:10:00Z"
    }
  ]
}
```

DTO 不返回 frozen args、snapshot、preview token、tenant 或内部 subject ref；source message inactive/跨 owner/tenant/已过期 action 不进入列表。

### 5.3 前端 store

```ts
async function editAndResend(messageId: string, newContent: string) {
  if (!newContent.trim() || isStreaming.value) return;
  await doStream({
    action: 'edit',
    traceId: createChatTraceId(),
    targetMessageId: messageId,
    newContent,
    newParts: [{ type: 'text', text: newContent }]
  });
}

async function regenerate(messageId: string) {
  if (isStreaming.value) return;
  await doStream({
    action: 'regenerate',
    traceId: createChatTraceId(),
    targetMessageId: messageId
  });
}
```

调用前不再 `slice()` / `pop()` 或直接 push 到 authoritative `currentMessages`。store 先生成并把 traceId 存入 `pendingCommand`，再用该对象供 `displayMessages` 计算临时 active projection；SSE 结束（含 `ai_error + done`）后沿用 conversation detail refresh 接管。有 ack 时 messageId/traceId 快匹配，ack 丢失时按 pendingCommand.traceId 匹配；failed+updated 必须保留 detail 中已提交的 send user / edit replacement并移除未持久化 assistant 临时态，failed/committed+unchanged 则撤销 regenerate 临时投影并保留旧回答。领域拒绝时清 pending 并保持当前消息；refresh 失败时保留 pending projection、标记 stale 并阻止下一条 command。参数全部使用 message ID，不再依赖 v-for index。

### 5.4 `chat-message.vue`

```ts
const editBlockedReason = computed(() => {
  if (props.isStreaming) return 'page.ai.chat.editBlockedStreaming';
  if (!props.message.editPolicy?.canEdit) {
    return `page.ai.chat.editBlocked.${props.message.editPolicy?.reason ?? 'unknown'}`;
  }
  return null;
});
```

前端不扫描后续 `toolCalls` 决定安全策略。tooltip 提供原因和“复制原文”；服务端命令仍再次校验。

---

## 6. 测试覆盖

### 6.1 后端（pytest）

| 测试 | 覆盖决策 |
|---|---|
| `test_edit_blocked_when_write_succeeds_in_suffix` | D.1/D.3 — 任一后缀 write success 拒绝 |
| `test_edit_blocked_when_write_failed_is_uncertain` | D.1 — failed write 保守拒绝 |
| `test_edit_allowed_when_readonly_succeeds_or_fails` | D.1 — readonly terminal 状态允许 |
| `test_edit_allowed_when_operation_rejected_or_expired` | D.1 — 未执行状态允许 |
| `test_edit_blocked_when_operation_running_or_pending` | D.1/D.4 — in-progress 拒绝 |
| `test_edit_soft_deletes_active_suffix_once` | D.2/D.3/D.7 — mutation 范围和单次 INSERT |
| `test_edit_creates_supersedes_lineage` | D.2 — replacement 指向原 user |
| `test_active_history_filters_and_orders_stably` | D.2/D.3 — active projection + 稳定排序 |
| `test_operation_log_records_source_and_readonly_snapshot` | D.1 — 因果键和执行时快照 |
| `test_readonly_snapshot_defaults_false_and_preview_is_write` | D.1 — 未知保守；`user.import_preview` 不得快照为 readonly |
| `test_assistant_parent_points_to_source_user_for_all_actions` | D.2/D.6 — send/edit/regenerate/tool-only 均有持久因果键 |
| `test_edit_rejects_non_owner_cross_conversation_and_inactive_target` | D.3 — owner/target 校验 |
| `test_concurrent_edits_only_one_succeeds` | D.4 — run guard + row lock |
| `test_pending_guard_outlives_hitl_ttl_and_disconnect` | D.4 — pending lease ≥ HITL TTL+60s，SSE disconnect 不释放 |
| `test_all_pending_terminal_paths_finalize_then_release_owner_guard` | D.4 — resume/wake-false/TTL/startup cleanup 均先 commit terminal，再 compare-and-delete；错误 token 无效 |
| `test_regenerate_uses_same_guard_without_duplicate_user` | D.6/D.7 — 无绕过、无双写 |
| `test_regenerate_legacy_parent_null_is_blocked` | D.6/D.8 — 不猜 source user |
| `test_edit_response_failure_keeps_replacement_active` | D.7 — edit 已提交但生成失败时不恢复旧 suffix |
| `test_edit_assistant_persist_failure_returns_failed_updated_projection` | D.7 — messageId=null 但 replacement 已 durable，detail 按 trace 收敛而非恢复旧 suffix |
| `test_regenerate_response_failure_keeps_old_assistant_active` | D.7 — 新回答未持久化不切 active |
| `test_regenerate_rejected_expired_or_tool_failed_keeps_old_active` | D.7 — 非成功终态只落 operation fact + projection unchanged，不建 active assistant/supersedes |
| `test_client_trace_is_validated_persisted_and_returned` | D.7 — request trace 写 message/operation，MessageOut 返回，冲突不串 run |
| `test_commit_before_done_disconnect_reconciles_by_request_trace` | D.7 — commit 后 ack 前断线，detail 可按请求前 trace 找到 active assistant |
| `test_chat_command_field_matrix_rejects_invalid_combinations` | D.7 — 三 action target/content 互斥 |
| `test_legacy_tool_suffix_is_blocked_but_plain_text_allowed` | D.8 — 保守兼容 |
| `test_detail_computes_edit_policy_without_n_plus_one` | D.5 — 批量 policy |
| `test_pending_prepared_action_blocks_edit_and_regenerate` | D.1/D.9 — action 未终态时两个入口都拒绝 |
| `test_rejected_action_allows_revision_but_success_uses_execution_fact` | D.1/D.9 — terminal action 与 operation 联合判定 |
| `test_confirm_edit_race_has_single_winner` | D.4/D.9 — 固定锁序 + CAS，不在失活 source 上执行 |
| `test_stale_source_expires_action_without_execute` | D.9 — source inactive → stale error/expired，业务函数未调用 |
| `test_replacement_does_not_inherit_prepared_action` | D.2/D.9 — 新 revision 使用新 message ID，无 action 迁移 |
| `test_detail_pending_actions_are_scoped_and_sanitized` | D.5/D.9 — owner/tenant/active scope，隐藏 frozen data |

### 6.2 前端（vitest）

| 测试 | 覆盖决策 |
|---|---|
| `editButtonUsesServerEditPolicy` | D.5 — 不从 tool card 推断 |
| `editButtonIsLocallyDisabledWhileStreaming` | D.4/D.5 — 本地只收紧 |
| `editCommandUsesMessageIdWithoutLocalSlice` | D.3/D.7 — 单次 command |
| `regenerateCommandDoesNotPopOrResendUser` | D.6/D.7 — 无绕过和双写 |
| `editRejectionKeepsCurrentMessages` | D.7 — 拒绝不破坏本地状态 |
| `pendingEditProjectsReplacementWithoutMutatingCurrentMessages` | D.7 — 临时 active projection 单源 |
| `pendingCommandOwnsTraceBeforeRequestAndAckLossCanReconcile` | D.7 — request 前 trace 固定；无 done 时用 detail.traceId 接管 |
| `regenerateUnchangedProjectionRestoresOldAssistant` | D.7 — rejected/expired/failed 不隐藏旧回答 |
| `editFailedUpdatedProjectionKeepsReplacementUser` | D.7 — assistant 未持久化仍以 detail replacement 为准，并显示回复失败 |
| `failedDetailRefreshKeepsStaleProjectionAndBlocksNextCommand` | D.7 — 不回退到与后端冲突的旧 suffix |
| `blockedReasonTooltipAndCopyOriginal` | D.5 — 替代路径 |
| `pendingActionPolicyCannotBeRelaxedByToolCards` | D.1/D.5 — 前端卡片不参与放宽 |

### 6.3 E2E（Playwright）

| 场景 | 验证 |
|---|---|
| 纯文本编辑 | 编辑后 reload 仅见 replacement + 新 assistant，无旧 active 消息和重复 user |
| readonly 后编辑 | 查询 tool 成功后仍可编辑，历史 query card 随 inactive assistant 隐藏 |
| write 后编辑/regenerate | 两个入口都 disabled，直接 API 也被后端拒绝 |
| 编辑中间消息 | 后续 active 消息全部隐藏，审计数据仍保留 |
| HITL 与并发 | pending 时拒绝；两个标签同时编辑只有一个成功 |
| PreparedAction 与编辑竞态 | 一个标签批准、另一个编辑：最多一方推进；旧 action 永不执行到 replacement |

审计历史页面 / diff 的 E2E 随 Phase 2 实现，不阻塞本期核心语义上线。

---

## 7. Plan / Task 状态块

### Safety gate（实现完成前）

- [x] Task 0 **✅ 已完成（2026-08-07）**：`AI_MESSAGE_REVISION_ACTIONS_ENABLED=false` 统一隐藏 edit/regenerate 入口；store 方法只返回 `false`，不 slice/pop/push authoritative messages、不清附件、不发 stream 请求
- [x] Task 0a **✅ 已完成（2026-08-07）**：用户导入导出 spec Task 35 已完成 16 个 built-in effect metadata 审计、file_id owner/trusted tenant/内容/私有路径边界及 HITL tenant 复核；这只完成 guard 前置信任基础，不代表 edit/regenerate 可以开放
- [ ] Task 0b（P0 跨 spec 门禁）：Task 1/2/2b 先作为 Gateway Task 35a 的共享基础落地，再完成 PreparedAction 持久化、source binding、confirm sole authority 和 detail pendingActions；整个过程中 edit/regenerate 继续关闭
  - 0.1 **Safety Gate 同时禁 UI 与命令副作用** — 单靠隐藏按钮不能防其他组件或测试直接调用 store action，因此 action 本身也必须 no-op。**反例**: 只用 `v-if` 隐藏 → 调用 `regenerate()` 仍会 pop 历史并重复 tool。**回归**: Vitest 断言按钮不存在、两 action 返回 false 且消息/附件/stream 调用均不变。
  - 0.2 **Gate 只由单一常量控制** — UI 与 store 共享 `AI_MESSAGE_REVISION_ACTIONS_ENABLED`，避免一端误开。**反例**: 两处硬编码 boolean → 后续只改一处形成隐藏入口或无响应按钮。**回归**: safety-gate 测试同时覆盖 store 与 `chat-message.vue`。

### Current release（安全与一致性收尾）

- [x] Task 1 **✅ 已完成（2026-08-07）**：`AiMessage.is_active` / `supersedes_message_id` + `AiOperationLog.source_user_message_id` / `readonly_snapshot` migration；已补 active history、operation source/status 和 assistant run partial unique indexes
- [x] Task 2a **✅ trusted tenant 子集已完成（2026-08-07）**：服务端 resolver 向 `ChatDeps/AiToolContext` 注入 tenant_id；旧 direct HITL PendingPayload 已保存 tenant 并在 resume/confirm 复核；Task 35a 将同一不变量迁入 PreparedAction，客户端字段仍不能覆盖
- [x] Task 2 **✅ 已完成（2026-08-07）**：ChatCommand/Web 在请求前生成并验证稳定 traceId，MessageOut 返回 traceId；user message flush/返回 ID，注入 `ChatDeps.source_user_message_id`，user/assistant 写原 run trace，Gateway operation log 写因果键与 readonly snapshot
- [ ] Task 2b（共享 guard 前置）：为 send/confirm 实现 conversation run guard、owner token、stream heartbeat、action expiry+60s pending lease 和统一 terminal cleanup；PreparedAction 保存 trusted tenant/source/finalization context，Redis waiter 只通知；覆盖 confirm/TTL/startup cleanup，pending SSE finally 不释放
  - [x] Task 2b.0 **✅ send/direct-HITL 子集已完成（2026-08-07）**：conversation owner token、stream heartbeat、pending TTL+grace handoff、confirm/resume/timeout/startup terminal cleanup 已落地；PendingPayload 保存 trusted tenant/source/finalization context，pending SSE finally 不释放。PreparedAction 替换旧 PendingPayload 仍属于 Task 35a.1-35a.5，因此 Task 2b 保持未完成
- [ ] Task 2c（跨 spec 前置）：完成工具卡 spec Phase 1 的 action/outcome-aware finalizer、HITL resume 持久化、durability/projection done、ack-loss reconciliation 与前端 handoff state；后续 edit/regenerate 直接复用，不另建收口链路
- [ ] Task 3：实现 suffix operation + PreparedAction policy、legacy policy和 D.9 固定锁序/CAS，统一领域错误层级
- [ ] Task 4：在 Task 2b guard 上补 edit/regenerate 的 target row lock、owner/active/role 重验和 mutation conflict；不再实现第二套 guard
- [ ] Task 5：重构共享 ChatRunService，接入工具卡 spec 已落地的 `finalize_assistant_turn`；`POST /ai/chat` 支持 send/edit/regenerate，保证单次 mutation、assistant parent、run 级幂等收口和 action 字段互斥
- [ ] Task 6：active history 查询、revision lineage、conversation detail 批量 `editPolicy` / `regeneratePolicy` + scoped `pendingActions`
- [ ] Task 7：后端 pytest 覆盖 §6.1，包含并发、越权、legacy 与无双写
- [ ] Task 8：前端 store 改为 messageId ChatCommand，删除 edit slice / regenerate pop；以 pendingCommand 派生临时 active projection，detail 失败进入 stale 并阻止下一条 command
- [ ] Task 9：chat-message 使用服务端 policy，补 tooltip / 复制原文 / 双语 i18n
- [ ] Task 10：Vitest + E2E 覆盖 §6.2-§6.3，并回归 tool card / HITL
- [ ] Task 11：本 spec 状态改为 ✅，记录 ship date 和实现期新增决策

### Later release（审计体验，不阻塞本期）

- [ ] Task 12：审计页面 `/ai/audit/conversations/{cid}` 显示 inactive 消息与 revision lineage
- [ ] Task 13：编辑历史 diff（原始 vs replacement）
- [ ] Task 14：批量导出对话审计 JSON（含 inactive，权限与脱敏独立验收）

### ai-tool-gateway v1.5+（按量化触发条件启动）

- [ ] Task 15：tool effect / reversibility 元数据标准化和 compensating action 设计
- [ ] Task 16：deferred operation 状态接入同一 edit guard（不在本 spec 引入 Worker 或第二实时通道）

---

## 8. 范围外

- ChatGPT 风格分支树和版本切换 UI；
- AI 自动撤销已执行 tool 副作用；本期只阻止重复执行，撤销仍走原业务模块；
- ARQ / Worker、deferred execution、WebSocket 或第二实时通道；
- 通用行级 Chat HITL；批量复核使用业务审核页面 / reviewRef；
- 跨 conversation 的编辑历史合并；
- 移动端编辑 UI；
- 把 inactive 历史重新喂给 LLM（新流只使用 active history）。

---

## 9. 开放问题

无。D.1-D.9 已给出当前版本可实现且保守的安全边界。ADR-0002 PreparedAction 属于当前 action 级确认收尾，不等同于 deferred；未来只有在 ADR-0001 的量化触发条件满足时，才重新设计 deferred 状态与 reversibility。

---

## 10. 关联

- 本 spec 是 hohu 生态 AI 的 replay 行为契约：Gateway 定义“如何执行 tool”，本 spec 定义“何时允许重新执行”；
- 与 [`2026-08-05-chat-tool-card-embed-in-message.md`](./2026-08-05-chat-tool-card-embed-in-message.md) 在数据生命周期上协同：inactive assistant 的卡片从 active UI 自然消失，但安全判断永远使用 operation log；
- 与 [`../adr/0001-ai-safety-consistency-before-deferred-execution.md`](../adr/0001-ai-safety-consistency-before-deferred-execution.md) 一致：当前只做同步链路的安全与一致性收尾，不扩建完整 deferred 基础设施。
- 与 [`../adr/0002-gateway-owned-confirmation-flow.md`](../adr/0002-gateway-owned-confirmation-flow.md) 一致：action 绑定不可变 source message ID，批准前复验 active revision，旧 action 不迁移。
