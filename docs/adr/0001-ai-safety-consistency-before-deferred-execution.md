# ADR-0001: AI 延迟执行前先完成安全与一致性闭环

- **Status**: Accepted
- **Date**: 2026-08-06
- **Deciders**: hohu core team
- **Tags**: ai / backend / frontend / security / audit / architecture

## Context（背景）

HoHu 已经形成以声明式 `@ai_tool`、Tool Registry、Gateway Executor、RBAC/Data Scope、HITL 和 `ai_operation_log` 为核心的 AI Tool Gateway。用户导入导出、工具结果卡片和消息编辑进一步暴露了当前阶段的主要风险：同步边界需要可预测，工具执行与消息展示需要一致，编辑或重新生成不能重复业务副作用，审计事实必须可以沿消息追溯。

完整异步方案的方向并没有错：长任务最终可能需要独立 Worker、持久任务状态、断线后查询或推送进度，以及批量业务复核。但当前用户导入限制为 2000 行、导出同步限制为 5000 行，已有同步路径能够覆盖管理后台的主要场景；现阶段尚无负载数据或 SLA 证明必须同时引入 ARQ/Worker、deferred execution、多套实时通道和通用行级 Chat HITL。

如果现在把这些能力一次性加入主链路，会同时扩大事务、幂等、重试、取消、消息接管、跨进程恢复和前端状态同步的状态空间，反而延迟当前已经发现的安全与一致性缺口。反过来，如果永远停留在同步执行，也会在真实的大任务或断线存活需求出现后形成容量上限。因此需要冻结当前边界，并给 v1.5+ 设定可验证的重启条件。

## Decision（决策）

**保留 AI Tool Gateway 核心架构，当前版本优先完成安全、一致性和审计闭环；延迟执行及其配套基础设施按证据推迟到 `ai-tool-gateway v1.5+`，且各能力独立触发、独立设计。**

### 1. 保留现有核心分层

- 继续以 `@ai_tool` metadata、Tool Registry、Gateway Executor、权限与 Data Scope、风险分级/HITL、独立 tool session 和 `ai_operation_log` 作为唯一工具执行主链路。
- 当前迭代只收紧同步上限、上传与权限边界、事务和幂等、消息持久化、执行因果关联及 edit/regenerate 副作用防线；不为异步化重写 Gateway。
- 超出已声明同步上限的请求继续显式拒绝并给出分批处理指引，不返回伪异步任务或无法兑现的进度承诺。

### 2. 审批与调度是两个正交维度

- `approval_mode` 表示**是否需要人批准**，取值为 `autonomous | hitl`。
- `dispatch_mode` 表示**在哪里以及何时执行**，取值为 `inline | deferred`。
- 四种组合在模型上都合法；例如长时间只读导出可以是 `autonomous + deferred`，短时间高风险写操作可以是 `hitl + inline`。不得用 `hitl` 暗示异步，也不得用 `deferred` 绕过审批。
- 现有持久化字段 `execution_mode` 在兼容期仍只承载 `autonomous | hitl`；未来引入 deferred 时必须新增独立 `dispatch_mode`，不能向同一枚举混入 `inline/deferred`。

### 3. 执行事实与 UI 投影分离

- `ai_operation_log` 是工具执行的事实源，负责记录 tool call 状态、风险、参数/结果摘要、时间和来源消息关联。权限判定、编辑/重新生成副作用检查及审计查询必须读取该事实源。
- `ai_message.tool_calls` 是归属于 assistant message 的历史 UI 投影，用于 reload 后恢复卡片位置和结果展示；它可以由执行事实生成或补偿，但不得作为安全授权或副作用判定的唯一依据。
- streaming buffer 只承载当前运行的临时展示；持久消息接管成功后由 `message.tool_calls` 展示历史。接管失败时不得先丢弃临时卡片。
- `AiToolMeta.readonly` 与 `idempotent` 都是安全契约，不是 UI 提示：只有除 Gateway 自身审计与短期 query cache 外不产生持久副作用的 tool 才能为 `readonly=true`；只有天然幂等或具备稳定幂等键/结果复用协议的 tool 才能为 `idempotent=true`。当前 edit/regenerate 判断必须把 `readonly` 快照进 operation log，任何自动重试必须读取经审计的 `idempotent`；未知或元数据不可信时分别按 write / non-idempotent 保守处理。像 `user.import_preview` 这样每次创建 batch/file 的预检，以及每次创建 ExportTask/file 的 `user.export`，均不得声明可自动重放。
- `file_id` 是受保护资源引用，不因其不属于用户 data scope 就免除授权。任何 AI tool 读取文件前必须校验 owner、`tenant_id`、业务类型、删除状态、扩展名/MIME/magic bytes、大小和私有存储根路径；tenant 只能由认证中间件/服务端 resolver 注入执行上下文并在 HITL resume 时复核，禁止信任客户端字段。不存在与跨 owner/tenant 使用同一拒绝语义，避免 ID 枚举。
- 每次 ChatCommand 使用请求发出前已知且持久化的 run trace；assistant/operation 与 source user 建立因果关系，持久消息/终态先 commit 再发 `done`。conversation run guard 必须跨 SSE 断开和 HITL pending 延续，所有 resume、超时、拒绝与启动清理路径共用 terminal finalizer 并按 owner token 释放。regenerate 只有成功新回答才能替换旧 active assistant。

### 4. Chat HITL 不承担通用行级业务审核

- Chat HITL 的边界保持为“批准或拒绝一个 tool call”，可以展示摘要和影响范围，但不扩展成通用表格编辑器或逐行勾选协议。
- 需要逐行选择、多人指派、复核意见或长期待办的批量操作，使用业务原生审核页和持久化审核对象；聊天仅返回 `reviewRef`（或等价引用）跳转到该页面。
- 执行端必须绑定审核对象、不可变数据快照/hash 和操作者，避免审核后输入被替换。用户导入已有的 preview token/batch 机制继续作为同步场景基础。

### 5. v1.5+ 按量化证据重启，而非按日期自动启动

以下触发器针对能力分别生效；满足任一项只表示应启动对应专项 spec 和容量验证，不代表一次性实现全部异步栈：

| 能力 | 可验证触发条件 | 证据来源 |
|---|---|---|
| deferred execution + Worker | 任一生产部署滚动 30 天窗口内，超同步上限拒绝达到 20 次或占同类请求至少 5%；或滚动 7 天窗口内有效样本不少于 100 次，且该窗口 `p95 >= 10s` 或超时/非用户取消率 `>= 1%` | 去重的 canonical terminal observation event（当前用户批量场景为 `user_bulk_request_terminal`）；batch/task/operation log 只作审计对账 |
| 断线/进程重启后继续执行 | 出现已确认的业务 SLA，明确要求客户端断线或 Web 进程重启后任务继续，且该验收场景无法由 inline 模型通过 | 已批准需求、故障复现和验收测试 |
| 任务进度查询或推送 | deferred 任务在滚动 7 天窗口内 `p95 >= 30s`，并且产品验收要求展示至少两个中间阶段；否则只提供最终状态，不新增第二实时通道 | 任务时长指标、产品验收用例 |
| 行级业务审核 / `reviewRef` | 至少一个已批准业务流程要求单批超过 100 行的部分通过/拒绝，或要求审核人指派、意见与 SLA，无法用整次 tool call 的 yes/no 表达 | 业务 spec、审核样例与验收矩阵 |

触发后仍须在专项 spec 中确定任务状态机、幂等键、重试/取消语义、存储和单一主要进度协议。不得预设 ARQ、WebSocket、SSE 或轮询中的具体组合；选型必须由当时的部署模型和测量数据决定，并避免为同一状态维护两个权威实时源。

本 ADR 生效后，核心 Gateway spec §14 / §16.2 中“ARQ + WebSocket/Redis pub/sub”及“超限自动异步”的旧 Roadmap 表述只视为历史候选，不再构成实现承诺；是否采用及采用哪一种技术必须重新经过上述门槛和专项 spec。

## Alternatives Considered（备选方案）

### 备选 A: 立即完成完整 Phase 3

现在同时引入 ARQ/Worker、deferred 状态机、SSE + WebSocket/轮询、恢复和行级 HITL。

- ✅ 一次覆盖未来的大任务与进度体验。
- ❌ 当前缺少负载/SLA 证据，状态空间和运维成本显著增加，并会推迟已知的副作用防护、持久化和审计问题。

### 备选 B: 永久保持同步并要求用户手工分批

- ✅ 实现和部署最简单。
- ❌ 无法满足未来长任务的可靠执行和断线存活要求，也会把稳定性责任长期转嫁给用户。

### 备选 C: 把批量行级复核扩进通用 Chat HITL

- ✅ 所有确认看起来都在同一个聊天交互中完成。
- ❌ 行选择、权限、审核意见、任务指派和长期待办是业务工作流语义，会污染 Gateway 通用协议并使移动端、审计和恢复同时复杂化。

## Consequences（后果）

### 正面

- 现有 Gateway 投资得以保留，近期工作直接消除重复执行、审计断链和消息展示漂移。
- 审批、执行调度、业务审核三类概念不再混用，未来可以按需独立演进。
- v1.5+ 由可观测数据和明确 SLA 触发，避免为了假设场景提前承担队列与多通道运维成本。

### 负面 / 已知 trade-off

- 超过导入/导出同步上限的用户当前仍需分批，尚不能获得后台任务和实时进度。
- inline 请求不能承诺在客户端断线或 Web 进程重启后继续。
- 未来触发 deferred 后仍需新增任务持久化、Worker 运维、幂等/重试/取消及迁移方案；本决策只是延后，不是消除该成本。
- 为判断触发器，需要持续保存可按逻辑请求去重的 canonical terminal event，并由其派生错误码、终态和耗时等低基数指标；缺少观测数据时不得以主观感觉宣布“必须异步化”。

### 后续行动

- [x] 修订用户导入导出 spec：冻结当前同步安全边界，并把完整异步 Phase 3 指向本 ADR 的触发条件。
- [x] 修订工具卡嵌入 spec：落实 execution fact、持久消息投影与 streaming 临时态的职责边界。
- [x] 修订消息编辑 spec：以 `ai_operation_log` 和来源消息关联实施 edit/regenerate 的统一副作用防线。
- [x] ✅ 2026-08-07：完成 16 个内置 tool 的 `readonly/idempotent` 精确审计；`user.import_preview` 与 `user.export` 均为 write/non-idempotent，未知 metadata 保守降级，静态门禁 fail-closed 扫描 16/16。
- [x] ✅ 2026-08-07：完成 AI 文件引用边界：`sys_file` 新上传绑定认证 owner/tenant，历史 owner 不按可复用 username 回填；trusted tenant 贯穿 context/HITL；受保护 loader、私有存储根、XLSX 解压预算、静态历史文件隔离和文件/导出 ownership scope 已收口。
- [ ] 先完成稳定 run trace、source/parent 因果键、conversation guard、action/outcome finalizer 和 durability/projection handoff，再开放 edit/regenerate。
- [ ] 在实现收尾时补充按 request key 去重的 canonical terminal observation event，由其派生错误码、耗时和终态统计，确保滚动 7/30 天窗口可查询。
- [ ] 任一触发条件满足后，为对应能力新建 v1.5+ spec；不得直接把本 ADR 当作实现设计。

## References（参考）

- 核心 Gateway: [`2026-07-02-ai-tool-gateway-design.md`](../specs/2026-07-02-ai-tool-gateway-design.md) §1、§2、§6、§8、§14
- 用户导入导出: [`2026-08-01-user-import-export-design.md`](../specs/2026-08-01-user-import-export-design.md) §2.6、§2.8、§2.10、§7、§10 Phase 3
- 工具卡片归属: [`2026-08-05-chat-tool-card-embed-in-message.md`](../specs/2026-08-05-chat-tool-card-embed-in-message.md) §2-§3
- 消息编辑副作用: [`2026-08-06-ai-message-edit-semantics.md`](../specs/2026-08-06-ai-message-edit-semantics.md) §2-§7

---

## 决策记录（事后追加，原文不动）

- 2026-08-07：Safety Gate + Task 35 已实施。前端 edit/regenerate 入口保持关闭，直到稳定 trace/source、conversation guard、统一 finalizer 与 durability/projection handoff 完成；本次实现不构成开放编辑功能的授权。
- 2026-08-07：历史文件采用 fail-closed 升级策略：无法由不可变 ID 证明 owner 的 `sys_file` 不回填；旧 `uploads/file_storage` 只作认证读取 fallback，公共静态挂载拒绝 artifact namespace 与历史文档后缀。反向代理必须同步 deny，不能把 storage key 当秘密。
- 2026-08-07：`.xls` 因当前解析栈没有受维护的 BIFF parser，从导入/AI/UI 契约移除；通用上传扩展名兼容不代表 AI 可解析。重新支持必须先有独立 parser、安全预算和正反样本。
- 2026-08-07：部署边缘同步实施历史 artifact deny：内置/外部 Nginx 对 legacy namespace 与敏感文档后缀返回 404；API/Scheduler 共享持久化私有卷，禁止把短期 artifact 留在容器 writable layer。该部署闭环属于 Task 35，不触发 deferred/Worker Phase 3。
