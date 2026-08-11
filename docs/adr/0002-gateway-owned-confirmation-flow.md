# ADR-0002: AI 操作确认编排由 Gateway 统一负责

- **Status**: Accepted
- **Date**: 2026-08-07
- **Deciders**: hohu core team
- **Tags**: ai / backend / frontend / security / audit / architecture

## Context（背景）

HoHu 当前的 AI Tool Gateway 已能在 LLM 调用某个具体高风险 tool 后，根据 `risk`、`hitl_always`、dry-run 和权限结果发出 `confirmation_required`，再由前端展示 HITL 抽屉。这能覆盖“单个 tool 执行前确认”，但不能表达两个 tool 之间的受控关系。

用户导入暴露了这一缺口：`user.import_preview` 负责生成预览和 `preview_token`，`user.import_execute` 负责执行，现有 spec 却要求 LLM 在预览后用自然语言请求确认，并在用户回复后再次调用 execute。实际运行中，LLM 可能只输出一段 Markdown“请确认”，而没有调用 execute；Gateway 因而不知道需要创建确认，前端也没有结构化状态可以弹出抽屉。即使 LLM 后续调用 execute，也可能遗漏或改写参数。Prompt、模型行为和自由文本不能成为 TOB 系统的授权控制面。

HoHu 还需要同时覆盖三类交互：直接调用高风险 tool 时确认；先生成业务预览、再确认与该预览绑定的执行动作；用户明确只要求查看时仅返回预览。不同模型、部署方 Prompt、Web/App/Desktop 客户端都必须得到一致、可恢复、可审计的语义，不能让每个业务 tool 或客户端各自拼接确认流程。

本决策受 [ADR-0001](./0001-ai-safety-consistency-before-deferred-execution.md) 约束：当前继续使用 AI Tool Gateway 主链，优先完成安全与一致性闭环；确认与执行调度正交，本期执行仍为 `inline`，不因此引入 ARQ/Worker、DAG、第二实时通道或通用行级业务审核。

## Decision（决策）

**所有 AI 操作的确认编排由 Gateway 统一负责：LLM 只表达用户意图并发起直接调用或预览；Gateway 创建不可变的 `PreparedAction`、发出结构化确认状态，并在用户批准后使用服务端冻结参数调用绑定的执行能力。**

### 1. 确认拓扑与审批、调度分离

Gateway 协议增加独立维度 `interaction_flow`：

- `direct`：LLM 发起一个具体 tool call。Gateway 继续按风险策略计算 `approval_mode=autonomous|hitl`；`autonomous` 直接执行，`hitl` 先创建待确认动作。
- `prepared`：LLM 发起 prepare/preview tool，并表达 `requested_outcome=preview_only|execute_if_approved`。`preview_only` 只返回结构化预览，不创建待确认动作；`execute_if_approved` 由 Gateway 在 preview 成功后自动创建与预览绑定的待确认动作，不再把结果交给 LLM 决定是否、何时请求确认。

`interaction_flow` 不替代 ADR-0001 的 `approval_mode` 和未来的 `dispatch_mode`。当前 `prepared + execute_if_approved` 必须为 `hitl + inline`；未来若批准后改为 `deferred`，也不得改变批准对象、冻结参数和审计语义。

`requested_outcome` 是 LLM 对用户意图的结构化表达，不是授权：误判为 `execute_if_approved` 最多产生一个可拒绝的确认，不能自动执行；误判为 `preview_only` 不会调用绑定的 execute（preview 自身仍可能创建 batch/file 等受控 artifact）。用户批准只能来自认证客户端调用 Gateway 确认 API。

### 2. 所有确认统一为 `PreparedAction`

直接确认和预览后确认都必须落到同一类 Gateway `PreparedAction`。它至少持久化：

- action ID、状态、过期时间和一次性版本；
- prepare/direct tool call ID、绑定的 execute tool 标识；
- 服务端冻结的执行参数及 `args_hash`；
- preview/impact snapshot、snapshot hash 和业务对象引用；
- `user_id`、可信 `tenant_id`、agent、conversation、source user message、message revision/run trace；
- 风险、审批策略、安全展示摘要以及创建、批准、拒绝、执行的审计时间和操作者。

状态机固定为：

```text
prepared -> pending_confirmation
pending_confirmation -> approved | rejected | expired
approved -> running | failed
running -> succeeded | failed
prepared -> expired
```

状态迁移必须使用 compare-and-set；只有 `pending_confirmation` 可以由 owner 批准或拒绝，同一个 action 最多进入一次执行。`rejected`、`expired` 和任意 terminal action 永远不能恢复执行。

### 3. 批准不携带或重建业务参数

确认 API 只接受 `confirmation_id` 和 `approve|reject`，不得接受 execute tool 名、`preview_token`、文件 ID、策略或任意业务参数。批准后 Gateway 只读取 `PreparedAction` 中的绑定和冻结参数执行；浏览器、LLM 和自由文本都不能在批准阶段替换输入。

prepared flow 的 execute tool 必须是 Gateway-only capability，不暴露在 LLM tool schema 中。业务模块负责声明 prepare 结果如何生成安全展示摘要、业务引用和绑定执行参数，但不得自行发送确认事件或依赖 Prompt 调用 execute。

批准和真正执行前，Gateway 必须重新校验 action owner、可信 tenant、用户启用状态、当前权限与 Data Scope、过期时间、业务 snapshot/hash，以及 source message/revision 仍然有效。任一校验失败都进入可审计终态，不得降级为直接执行。

### 4. 业务预览与 Gateway dry-run 是不同概念

- Gateway dry-run 是同一个 direct tool 在执行前产生的短期影响估算，用于风险分类和确认摘要，不产生可供后续业务执行的授权对象。
- prepared preview 是业务级结果，可创建 batch、token 或私有 artifact，并以 snapshot/hash 绑定后续 execute。它可能有持久副作用，因此不能据“只是预览”自动标记为 `readonly=true`。

用户导入继续使用 batch、`preview_token`、文件 hash 和 records hash 作为业务绑定基础；导入策略、冲突策略或源文件发生变化时必须重新 preview，不能修改已有 action 的冻结参数。

### 5. 结构化状态驱动客户端，文本不参与授权

Gateway 在 action 进入 `pending_confirmation` 后发出结构化 `confirmation_required`，其中只包含 confirmation/action ID、过期时间和经过脱敏的 presentation；前端据此展示抽屉和消息内 pending 卡片。Gateway 负责产生状态，客户端负责投影 UI，因此“自动弹窗”不是由后端操纵界面，而是所有客户端必须实现的协议响应。

`PreparedAction` 与 operation log 是事实源，SSE 事件和 `ai_message.tool_calls` 是可重建投影。会话详情必须能够查询仍有效的 pending action，使刷新、切换会话或 SSE 接管后无需解析历史 Markdown 即可恢复确认 UI。LLM 输出的“请确认”、按钮文案或 Markdown 表格永远不构成批准，也不能触发执行。

### 6. 本能力不是工作流或业务审核引擎

一个 `PreparedAction` 只批准或拒绝一次完整 tool action，不支持逐行勾选、多级审批、审批人指派、意见、SLA、补偿或 DAG。需要这些语义时，仍按 ADR-0001 创建业务原生审核对象并通过 `reviewRef` 跳转；不得把 Gateway 确认状态机扩展成通用工作流。

## Alternatives Considered（备选方案）

### 备选 A: 继续依赖 Prompt 驱动 preview → execute

- ✅ 无需新增协议和持久化对象。
- ❌ 模型可能只生成确认文本、重复预览、跳过 execute 或改写参数；不同模型和 Prompt 的行为不可作为授权保证。

### 备选 B: 保持 execute 对 LLM 可见，仅给 execute 配置 `hitl_always`

- ✅ 能复用现有单 tool HITL。
- ❌ Gateway 仍要等待 LLM 主动调用 execute，无法保证 preview 成功后及时进入确认；LLM 还可以提交与预览不一致的参数，preview/execute 关系不是协议事实。

### 备选 C: 每个业务模块自行实现确认弹窗和状态

- ✅ 单个业务可以快速定制复杂 UI。
- ❌ Web/App/Desktop 契约分叉，权限复验、过期、幂等和审计被重复实现；第三方 tool 难以安全接入，不符合开源平台的通用扩展边界。

### 备选 D: 直接建设通用工作流、队列和行级 HITL

- ✅ 可一次覆盖长任务和复杂审批。
- ❌ 当前问题只需要一次 action 的确定性批准；提前引入任务调度、重试、补偿和多级审核会违反 ADR-0001 的证据驱动边界。

## Consequences（后果）

### 正面

- preview 完成后是否产生确认由结构化协议决定，不再受 LLM 是否输出正确话术影响。
- 批准对象、执行参数、业务 snapshot、操作者和结果形成完整审计链，可阻止换参、重复批准和跨租户执行。
- 单工具确认、两阶段确认和纯预览共享同一客户端协议，业务 tool 只声明能力与展示数据。
- 将来切换模型、增加客户端或在批准后引入 deferred execution 时，无需重写授权语义。

### 负面 / 已知 trade-off

- 需要新增持久化模型、状态迁移、过期清理、pending 查询和旧 HITL 兼容迁移，复杂度高于修补 Prompt。
- 前端必须处理持久 pending 投影及刷新恢复，不能只维护当前流中的单个临时 confirmation。
- LLM 仍负责区分用户要“只看”还是“准备执行”；误判可能多弹一次确认或少进入一次确认，但不会越过用户批准产生执行。
- 该协议只解决 action 级 yes/no，不满足逐行或多级业务审核。

### 后续行动

- [x] 修订 Gateway spec：定义 metadata、`PreparedAction` schema、状态机、事件、API、权限复验、幂等和兼容策略（2026-08-07）。
- [x] 修订用户导入导出 spec：删除 Prompt 驱动 transition，冻结导入参数，并将 `user.import_execute` 改为 Gateway-only（2026-08-07）。
- [x] 修订工具卡嵌入 spec：定义 preview/pending presentation 与 reload 恢复（2026-08-07）。
- [x] 修订消息编辑 spec：定义 source message/revision 绑定及失效规则（2026-08-07）。
- [ ] 以用户导入完成首个 `prepared + hitl + inline` 纵向切片和安全/E2E 回归，再迁移其他适用 tool。
- [x] 完成 2026-08-11 代码纠偏：所有新 direct HITL 也持久化同一 action；`requested_outcome` 保持必填；ConfirmationPresentation 统一为有序 fields DTO；后端 867 个 AI pytest 与前端 43 Vitest/typecheck/build 通过。
- [ ] 在可用 dev stack 实跑仓库内新增的三条确定性 Playwright；当前 `--list` 3/3 通过，但 9527/8000/5432 未启动，浏览器验收仍是 release gap。
- [ ] Task 36 容量观测继续后移；本决策不授权 ARQ/Worker、第二实时通道或行级 HITL。

## References（参考）

- 相关 ADR: [ADR-0001](./0001-ai-safety-consistency-before-deferred-execution.md)
- Gateway: [`2026-07-02-ai-tool-gateway-design.md`](../specs/2026-07-02-ai-tool-gateway-design.md) §5.3、§8
- 用户导入导出: [`2026-08-01-user-import-export-design.md`](../specs/2026-08-01-user-import-export-design.md) §2.14、§2.19、Task 26a、Task 29
- 工具卡片: [`2026-08-05-chat-tool-card-embed-in-message.md`](../specs/2026-08-05-chat-tool-card-embed-in-message.md) §2.7-§2.10
- 消息编辑: [`2026-08-06-ai-message-edit-semantics.md`](../specs/2026-08-06-ai-message-edit-semantics.md) D.1-D.4

---

## 决策记录（事后追加，原文不动）

- **2026-08-07**: ADR 初稿建立，状态为 Proposed；待四份关联 spec 完成协议收敛和评审后再转 Accepted。
- **2026-08-07**: 四份关联 spec 已完成协议收敛；ADR 继续保持 Proposed，待架构评审确认后转 Accepted。
- **2026-08-07**: 架构评审确认，状态转为 Accepted；后续实现按 Task 35a 分阶段落地。
