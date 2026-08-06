# AI Message Edit Semantics（AI 消息编辑语义） — v1.0

**Status**: 📝 Ready for Plan（决策 D.1-D.6 已闭环；待 Plan mode 拆 Phase 1/2 Task）
**Created**: 2026-08-06
**Owner**: hohu core team
**Depends on**:
- `app/modules/ai/models/message.py`（`AiMessage` 已存在，本 spec 加 `is_active` 字段）
- `app/modules/ai/service/operation_log_service.py`（用 `ai_operation_log` 判断副作用，已存在）
- `app/modules/ai/api/chat.py:659`（`save_user_message` 调用点，本 spec 改前置 truncate）
- 前端 `hohu-admin-web/src/store/modules/ai/index.ts:751`（`editAndResend` 是当前 bug 源头）
**Related**:
- [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §8.1 SSE 协议（本 spec 不改协议）
- [`2026-07-16-tool-result-view-design.md`](./2026-07-16-tool-result-view-design.md)（tool_call result UI 不变）
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

### D.1 **TOB AI 编辑必须前置检查 tool_call 副作用**

`editAndResend` 调用前，后端先查 `ai_operation_log` 表：该 user message 之后是否存在 `status='success'` 的 tool_call 记录。

- **无 success 记录**（纯文本对话、tool_call 全部 failed/rejected/expired）→ 允许编辑
- **有 success 记录**（已产生数据库/邮件/物理副作用）→ 拒绝编辑，返回 `AI_EDIT_BLOCKED_HAS_SIDE_EFFECT` 错误码

**反例**:
- 不检查直接编辑已 import 用户的请求 → 用户被重复导入，数据污染
- 仅检查"是否有 tool_call"而不区分 status → 已失败的 tool_call（无副作用）也会阻止编辑，过度保守
- 用 `ai_message.tool_calls` JSON 字段判断 → user message 上不会有 tool_calls，需要 JOIN assistant message，逻辑复杂

**回归**: 单元测试覆盖 4 种 tool_call 状态（success/failed/rejected/expired）的编辑放行/拦截行为；E2E 覆盖"导入用户后尝试编辑 → 按钮 disabled"。

### D.2 **消息表永不物理删除，统一 soft delete via `is_active`**

`AiMessage` 加 `is_active: bool = True` 字段。编辑 = 把旧消息 `is_active=False` + INSERT 新消息。所有查询默认 `WHERE is_active=True`，审计查询不带此 filter。

**反例**:
- 方案 B（truncate 硬删除）→ 管理员无法举证"我当时问的什么"，TOB 合规审计失败
- 方案 A（分支树，加 parent_message_id 维护父子）→ TOB 用户不需要分支切换 UI，复杂度溢出
- 用独立 `ai_message_history` 表存历史 → 查询要 UNION 两表，复杂度高；soft delete 一表搞定

**回归**: migration 加字段 + 默认 true；所有 `select_messages_by_conversation` 加 `is_active=True` filter；审计页面（Phase 2）查 `is_active=False` 显示历史版本。

**注释**: 现有 `parent_message_id` 字段（message.py:42）保留不动，未来若升级到分支模型可复用，本 spec 不依赖它。

### D.3 **编辑粒度：从被编辑消息位置 soft delete 到对话末尾**

编辑 user message X 时，soft delete `conversation_id=C AND id >= X.id` 的**所有消息**（含 X 自己 + X 之后的所有 assistant/user 消息）。然后 INSERT 新 user message，触发新流。

**反例**:
- 只 soft delete user message X，保留 X 之后的 assistant 回复 → 上下文断裂（新 user 消息后面接的是给"旧 user 消息"的回复）
- 只 soft delete 到下一个 user message 之前 → 同样断裂
- 创建"分支"保留所有版本 → D.2 反例

**回归**: 测试覆盖"编辑中间消息后，其后 5 条消息全部 is_active=False"；测试覆盖"编辑最后一条 user 消息，仅该消息被 soft delete"。

### D.4 **streaming 中 / HITL pending 时禁止编辑**

`isStreaming.value === true` 或被编辑消息之后存在 `status='pending_confirmation'` 的 tool_call → 禁止编辑。

**反例**:
- 允许 streaming 中编辑 → doStream 并发写 currentMessages，状态混乱
- 允许 HITL pending 中编辑 → 用户编辑后旧 HITL 仍在队列，可能被 approve 造成副作用

**回归**: 前端 `isStreaming` 判断已有（line 752），保留；后端增加 `pending_confirmation` 检查作为兜底（防前端绕过）。

### D.5 **拒绝编辑时的用户引导：禁用按钮 + tooltip + 替代路径**

被拦截编辑的消息，"编辑"按钮 disabled，hover tooltip 显示具体原因：
- "该消息已触发业务操作（导入用户），无法编辑。请新发消息澄清，或前往操作日志撤销。"
- 提供"复制原文"按钮（让用户方便新发消息时引用）
- 不提供"强制编辑"入口（TOB 场景下副作用不可逆，不应给绕过路径）

**反例**:
- 弹窗提示但不提供替代路径 → 用户卡住，不知道下一步
- 提供"强制编辑（会清除副作用）"入口 → 副作用无法清除（数据库已写入），强制编辑只会造成更多不一致

**回归**: vitest 覆盖 `hasExecutedToolCall` computed 在 4 种 tool_call 状态下的返回值；E2E 覆盖"导入用户 → hover 编辑按钮 → 看到 tooltip"。

### D.6 **编辑语义统一适用 regenerate（重新生成）**

`regenerate()`（删最后一条 assistant 重新跑）也按 D.1 规则：如果该 assistant 消息对应 tool_call 有 success 副作用，禁止 regenerate（避免重复执行）。regenerate 当前实现 `msgs.pop()` 是前端模拟，本 spec 顺手修正为后端 soft delete。

**反例**:
- 编辑拦截但 regenerate 不拦截 → 用户绕路用 regenerate 重复执行 tool
- regenerate 走完全独立路径 → 维护两套语义，易出 bug

**回归**: 单元测试覆盖"已成功 tool_call 的 assistant regenerate 拒绝"。

---

## 3. 方案 ADR 对照

| 方案 | 数据保留 | UI 复杂度 | TOB 适配 | 工作量 | 选型 |
|---|---|---|---|---|---|
| **A. 分支树（ChatGPT 风格）** | 全保留 + 父子关系 | 高（切换器 `< 1/3 >`） | 差（探索式 ≠ 业务执行） | 2-3 天 | ❌ |
| **B. Truncate + Insert（GitHub Copilot 风格）** | 硬删除 | 低 | 中（无审计） | 1 天 | ❌ |
| **C. Soft Delete + Insert** | 全保留，UI 隐藏 | 低 | 中（无副作用感知） | 1.5 天 | ⚠️ 不够 |
| **D. C + 编辑前置检查（本 spec）** | 全保留 + 副作用拦截 | 低 | 优 | 2-3 天 | ✅ |

**为何不直接选 B**：B 硬删除丢失审计举证能力。CRM/ERP 场景"管理员 3 个月前问过什么、改过什么"是合规审计常见诉求，C 加一个 bool 字段成本极低，收益是审计完整性。

**为何不选 A**：A 为「探索多版本」设计。hohu 用户不需要"对比 3 种折扣方案"，需要"快速改正错误"。A 的 UI 复杂度（切换器、树遍历）对 TOB admin 是负担。

**为何 D 优于 C**：C 只解决"保留历史"问题，未解决"编辑触发副作用重复执行"问题。D 加前置检查，是 TOB AI 真正需要的语义。

---

## 4. 数据模型变更

### 4.1 `ai_message` 表

```python
# app/modules/ai/models/message.py
class AiMessage(Base):
    # ... 现有字段 ...
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False,
        comment="Soft delete 标记；编辑/截断时置 false，审计可查"
    )
```

### 4.2 Migration

```bash
alembic revision --autogenerate -m "add is_active to ai_message"
alembic upgrade head
```

migration 包含：
- `ALTER TABLE ai_message ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true`
- 创建 partial index：`CREATE INDEX ix_ai_message_conv_active ON ai_message (conversation_id, id) WHERE is_active = true`（加速常规查询）

### 4.3 查询层改动

`conversation_service.select_messages_by_conversation()`（已存在）改为：
```python
stmt = (
    select(AiMessage)
    .where(
        AiMessage.conversation_id == conversation_id,
        AiMessage.is_active == True,  # noqa: E712
    )
    .order_by(AiMessage.id)
)
```

审计页面（Phase 2）显式 `is_active.in_([True, False])`。

---

## 5. API 契约变更

### 5.1 新增：`POST /ai/conversations/{cid}/messages/{mid}/edit`

```json
// Request
{
  "newContent": "你好，很高兴认识你",
  "newParts": [{"type": "text", "text": "你好，很高兴认识你"}]
}

// Response（成功，触发 SSE 流）
{
  "code": 200,
  "msg": "success",
  "data": { "streamUrl": "/ai/chat?edit_mode=true" }
}

// Response（拦截）
{
  "code": 400,
  "msg": "该消息已触发业务操作，无法编辑",
  "data": {
    "errorCode": "AI_EDIT_BLOCKED_HAS_SIDE_EFFECT",
    "executedTools": [
      { "toolName": "user.import", "toolCallId": "xxx", "summary": "导入 50 个用户" }
    ]
  }
}
```

实现细节：
- 后端 service `edit_message(db, cid, mid, new_content, new_parts, user_id)`：
  1. 查 `ai_operation_log WHERE conversation_id=cid AND user_message_id=mid AND status='success'`
  2. 有记录 → raise `BusinessRuleException("...has side effect", error_code="AI_EDIT_BLOCKED_HAS_SIDE_EFFECT")`
  3. 无记录 → `UPDATE ai_message SET is_active=False WHERE conversation_id=cid AND id >= mid`
  4. INSERT 新 user message（`is_active=True`）
  5. 触发 LLM 流（与现有 `/ai/chat` 共享 chat_service）

### 5.2 前端 `editAndResend` 改造

```ts
async function editAndResend(messageId: string, newContent: string) {
  if (!newContent.trim() || isStreaming.value) return;

  // 调用新 edit API（后端会做副作用检查 + soft delete + insert）
  const { data, error } = await fetchEditMessage(messageId, newContent);
  if (error) {
    // AI_EDIT_BLOCKED_HAS_SIDE_EFFECT → 已通过 chat-message disabled 拦截，兜底
    window.$message?.error(error.message);
    return;
  }

  // 本地同步：slice 到该 message 之前的消息 + push 新 user message
  const idx = currentMessages.value.findIndex(m => m.messageId === messageId);
  if (idx >= 0) {
    currentMessages.value = [
      ...currentMessages.value.slice(0, idx),
      { /* new user message from data */ },
    ];
  }

  await doStream();
}
```

注意：参数从 `messageIndex: number` 改为 `messageId: string`（更稳定，避免 v-for idx 漂移）。

### 5.3 前端 chat-message.vue 改造

```ts
// 是否允许编辑（基于 tool_call 副作用）
const editBlockedReason = computed(() => {
  if (props.isStreaming) return 'page.ai.chat.editBlockedStreaming';
  // 找到本消息之后所有 success tool_call
  const afterMessages = aiStore.currentMessages.slice(props.index + 1);
  for (const msg of afterMessages) {
    if (msg.toolCalls?.some(tc => tc.ok)) {
      return 'page.ai.chat.editBlockedHasSideEffect';
    }
  }
  return null;
});

const canEdit = computed(() => props.message.role === 'user' && !editBlockedReason.value);
```

模板：
```vue
<NTooltip :disabled="!editBlockedReason">
  <template #trigger>
    <button :disabled="!canEdit" @click="startEdit">编辑</button>
  </template>
  {{ t(editBlockedReason) }}
</NTooltip>
```

---

## 6. 测试覆盖

### 6.1 后端单元测试（pytest）

| 测试 | 覆盖决策 |
|---|---|
| `test_edit_blocked_when_tool_call_success` | D.1 — user 消息后 success tool_call → 拒绝编辑 |
| `test_edit_allowed_when_tool_call_failed` | D.1 — tool_call status=failed → 允许编辑 |
| `test_edit_allowed_when_no_tool_call` | D.1 — 纯文本对话 → 允许编辑 |
| `test_edit_soft_deletes_from_target_to_end` | D.3 — soft delete 范围正确 |
| `test_edit_preserves_inactive_messages_for_audit` | D.2 — is_active=False 消息仍在数据库 |
| `test_select_messages_filters_is_active` | D.2 — 常规查询不返回 inactive |
| `test_edit_blocked_during_pending_confirmation` | D.4 — HITL pending 中拒绝 |
| `test_regenerate_blocked_when_tool_call_success` | D.6 — regenerate 同样拦截 |

### 6.2 前端单元测试（vitest）

| 测试 | 覆盖决策 |
|---|---|
| `editButtonDisabled_whenHasSuccessToolCall` | D.5 — 按钮 disabled |
| `editButtonEnabled_whenOnlyFailedToolCall` | D.1 — failed 不拦截 |
| `editBlockedReasonTooltipShown` | D.5 — tooltip 显示原因 |
| `editBlockedStreaming` | D.4 — streaming 中 disabled |

### 6.3 E2E（Playwright）

| 场景 | 验证 |
|---|---|
| 纯文本对话编辑 | 发"你好" → 编辑成"你好，很高兴认识你" → 对话变 2 条（不是 4 条） |
| 导入用户后编辑拦截 | 上传 Excel → 导入成功 → hover 编辑按钮 → tooltip 显示"已触发业务操作" → 按钮 disabled |
| 编辑中间消息 | 3 轮对话 → 编辑第 1 条 → 第 2、3 条消失（is_active=False）→ 新流从编辑后开始 |
| 编辑历史回放（Phase 2） | 编辑后切换到审计视图 → 看到原版本 + 新版本 |

---

## 7. Plan / Task 状态块

### Phase 1（核心语义，本期）

- [ ] Task 1：后端 `AiMessage.is_active` 字段 + migration + `select_messages_by_conversation` 加 filter
- [ ] Task 2：后端 `edit_message` service + `POST /ai/conversations/{cid}/messages/{mid}/edit` API
- [ ] Task 3：后端 `ai_operation_log` JOIN 查询判断副作用（决策 D.1 算法）
- [ ] Task 4：后端 pytest 覆盖 §6.1 8 个测试
- [ ] Task 5：前端 `fetchEditMessage` API wrapper + `editAndResend` 改造（参数从 index → messageId）
- [ ] Task 6：前端 chat-message.vue `editBlockedReason` computed + tooltip + disabled 按钮
- [ ] Task 7：前端 vitest 覆盖 §6.2 4 个测试
- [ ] Task 8：E2E 覆盖 §6.3 前 3 个场景
- [ ] Task 9：i18n key 补充（`page.ai.chat.editBlockedStreaming` / `editBlockedHasSideEffect` / `editBlockedPendingConfirmation`）
- [ ] Task 10：本 spec §7 状态块改 ✅ + ship-time 决策补齐

### Phase 2（审计回放，下期）

- [ ] Task 11：审计页面 `/ai/audit/conversations/{cid}` 显示 inactive 消息
- [ ] Task 12：编辑历史 diff 视图（原始 vs 编辑后）
- [ ] Task 13：批量导出对话审计 JSON（含 inactive）

### Phase 3（延伸，按需）

- [ ] Task 14：regenerate 同步走 edit API（D.6 完整实现）
- [ ] Task 15：高危操作（IoT 设备控制、批量删除）独立"二次确认 + 操作日志回滚指引"流程
- [ ] Task 16：跨模块（CRM/ERP/OA/IoT）tool_call 副作用声明标准化（每个 tool 自描述 reversibility）

---

## 8. 范围外

- ChatGPT 风格分支树（`parent_message_id` 字段保留但不本 spec 启用）
- AI 主动撤销 tool call 副作用（"反操作"由用户在原业务页面手动完成）
- 跨 conversation 的编辑历史合并
- 移动端编辑体验（H5/小程序的编辑 UI 适配，等桌面端稳定后再做）
- LLM 上下文重放：编辑后的新流，LLM 看到的 history 是否包含 inactive 消息（**默认不包含**，与 UI 一致）

---

## 9. 开放问题

无。原讨论中的 A/B/C/D 选型已在 §3 ADR 闭环。如未来出现：

- 用户反馈"编辑被拦截太严格" → 考虑加 D.5 的"复制原文 + 一键新消息"快捷路径
- 审计需求升级到"对话级回放（含 streaming 中间态）" → Phase 2 加 stream_events 持久化

会回写本 spec 新增决策。

---

## 10. 关联

- 本 spec 是 hohu 生态 AI 模块的"行为契约"基础，CRM/ERP/OA/IoT 模块的 AI tool 实现都依赖此语义保证副作用不重复执行
- 与 [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) 互补：tool gateway 定义"如何调 tool"，本 spec 定义"如何不重复调 tool"
- 与 [`2026-08-05-chat-tool-card-embed-in-message.md`](./2026-08-05-chat-tool-card-embed-in-message.md) 正交：嵌入位置 vs 编辑语义互不影响
