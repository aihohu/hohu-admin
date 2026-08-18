# AI 管理能力 MVP 收口与阅读入口

> 状态：已批准，实施中
> 创建日期：2026-08-14
> 影响项目：`hohu-admin`、`hohu-admin-web`
> 当前范围负责人：hohu core team
> 详细技术基线：[`AI Tool Gateway`](./2026-07-02-ai-tool-gateway-design.md)
> 实施计划：[`../plans/2026-08-14-ai-management-mvp-closure.md`](../plans/2026-08-14-ai-management-mvp-closure.md)

## 0. 本文职责与阅读规则

本文是 **当前 AI 管理 MVP 的唯一实施入口**，回答以下问题：

- 当前必须完成什么；
- 哪些能力明确延期；
- 默认开启 AI 时如何继续 fail closed；
- 用哪些业务 Agent 验证架构；
- 开发、测试和发布按什么顺序推进。

本文不复制 Gateway、PreparedAction、SSE、消息投影或导入导出的全部技术细节。专项 spec 继续是其领域内部不变量的事实源，但不再各自决定当前产品优先级。

### 0.1 后续任务默认阅读路径

1. 任何当前 AI MVP 任务先读本文。
2. 只在任务命中下表范围时，再读对应专项 spec 的相关章节。
3. 不要求为普通 AI MVP 任务遍历 `docs/specs/` 下全部 AI 文档。
4. 若专项 spec 的历史范围与本文冲突，以本文较新的 MVP 决策为准；专项技术不变量未被本文显式推翻的部分继续有效。

| 任务类型 | 追加阅读 |
|---|---|
| Registry、Gateway、HITL、权限链、SSE、审计 | [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) + ADR-0001/0002 |
| 用户管理 AI 工具 | [`2026-08-11-ai-user-management-tools.md`](./2026-08-11-ai-user-management-tools.md) |
| 用户导入导出 | [`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md) |
| 工具卡归属、reload、HITL resume/download | [`2026-08-05-chat-tool-card-embed-in-message.md`](./2026-08-05-chat-tool-card-embed-in-message.md) |
| 消息编辑/重新生成 | 当前延期；只有明确恢复该功能后才读 [`2026-08-06-ai-message-edit-semantics.md`](./2026-08-06-ai-message-edit-semantics.md) |
| Tool Result view type | [`2026-07-16-tool-result-view-design.md`](./2026-07-16-tool-result-view-design.md) |
| SSE 断线续传 | [`2026-07-13-sse-resume-design.md`](./2026-07-13-sse-resume-design.md) |

### 0.2 防止再次形成双重真相源

- 本文记录产品范围、跨模块授权链、最小 Agent 集合、验收矩阵和任务状态。
- 专项 spec 记录具体 schema、状态机、事件字段、错误码和模块内算法。
- 实现改变专项契约时，同时回写本文的任务状态和对应专项 spec；只改变排期时仅回写本文。
- 新增第四个以上业务 Agent 时，为该 Agent 单独写短 spec，并在本文 Agent 表增加链接，不向 Gateway 大 spec 继续堆实现日志。

---

## 1. 核心定位

**作为** HoHu 企业管理员，**我想要** 在默认可用的 AI 助手中安全管理用户、部门和角色，**以便** 不离开管理后台就能用自然语言完成跨模块管理，同时继续遵守现有功能权限、数据权限、人工确认和审计规则。

只完成用户管理不足以证明架构成立：用户数据主要验证普通行级 CRUD；部门还需要验证树形层级和父子范围；角色则需要验证权限集合、数据范围和 Agent 绑定本身。MVP 必须至少覆盖这三个管理领域，才能判断 Pydantic AI、Supervisor、Tool Registry 和 Gateway 是否具备可扩展性。

### 1.1 术语

| 术语 | 定义 |
|---|---|
| 模块开关 | `AI_MODULE_ENABLED`，只控制是否启用业务 AI router/service，用于紧急熔断或无 AI 部署；关闭时保留统一 disabled guard |
| AI 入口权限 | `ai:chat:use`，控制新聊天、LLM/Tool 执行和 approve；已认证 owner 的 reject/纯终态回放是无副作用收口例外 |
| Agent 绑定 | `role_ai_agent`，控制角色可见和可选择的业务 Agent |
| Tool 功能权限 | `AiToolMeta.required_perms`，控制当前用户能否调用具体业务动作 |
| 数据权限 | `DataScopeContext`，控制当前用户能查询或修改的业务行/部门集合 |
| 当前 MVP Agent | `user_mgmt`、`dept_mgmt`、`role_mgmt` |

---

## 2. 范围

### 2.1 MVP 必做

1. AI 模块默认开启，模块开关重新定义为紧急熔断，不承担日常授权。
2. 用户侧 AI 执行 API 增加后端 `ai:chat:use` 权限，不能只依赖菜单隐藏；reject/纯终态回放按 owner + tenant 独立收口。
3. 移除 `shared`、超级管理员、手工选择、粘滞选择和默认回退等 Agent 可见性旁路；所有 Agent 均通过统一 Policy 判定，fresh install 通过 `R_SUPER` 显式绑定为超级管理员授权。
4. `user_mgmt` 补齐 `user.update_dept` 和 `user.update_roles`。
5. `dept_mgmt` 从只读统计补齐最小写操作纵向切片。
6. `role_mgmt` 从只读统计补齐支持受限委派的写操作纵向切片和权限提升防护，不能退化为“仅超级管理员能写”。
7. 已完成 Agent 默认启用；fresh install 默认仅超级管理员可使用，普通角色需显式授权。
8. 使用真实浏览器、不同角色和不同数据范围账号完成端到端验收，并至少完成一次真实模型/provider smoke。
9. 工具卡当前范围完成 HITL resume、download、tool-only、reload 和最终工程门禁。
10. 完成 AI Trace 列表/详情与管理页面审计纵向切片，使页面业务结果、PreparedAction 和操作日志可以按 trace 闭环核对。

### 2.2 明确不做

- 消息编辑与重新生成；Safety Gate 继续关闭入口。
- ChatGPT 风格消息分支树或历史版本切换。
- Config/Provider 全量写工具。
- 让用户在聊天中提交 Provider API Key、密码、token 或其他密钥。
- MCP、异步 Worker、Conversation Summary、PDF/Word 解析等扩展能力。

#### 2.2.1 “Config/Provider 全量写工具”的含义

“全量写工具”是指把传统管理页面的全部新增、修改、删除、批量操作和敏感字段配置直接映射成 LLM 可调用工具；它不等于 Config/Provider 完全不接入 AI，也不排除只读、测试或受严格白名单约束的有限操作。

| Agent | 当前 MVP 不做的全量写能力 | 后续可以先做的有限能力 |
|---|---|---|
| `config_mgmt` | 任意配置 key 的新增/修改/删除、批量删除、导入，以及修改认证、安全、密钥、AI Guardrail 等关键配置 | `config.list`、`config.lookup`；在独立评审后增加仅允许安全 key 白名单的 `config.update_safe` |
| `provider_mgmt` | Provider/Model 的任意新增、修改、删除、启停、默认模型切换，以及通过聊天写入 API Key、Base URL 等连接凭据 | 脱敏的 `provider.list`、`provider.models`、`provider.test_connection`；非敏感写操作后续逐项评审 |

暂不开放全量写有三点原因：

1. Config 中包含认证策略、默认密码、上传、安全和 AI 自身配置，任意 key 写入可能绕过现有权限链或关闭安全防线。
2. Provider 包含 API Key 等密钥；聊天文本、模型上下文、消息持久化和审计链都不应承载原始密钥。并且没有可用 Provider 时 AI Agent 本身无法运行，不能依赖 Provider Agent 完成首次初始化。
3. Config/Provider 主要验证全局配置和敏感字段控制，不能替代 User/Dept/Role 对普通数据、层级数据、功能权限和数据权限的架构验证，因此不作为当前前三个 Agent 的 MVP 阻塞项。

后续开放任何写工具前必须同时满足：字段/key allowlist、敏感字段永不进入 LLM、`super_admin_only`、强制 HITL、dry-run、批准时快照复验、完整审计，以及管理页面和 AI 共用同一 Service/Policy。Provider API Key 的新增或替换继续使用传统安全表单或未来的带外 secret channel，不进入普通聊天工具参数。

### 2.3 后续候选，不阻塞 MVP

| 优先级 | Agent | 候选能力 | 约束 |
|---|---|---|---|
| P1 | `config_mgmt` | list/lookup、白名单配置 update | 敏感键、认证与安全开关禁止 AI 修改 |
| P1 | `job_mgmt` | list/lookup、update_cron、toggle | `run_now` 继续禁止；写操作 HITL |
| P2 | `provider_mgmt` | list/models/test_connection | API Key 新增/修改继续走传统 UI；避免 Provider 初始化循环和密钥进入 LLM |

是否启动候选 Agent，以前三个 Agent 的测试结果为依据，不为凑数量同时铺开。

---

## 3. 默认开启与授权架构

### 3.1 默认值

```text
AI_MODULE_ENABLED=true
```

`false` 只用于以下情况：

- 发生注入、越权、数据泄漏或模型供应商故障，需要紧急下线全部 AI 路由；
- 部署方明确选择无 AI 运行模式；
- 维护窗口需要整体隔离 AI 流量。

不能把生产默认关闭当作功能权限策略。HoHu 是 AI 管理系统，安全默认值应体现在“未授权用户无法使用”，而不是“产品能力默认不可用”。

`AI_MODULE_ENABLED=false` 时不注册业务 AI router/service，只保留 `/ai/**` disabled guard，统一返回 HTTP 503 和标准错误响应 `{code: 503, msg, data: null, errorCode: "AI_MODULE_DISABLED"}`；不得出现部分 router 404、部分 router 503，也不得初始化 Provider、Agent 或 Gateway 执行链。

### 3.2 后端授权链

```text
AI_MODULE_ENABLED
  → authentication
  → ai:chat:use（执行/完整结果路径；reject/最小状态回放例外）
  → Agent enabled + Role-Agent visibility
  → Tool required_perms
  → DataScopeContext / tenant scope
  → dry-run + risk classification + HITL
  → 批准执行前重新校验权限、范围和冻结快照
  → operation log / trace
```

任一层失败都必须在调用业务函数前终止。菜单和按钮只负责用户体验，不能替代 API 鉴权。

#### 3.2.1 Agent 授权必须覆盖全部选择路径

Agent 可见性和可执行性由唯一 `authorize_agent_access(user, agent_code)` Policy 判定，列表、聊天和 Gateway 不得分别实现近似规则。Policy 同时要求：

1. Agent 存在且 `enabled=true`；
2. 当前用户至少一个启用角色与该 Agent 存在启用的 Role-Agent 绑定；`R_SUPER` 也通过 seed 的显式绑定获得可见性，不保留代码旁路；
3. 当前用户对该 Agent 至少有一个可见 Tool，Tool 仍独立校验 `required_perms`；
4. Agent、Role-Agent、Tool 与当前用户位于同一可信 tenant 上下文。

以下来源必须全部调用该 Policy：Agent 列表、请求中的显式 `agentCode`、会话粘滞 Agent、Supervisor 候选集和最终结果、Supervisor 关闭时的默认选择、空消息/注入拦截后的 fallback，以及 approve/执行型 resume 恢复的 PreparedAction Agent。reject 和无业务内容的最小状态回放不执行 Agent/Tool，不把该 Policy 当作收口前置条件；完整结果回放必须按当前入口、Agent、Tool 和 data scope 重新授权。

- 显式选择未授权 Agent 时返回 403 + `AI_AGENT_FORBIDDEN`，不能静默改用默认 Agent。
- 粘滞 Agent 被禁用或解绑后，清除粘滞结果，只能在最新可见候选集中重新路由；没有候选时返回 `AI_AGENT_NOT_AVAILABLE`。
- Gateway 在普通调用和批准执行时都校验 `tool.agent_code == authorized_agent_code`；每个 Tool 只属于一个 Agent，`shared` 只是普通 Agent code，不代表跨 Agent 调用豁免。
- pending 期间入口权限、Agent enabled、Role-Agent 绑定、Tool enabled/归属或 Tool 权限任一变化，approve/执行型 resume 均 fail closed，并在同一事务进入 failed/expired 终态后返回稳定的 stale/forbidden 错误；reject 和最小状态回放仍只按 owner + tenant 收口/读取，不能因执行权限已丢失而留下 pending，但不得返回业务 result/message 内容。

#### 3.2.2 Agent 全局配置全部属于超级管理员边界

Role 管理员可以把自己有权委派的 Agent 绑定给下级角色，但不能改变 Agent 的全局定义。`name`、`description` 会进入 Supervisor 路由 prompt，`display_order` 会影响候选顺序，三者也不是安全的普通展示字段。`PUT /ai/admin/agents/{id}` 必须执行后端授权：

- 任意可变字段都同时要求当前用户拥有启用的 `R_SUPER` 和 `ai:agent:edit`；普通角色即使拥有 `ai:agent:edit` 也整体返回 403 + `AI_AGENT_ADMIN_REQUIRED`；
- `name`、`description`、`display_order`、`enabled`、`system_prompt`、`model_preference`、`daily_quota_per_user`、`risk_appetite` 均按全局路由/执行配置处理；
- `agent_id`、`code`、`is_builtin` 始终不可修改；
- 请求包含不可变字段或调用者不满足完整授权时整体拒绝，不能忽略字段后部分成功。

上述限制由 Service/Policy 执行，不能只在 Agent 管理抽屉隐藏表单项。普通 Role 委派只改变角色绑定，不能通过路由描述注入、候选排序、启用未发布 Agent、system prompt 或 risk appetite 间接影响其他用户。

### 3.3 fresh install、upgrade 与 shared 基线

已完成且通过发布门禁的 Agent 才能 `enabled=true`；未完成的 `config_mgmt`、`provider_mgmt`、`job_mgmt` 保持禁用，描述不得承诺尚不存在的工具。seed 必须区分 fresh install 与 upgrade，不能用同一个 upsert 默认值覆盖部署方状态。

| 场景 | 模块开关 | Agent enabled | 入口权限与 Role-Agent 绑定 |
|---|---|---|---|
| fresh install | 未显式配置时为 `true` | 已完成的 `user_mgmt`、`dept_mgmt`、`role_mgmt` 和 `shared` 为 `true`；未完成 Agent 为 `false` | `R_SUPER` 获得 `ai:chat:use`，并显式绑定全部已启用 Agent；`file.parse` 写入 `ai:enabled_tools` 且仅 `R_SUPER` 获得 `ai:file:parse`；普通角色不授予入口权限或绑定 |
| upgrade：已有 Agent 行 | 保留部署值 | 保留现有 `enabled`，不得把管理员手工关闭的 Agent 翻回 `true` | 为 `R_SUPER` 幂等补 `ai:chat:use` 及全部已发布 Agent（含当前 disabled 行）的显式绑定；保留已有非 shared 绑定；已有非 shared 绑定的普通角色补 `ai:chat:use`；无绑定普通角色不补 |
| upgrade：新增已完成 Agent 行 | 保留部署值 | 新行按已完成状态设为 `true` | 只自动绑定 `R_SUPER`，不自动扩大普通角色能力 |
| upgrade：shared 旧直通用户 | 保留部署值 | 保留 shared 现有 enabled | 删除代码直通，不根据“曾经可直通”自动补权限或绑定；需要继续使用时由管理员显式授权 |

`shared` 在本 MVP 中保留为平台能力 Agent，只承载已经实现的 `file.parse`，不计入三个业务 Agent。必须删除 Role-Agent Service 对 shared 的禁止绑定及前端“无需绑定”假设；访问链固定为 `ai:chat:use` + shared Role-Agent 绑定 + `ai:file:parse`，其中 `file.parse` 保持 `default_enabled=false`，fresh install 由 seed 显式加入 `ai:enabled_tools`。upgrade 保留既有 `ai:enabled_tools`，不得把部署方未启用的 `file.parse` 自动打开；普通角色不自动获得权限或绑定。

历史 seed 已预建但 disabled 的 Agent 与管理员手工关闭的 Agent 没有可靠 provenance，upgrade 一律保留 `enabled=false`，不能猜测并自动开启；功能发布说明提示超级管理员显式启用。默认启用只适用于 fresh install 和 upgrade 中首次新增的已完成 Agent 行。

Phase 1 的入口权限迁移、shared 收口、permission/menu seed 和 Agent seed 必须一起完成；但 Phase 1 不是可独立部署单元。移除 shared/超管旁路后，`/ai/role-agent` 的普通管理员写入必须等 Phase 2 的 `GrantAuthority`、`grantable_agent_ids`、成员全局影响和锁协议全部通过，Phase 1 + Phase 2 只作为一个原子安全迁移/集成门禁合入，不代表可承接生产流量。Phase 3 还要封堵传统 writer/destructive 旁路并补齐 Agent，Phase 4 还要先完成不可可靠回填的 Trace 字段、审计和 E2E；整个 MVP 在 Phase 4 发布门禁通过前不得生产部署。任何中间构建都不能把未完成 Agent 标记为 completed/default-enabled。迁移测试必须证明部署方显式的模块开关和 Agent enabled 状态不会被覆盖。

### 3.4 AI API 权限矩阵

`ai:chat:use` 是聊天入口权限，不替代 owner、tenant、Agent、Tool 或后台管理权限。禁止给整个 `/ai/provider`、`/ai/operation-log` 等混合用途 router 统一叠加入口权限。

| 端点 | 入口权限 | 追加约束 |
|---|---|---|
| `GET /ai/agents` | `ai:chat:use` | 只返回通过 §3.2.1 Policy 且至少有一个可见 Tool 的 Agent |
| `POST /ai/chat` | `ai:chat:use` | conversation owner + tenant；所有 Agent 选择来源使用统一 Policy |
| `GET /ai/chat/resume` | 最小状态回放只要求认证 owner；完整结果回放及执行/LLM 分支要求 `ai:chat:use` | 全部分支校验 action/conversation owner + tenant；无入口或当前 Agent/Tool/data 权限时只返回 `{confirmationId,status,errorCode,finishedAt}`，完整投影重新授权，执行分支另复验模型和快照 |
| `POST /ai/confirm`（approve/reject） | approve 要求 `ai:chat:use`；reject 只要求认证 owner | 全部分支校验 action owner + tenant；approve 执行完整二次校验，reject 不执行 Tool/LLM 且必须写 rejected 终态 |
| `/ai/conversation` list/create/update | `ai:chat:use` | list 只返回当前 owner；create/update 校验 owner + tenant，列表不得嵌入 tool/result/presentation |
| `/ai/conversation/{id}` detail/history | `ai:chat:use` | owner + tenant；assistant/tool/result/pending presentation 逐项执行 §3.4.2 投影授权，失权项返回 tombstone |
| `DELETE /ai/conversation/{id}` | `ai:chat:use` | owner + tenant；按 §5 锁定并终态化未执行 action，不能遗留 pending |
| `GET /ai/query-cache/{trace_id}` | `ai:chat:use` | trace owner + tenant + TTL，并执行 §3.4.2 当前 Agent/Tool/data 投影授权；不存在与越权使用同一拒绝面 |
| `POST /ai/messages/{message_id}/routing-feedback` | `ai:chat:use` | message owner + tenant |
| `GET /ai/operation-log?tool_call_id=...` | owner 轮询分支要求 `ai:chat:use` | owner 分支执行 §3.4.2，失权时只返最小状态；`ai:trace:view/R_SUPER` 走 §3.5 脱敏审计 DTO；所有分支校验 tenant |
| `GET /ai/operation-log/traces`、`GET /ai/operation-log/traces/{trace_id}` | 不要求 `ai:chat:use` | `ai:trace:view` 或 `R_SUPER`；tenant scope + 脱敏，用于管理审计 |
| `GET /ai/chat/models` | `ai:chat:use` | 只返回 `chat-safe` 最小字段 options；执行仍需再次调用统一 selector |
| `GET /ai/admin/agents/model-options` | 不要求 `ai:chat:use` | `ai:agent:list`；只返回安全最小字段 options |
| `GET /ai/provider/models` | 不要求 `ai:chat:use` | `ai:provider:list`；仅供 Provider 管理，不得作为聊天接口复用 |
| `/ai/admin/agents/**`（除 `model-options`） | 不要求 `ai:chat:use` | 保持 `ai:agent:list/edit` |
| `POST /ai/provider/{provider_id}/test` | 不要求 `ai:chat:use` | `ai:provider:test-model`；仅已保存同租户 Provider/Model，并执行 §3.4.1 egress Policy |
| `/ai/provider/**` 管理 CRUD（除 `GET models`/`POST test`） | 不要求 `ai:chat:use` | 保持 `ai:provider:*`，不得因聊天入口权限误放行 |
| `/ai/role-agent/**` | 不要求 `ai:chat:use` | 保持 `system:role:ai-agent-auth` 并叠加 §4.3 委派 Policy；普通写入通过 Phase 1 + 2 原子集成门禁，不允许旧 Policy 过渡或中间构建生产上线 |
| `/ai/routing-feedback/**` 管理查询 | 不要求 `ai:chat:use` | 保持 `ai:routing-feedback:list` |

`POST /ai/confirm` 和 `GET /ai/chat/resume` 是分支权限端点，router 依赖只能先做 authentication，不能统一挂 `require_permissions("ai:chat:use")`。Service 必须先按 tenant + owner 无泄露地加载并锁定 action，再区分 reject/最小状态回放、完整结果回放与 approve/执行分支；approve/执行分支缺入口/Agent/Tool 权限时在同一事务写 failed/expired 终态后返回 403/stale，reject 不受执行权限阻断，回放缺少当前入口/Agent/Tool/data 权限时只能返回最小状态 DTO。

完整结果回放只能根据 PreparedAction 冻结的 `agent_code`、`execute_tool_name` 和完整 `subject_ref` 稳定 ID 重新执行 `authorize_agent_access()`、Tool permission 与 `authorize_replay_subjects()`；不得解析 `result_data/result_ui` 猜目标。legacy action 缺少可证明完整的 subject refs、目标已跨 tenant 或当前 data scope 无法覆盖任一目标时，一律降级为最小状态 DTO，不能因结果已持久化而恢复历史读取授权。

当前双用途 `GET /ai/provider/models` 拆分为三个单一职责契约：聊天模型选择改用 `GET /ai/chat/models`，要求 `ai:chat:use`；Agent 管理抽屉改用 `GET /ai/admin/agents/model-options`，要求 `ai:agent:list`；`GET /ai/provider/models` 只保留 Provider 管理用途，要求 `ai:provider:list`。前两个 option endpoint 的 `data` 固定为 `[{modelId: string, label: string, providerCode: string, capabilities: ["text", ...]}]`，不返回 Base URL、密钥或管理字段。前后端必须同步迁移，不能用“是否显示在某个页面”推断权限。

`chat-safe` 明确定义为 model/provider 均启用、`capabilities` 包含 `text`、符合当前 Agent 模型约束、属于当前 tenant，且 Provider URL 与 Model 级 `base_url` override 均通过 §3.4.1 egress Policy。模型列表不是 `POST /ai/chat` 的授权证明；`GET /ai/chat/models`、显式 `modelId`、Agent 默认模型、conversation create/update，以及每一次开始或重新进入 LLM run 前必须复用同一 `authorize_chat_model()`。显式提交不合规 model ID 时返回 400 + `AI_MODEL_NOT_AVAILABLE`，不得静默 fallback 到默认模型。

PreparedAction 创建时冻结发起该轮的稳定 `model_id/provider_id` 并纳入 canonical snapshot，不能在批准时从可变 `conversation.model_id` 反推。Alembic 为 `ai_prepared_action` 新增可空、无级联删除的 `resolved_model_id/resolved_provider_id BIGINT`；新建 action 在应用层强制两者存在，legacy NULL 行禁止从 Conversation 回填，只允许 reject、最小状态回放及不依赖后续模型的确定性工具收口。`reject` 只校验 action owner + tenant 并必须收口为 rejected 终态，不调用模型 selector；回放已持久化事件不校验模型，但完整业务结果仍按当前入口/Agent/Tool/data scope 授权，失权时只返回最小状态 DTO。`approve` 先按 Agent/Tool/权限/目标快照执行并可靠写入终态；只有后续确实要新发起 LLM continuation 时才校验冻结模型。冻结模型失效时不得调用模型，也不得让 action 留在 pending：已经执行的工具保留 succeeded/failed 终态和确定性结果投影，continuation 记录稳定 `AI_MODEL_NOT_AVAILABLE`；尚未执行且入口明确要求先完成模型调用的路径则以 failed/expired 终态退出。

#### 3.4.1 Provider 全部出站网络边界

Provider test 不是任意 URL 代理。删除现有接受任意字典的 `POST /ai/provider/test-model`，不得保留兼容 alias；Web 改为先保存 Provider/Model，再调用 `POST /ai/provider/{provider_id}/test`，body 固定为 `{modelId: string}`。新请求只能引用已保存、当前 tenant 可管理且确属该 Provider 的模型，不接受临时 `baseUrl`、原始凭据或任意请求字典，并继续要求 `ai:provider:test-model`。

统一 egress Policy 必须覆盖 Provider/Model 保存、test、普通 chat、Supervisor 路由、Agent run、批准后的 LLM continuation 以及未来 embedding/后台任务等每一个出站调用；Provider `base_url` 和 Model 级 override 都校验，不能只加固 test endpoint：

- 对 adapter/config 归一化后的最终 effective URL 校验；只允许 canonical Provider `base_url` 和 Model override 影响目的地，拒绝 URL userinfo、未知 transport/proxy/endpoint 配置键。默认仅允许 HTTPS 和部署配置的明确 Provider origin/端口；loopback、private、link-local、multicast、unspecified、reserved 地址全部拒绝，本地模型只能由部署方在受控配置中显式加入精确 origin/CIDR，不能由 API payload 放宽；
- 每次连接解析并校验全部 DNS 结果，连接固定到已校验地址并保持正确 TLS SNI/Host；默认禁止重定向，若未来允许则每一跳重新执行同一校验，阻断 DNS rebinding 和 redirect 绕行；
- 限制 connect/read/total timeout、响应体大小和并发数，不返回上游响应原文、解析后的密钥或堆栈；网络错误统一清洗为稳定错误码；
- URL/地址不合规时保存/test 返回 400 + `AI_PROVIDER_URL_FORBIDDEN`；运行时 selector 返回 `AI_MODEL_NOT_AVAILABLE` 并记录脱敏 security event，所有失败只暴露 Provider/model 稳定标识与分类结果。

Provider SDK/Pydantic AI model 必须注入同一个 hardened transport 或经过受控 egress proxy，默认忽略环境代理，只有部署配置的 egress proxy 可用；禁止任一 adapter 自行创建绕过 Policy 的 HTTP client。每次重试/重连都重新校验，不能把保存时校验当作永久授权。页面隐藏输入框或只在前端校验 URL 都不构成安全边界。

upgrade 运行 `python scripts/audit_ai_provider_egress.py` 检查全部存量 Provider URL 和 Model override。未命中当前部署 allowlist 的记录保留原始 `enabled` 配置但进入运行时 quarantine：不出现在 chat-safe options、不能被 Supervisor/default/显式 model ID 选择、test 和所有模型调用 fail closed，Provider 页面显示稳定 `EGRESS_POLICY_BLOCKED`，直到部署方修改 URL 或受控 allowlist；禁止为兼容旧配置自动放行私网地址。

#### 3.4.2 业务结果投影必须在每个读取面重新授权

撤销 `ai:chat:use`、Role-Agent、Tool 权限或 data scope 后，历史持久化不能成为继续读取业务结果的能力票据。统一 `authorize_result_projection(user, projection_ref)` 必须覆盖 resume/SSE replay、conversation detail/history、pending action presentation、query-cache、owner operation-log 轮询、tool-result download/file retrieval，以及任何返回 assistant/tool `content/result_data/result_ui` 的接口；前端 store 也不得从旧缓存恢复后端已判定不可见的内容。

- 新写入的 assistant/tool message、PreparedAction 和 query-cache entry 必须持久化 immutable `agent_code`、完整 tool code 集合、tenant、规范化 `subject_refs` 稳定 ID/类型及 canonical hash；聚合/统计结果无法用有限目标 ID 完整表达时，额外冻结当时的 canonical `data_scope_hash` 和 resolver version。message 只保存这些授权引用，不把权限布尔值当作未来凭据。
- 新一轮 assistant/tool 输出还必须冻结该会话全部既有 active assistant 投影的 message ID，并把该依赖集合传入本轮 PreparedAction、query-cache 和下载 token；读取时递归重验所有依赖的不可变 lineage。依赖记录缺失、legacy `NULL`、跨 owner、引用 user message 或任一依赖失权时整体 fail closed；不能只记录本轮 Tool 或只收集当前仍获准的旧消息，否则会把已撤权结果洗白为新输出。
- 完整投影要求 owner + tenant、当前 `ai:chat:use`、全部关联 Agent/Tool 仍可访问，并由领域 Policy 对全部有限 `subject_refs` 重新检查当前 data scope；聚合/统计结果则要求当前 resolver version 与 `data_scope_hash` 精确一致，否则拒绝回放。不得解析自然语言 content、result 或 presentation 猜测目标，也不得只检查其中一个目标。
- 任一检查失败或 legacy 记录无法从不可变 PreparedAction/operation-log lineage 证明完整引用时，conversation/detail 与 history 保留用户自己的 message 和顺序，但把对应 assistant/tool message 整体替换为 `{messageId,role,status:"redacted",errorCode:"AI_RESULT_PROJECTION_FORBIDDEN"}`；pending action 只留 `{confirmationId,status,errorCode,finishedAt}`，query-cache 返回与不存在相同的 404，owner operation-log 只返回最小状态。
- download token/URL 只能在投影授权通过后签发，绑定 owner + tenant + projection hash、短 TTL 且不可被其他用户复用；签名 key 必须与 API access JWT 密码学分域，通用 API 认证只接受显式 `type=access`，无 type、refresh 和 download token 一律拒绝。tokenized URL 只能进入不送给模型的 UI projection，`ToolResult.data`/LLM prompt 不得包含 bearer token；实际文件读取再次执行同一 Policy，旧 URL 不能成为撤权后仍有效的 bearer capability。
- scope-bound PreparedAction 执行前必须把发起时冻结的 `data_scope_hash` 与当前统一 resolver 结果精确比较，漂移时在任何业务副作用前返回 `AI_PREPARED_ACTION_SNAPSHOT_STALE`。resume 长等待前的授权结果不得跨等待复用；读取成功终态的 `result_data/result_ui` 前必须针对重新加载的 durable action 再次授权。产生外部文件的 Tool 必须在写文件前预授权，若授权在生成期间撤销，则删除未提交文件后 fail closed。
- `ai:trace:view/R_SUPER` 审计分支不复用 owner 历史读取权，而是严格返回 §3.5 allowlist DTO；不能借审计权限恢复 raw message/result/args。权限重新授予后仍按当次实时 Policy 计算，不缓存旧 allow 结论。

### 3.5 AI Trace 数据与页面契约

AI Trace 不能依赖当前 conversation 的可变 `agent_code` 或只在 PreparedAction 中存在的 tenant 信息反推。P1-A 审查修复已先行通过 Alembic 为 `ai_operation_log` 固化 tenant 边界；Phase 4 继续补齐其余 Trace 字段：

- ✅ `tenant_id BIGINT NOT NULL` 已由 `b8e4c7d2a1f0` 增加，现有单租户数据回填 `0`；所有新写入和 owner 查询均从可信上下文显式传入 tenant。
- ✅ 已增加 `(tenant_id, trace_id)` 和 `(tenant_id, queued_at, log_id)` 索引；后续列表按 `queued_at DESC, log_id DESC` 稳定排序。
- Phase 4 新增 `agent_code VARCHAR(64) NULL`、`target_summary TEXT NULL`；能按 `tool_call_id` 可靠关联 PreparedAction 的历史日志可回填 `agent_code`，其他 legacy 行保持 `NULL` 并在 DTO 显示为 unknown，禁止从当前 Conversation 猜测；`target_summary` 只存 allowlist 后的冻结目标摘要。

后端契约：

| Endpoint | Query/响应 | 拒绝语义 |
|---|---|---|
| `GET /ai/operation-log/traces` | `current/size`，可按 `traceId/actorId/agentCode/toolName/status/queuedFrom/queuedTo` 过滤；返回按 trace 分组的 `PageResult` 摘要 | 无 `ai:trace:view` 返回 403 + `AI_TRACE_FORBIDDEN` |
| `GET /ai/operation-log/traces/{trace_id}` | 只返回 `conversationId/sourceMessageId`、消息角色/时间等元数据、actor、Agent/Tool、confirmation 生命周期、脱敏 target summary、终态/错误/耗时；后端 DTO 禁止 message content、raw prompt、raw args/frozen args，确需预览时只能使用独立 allowlist + 脱敏后的 `safeMessageSummary` | 不存在或跨 tenant 统一 404 + `AI_TRACE_NOT_FOUND` |

Web 新增独立 `/ai/trace` 页面和 C 类型菜单，权限固定为 `ai:trace:view`；不复用需要 `monitor:operation-log:list` 的传统 `/system/operation-log` 请求链。页面提供列表、筛选和详情抽屉，任何字段都不得显示密码、API Key、raw prompt、raw args 或未脱敏 PII。

---

## 4. 三个 Agent 的最小闭环

### 4.1 `user_mgmt`

已有查询、统计、创建、资料更新、密码重置、删除、导入和导出继续保留。本期新增：

| Tool | 权限 | 风险 | 核心约束 |
|---|---|---|---|
| `user.dept_lookup` | `system:dept:list` | low / readonly | 只在可见部门内按名称/路径解析，同名不得猜测 |
| `user.role_lookup` | `system:user:role-auth` | low / readonly | 只返回可委派角色的最小摘要和稳定 ID，零/多命中必须澄清 |
| `user.update_dept` | `system:user:edit` + `system:dept:list` | high + HITL | 完整部门集合、主部门规则、旧/新对象 scope 与目标用户变更前后有效 data scope、批准时复验 |
| `user.update_roles` | `system:user:edit` + `system:user:role-auth` | high + HITL | 完整角色集合、禁自改、R_SUPER 保护、最终有效授权 dominance、批准时复验 |

新增 `system:user:role-auth` 用于把“可以写用户”与“写入中可以变更授权”组成双权限边界；它允许通过 Delegation Policy 查询“可委派角色”，不自动授予 `system:role:list` 或 Role 管理页面访问权。`user.update_roles`/页面角色替换同时要求 `system:user:edit + system:user:role-auth`，create/import 则分别要求自身 `add/import + role-auth`。upgrade 对拥有历史任一角色写入口权限 `system:user:add`、`system:user:edit` 或 `system:user:import` 的存量角色幂等补 `system:user:role-auth`，从而保留各自原入口能力，又不会让 add-only/import-only 角色获得现有用户编辑权；fresh install 按角色职责显式授予。

传统页面 API 必须同步拆分，不能让新 Tool 安全而旧资料接口继续越权：

- 基础 `PUT /system/user/{user_id}` 的 schema 移除 `roles/dept_ids`；提交这些字段直接拒绝，不能静默忽略。
- 新增 `PUT /system/user/{user_id}/roles`，body 固定为 `{roleIds: string[]}`，同时要求 `system:user:edit + system:user:role-auth` 并调用与 `user.update_roles` 相同的完整替换 Policy。
- 新增 `PUT /system/user/{user_id}/departments`，body 固定为 `{deptAssignments: [{deptId: string, isPrimary: boolean}]}`，要求 `system:user:edit + system:dept:list` 并调用与 `user.update_dept` 相同的完整替换 Policy。
- 页面角色/部门控件分别调用新 API；`GET /system/user/assignable-roles?query=&limit=` 只要求 `system:user:role-auth`，只返回 `{roleId, roleCode, roleName, dataScope}` 最小候选，`limit <= 20`。
- 页面 `POST /system/user` 只要 payload 出现 `roleIds`（包括空数组），除 `system:user:add` 外就必须要求 `system:user:role-auth`，并对完整目标角色集合执行与 `user.update_roles` 相同的 dominance Policy；无该权限时不能因同时拥有新增用户权限而写入角色。
- 用户导入只要模板出现角色列（包括整列空值），除 `system:user:import` 外就必须要求 `system:user:role-auth`，预检和执行都逐行按完整目标角色集合执行相同 Policy；任一行越权时整批不产生业务写入。没有角色列时不得从其他可编辑列或模板默认值解析出任意角色。
- AI `user.create` 和未显式提供角色的页面/导入固定由后端分配唯一 `R_USER`，这是不要求 `system:user:role-auth` 的窄例外：请求 schema 不接受角色参数，且 `R_USER` 必须存在、启用，其 permission/menu/Agent 和物化 data scope 均不超过操作者 `GrantAuthority`；否则整个创建失败。部门参数仍要求 `system:dept:list`、目标部门在 scope 内并调用同一部门 Policy。
- 其他任何会写入角色/部门的入口都必须显式要求对应授权并调用同一 Policy；不能通过创建、导入或后台任务绕过角色/部门 dominance。

完整集合替换规则：

- `user.update_dept` 的 dry-run/execute 先加载当前完整部门关联，一次性校验目标用户以及 `旧部门集合 ∪ 新部门集合` 全部位于操作者可写 scope；任一越界即整体返回 `AI_DATA_SCOPE_VIOLATION`，不得借替换删除越界关联、静默保留或部分更新。
- 部门关联会改变目标用户通过 `DEPT/DEPT_AND_SUB` 等角色物化出的数据权限。服务端必须以目标用户完整启用角色集合分别物化变更前和假设变更后的可访问部门/用户集合，两者都必须是操作者 `GrantAuthority` 的子集；即使新部门 ID 本身可见，只要其后代或组合后的 user scope 越界，也返回 `AI_USER_DEPT_AUTHZ_IMPACT_OUT_OF_SCOPE`。页面 API 与 AI Tool 共用该检查。
- `user_require_primary_dept=true` 时非空集合必须恰好一个主部门且不得提交空集合；关闭时最多一个主部门。部门 ID 去重，部门必须存在、启用且可分配。
- `user.update_roles` 的新角色集合必须非空、去重，并同时校验旧/新集合；非超管不得移除自己无权管理的旧角色。对目标用户应用完整新角色集合后，重新计算其有效 permission/menu、Agent 和实际数据集合，均不得超过 §4.3 的操作者授权上界。
- 禁止修改自己的角色；用户名 `admin`、当前或请求后拥有启用 `R_SUPER` 的用户只允许超级管理员操作。新角色必须存在且启用。
- PreparedAction 冻结用户 ID、状态、当前部门及主部门、排序后的旧/新部门或角色 ID 集合、目标用户完整启用角色定义、变更前/后物化 scope hash、主部门配置和操作者授权摘要；批准时任一事实变化均返回 `AI_PREPARED_ACTION_SNAPSHOT_STALE`。
- 名称只用于 lookup 和确认展示，写工具只接收冻结 ID。`user.role_lookup` 必须在 Phase 2 与 `user.update_roles` 同时交付，不能依赖 Phase 3 中仅属于 `role_mgmt` 的 `role.lookup`。

具体 schema、确认展示和完整错误码仍记录在 [`2026-08-11-ai-user-management-tools.md`](./2026-08-11-ai-user-management-tools.md) Plan 2；若其历史内容与上述跨模块 Policy 冲突，以本文为准，并在 Phase 2 编码前同步回写。

### 4.2 `dept_mgmt`

| Tool | 权限 | 风险 | 核心约束 |
|---|---|---|---|
| `dept.count/list/lookup` | `system:dept:list` | low / readonly | 只查询当前 data scope 内的部门；lookup 同名不得猜测 |
| `dept.create` | `system:dept:add` + `system:dept:list` | high + HITL | 父部门可写；创建顶级部门仅超级管理员 |
| `dept.update` | `system:dept:edit` + `system:dept:list` | high + HITL | 只改非结构字段，Tool schema 禁止 `parent_id/ancestors`；status 变更执行授权影响分析 |
| `dept.move` | `system:dept:move` + `system:dept:list` | high + HITL | 受限委派；源、旧父、新父和完整子树均在可写 scope |

新增 `system:dept:move`，使普通资料编辑权限不能隐式获得树结构移动能力。本期不新增 AI `dept.delete` Tool，但现有页面/API 删除入口必须按下述超级管理员 destructive Policy 收口，不能以 Tool 延期为由保留旁路。

约束：

- 所有部门读取入口统一要求认证和 `system:dept:list`，包括传统 `/tree`、`/tree-option`、`/tree-list`、列表/详情 API 以及 `dept.count/list/lookup`。页面 API 和 AI Tool 调用同一个 scoped selector。
- 超级管理员或 `DATA_SCOPE_ALL` 返回租户内全部部门；其他用户只返回 `accessible_dept_ids`。局部树不得补出越界祖先名称，可见节点父级不可见时按局部根节点投影；直接请求越界 ID 返回 404，避免枚举组织结构。
- 本文显式覆盖 Gateway SR-22 中“dept 是全局元数据、不应用 data scope”的历史决定；Role 元数据是否可见仍由 Role Policy 独立决定，不能据此类推。
- lookup 先在可见集合内按自然语言名称/路径解析，再把稳定部门 ID 交给写工具；同名只按可见候选判断，不泄露越界同名部门。
- `dept.create(parent_id=null)` 表示创建顶级部门，仅超级管理员可执行；普通委派管理员只能在可写父部门下创建。负责人等引用用户存在时也必须位于操作者 user scope。
- `dept.update` 只允许名称、排序、负责人、联系方式和状态等非结构字段，并校验所有引用用户。AI Tool 与基础页面 `PUT /system/dept/{dept_id}` 的 schema 都必须移除 `parent_id`、`ancestors`；传入结构字段直接拒绝，不能复用宽泛 `DeptUpdate` 绕过移动流程。
- `status` 虽非树结构字段，但启用/禁用会改变引用该部门的 `CUSTOM` 及其他 resolver 贡献。状态变更必须批量物化全部受影响角色/principal 的变更前后 scope，并应用与 move 相同的成员范围和集合子集规则；任一越界返回 `AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE`。PreparedAction 冻结部门状态、受影响 role/principal ID 和前后 scope hash；普通名称/排序更新不得复用该旁路省略 status 分析。
- 页面新增独立 `PUT /system/dept/{dept_id}/move`，body 固定为 `{newParentId: string | null}`，要求 `system:dept:move` 并调用与 `dept.move` Tool 相同的 Service/Policy；前端树移动不得继续提交基础更新 API。
- 父级变化只能走 `dept.move`。普通委派管理员只有在源部门、旧父部门、新父部门及完整受影响子树都位于可写 scope 时才能移动；移动到根节点、移动 scope 根节点或影响任一越界节点时仅超级管理员可执行。
- 树移动还会间接改变 `DEPT_AND_SUB` 用户的实际数据权限。dry-run 必须批量物化移动前后所有权限集合发生变化的 principal；普通操作者只有在这些用户全部位于其 user scope，且每个 principal 变更前后的可访问用户/部门集合都属于操作者集合子集时才能继续，否则返回 `AI_DEPT_MOVE_AUTHZ_IMPACT_OUT_OF_SCOPE`。不得因为角色定义本身未变化而跳过影响分析。
- move 拒绝自己作为父级、移动到后代、层级超限、源/目标相同、目标不存在及跨 tenant；冻结源/旧父/新父 ID、父链、排序后的完整子树 ID/路径，以及受影响 principal ID 与变更前后 scope hash。确认界面显示受影响账号数量和范围变化安全摘要。
- 批准执行不得再次按名称解析；父链、子树、权限或 scope 任一变化均返回 `AI_PREPARED_ACTION_SNAPSHOT_STALE`。

#### 4.2.1 部门成员页面接口不得旁路用户部门 Policy

现有 `/system/dept/{dept_id}/users` 是部门中心的 `user_depts` writer，必须和 `user.update_dept` 使用同一授权与事务规则，不能只凭 `system:dept:edit` 查询全租户用户或做局部 diff：

- `GET /system/dept/{dept_id}/users` 同时要求 `system:dept:list + system:dept:edit + system:user:list`；目标部门必须在可写 dept scope。候选集合为“user scope 内全部现有成员（含 disabled）∪ user scope 内启用的可分配用户”，支持 `query/current/size` 且 `size <= 100`，按 `user_id` 稳定分页，响应为 `{current,size,total,records:[{userId,userName,nickname,status,isMember,isPrimary}]}`；disabled 旧成员可保留/移除但不可新增，不返回邮箱、电话或 scope 外用户。
- 服务端先加载该部门全部现有成员；只要存在 scope 外成员，普通操作者整体返回 403 + `DEPT_MEMBERSHIP_GLOBAL_IMPACT_OUT_OF_SCOPE`，不得过滤后伪装成完整集合，也不得泄露被阻断成员身份。超级管理员可获得租户内完整候选。
- `PUT /system/dept/{dept_id}/users` 同时要求 `system:dept:list + system:dept:edit + system:user:edit`，body 的 `userIds` 是该部门完整最终成员集合。旧/新用户 ID 均必须存在、去重并处于 user scope；任一非法对象使整批零写入失败。
- 对每个新增/移除用户加载其完整旧部门关联和启用角色，构造加入/移除该部门后的完整新关联，再执行 §4.1 的主部门、目标用户前后物化授权与 dominance Policy。部门中心接口不得移除主部门成员，返回 `USER_PRIMARY_DEPT_REASSIGN_REQUIRED`，该用户必须改走显式提交新主部门的 `PUT /system/user/{id}/departments`；新增关联默认非主部门。
- 预读和执行都按 §5 role → dept → user 顺序锁定全部受影响对象，锁后重算完整成员集合、每个用户部门集合和前后 scope hash；任一漂移整体 stale/rollback，不允许只更新可见成员。

#### 4.2.2 传统部门删除入口的 MVP 边界

`DELETE /system/dept/{dept_id}` 与 `POST /system/dept/batch-delete` 在 MVP 期间分别保留既有 `system:dept:delete/batch-delete`，并额外强制当前用户拥有启用 `R_SUPER`；普通委派管理员的按钮和 API 都不可用。删除前按 §5 锁定全部目标、子部门、成员用户、引用该部门的 `role_depts` 角色及其成员，并要求目标集合均无子部门、无用户关联、无 Role custom-scope 引用；任一引用存在则整体返回稳定 `DEPT_DELETE_REFERENCED`，不得依赖 FK cascade 改写任何 principal 的授权。batch 要求非空、先去重并校验全部 ID，任一保护/引用/不存在对象使整批零删除；Service 不 commit。AI delete Tool 只有在未来定义 destructive HITL、相同引用保护和审计后才能开放。

### 4.3 `role_mgmt`

Role 写操作采用受限委派，不使用“所有写工具仅超级管理员”的退化方案。MVP 工具矩阵：

| Tool | 权限 | 风险 | 委派边界 |
|---|---|---|---|
| `role.count/list/lookup` | `system:role:list` | low / readonly | 返回租户内最小摘要及 `delegable/blockedReasonCode`，写入不得按名称重查 |
| `role.create` | `system:role:add` | high + HITL | 只能创建不超过操作者授权上界的非保护角色 |
| `role.update` | `system:role:edit` | high + HITL | `role_code` 不可变；状态/data scope 走全局影响检查 |
| `role.update_menus` | `system:role:menu-auth` | high + HITL | 完整集合替换，旧/新菜单及权限码均不得超出操作者 |
| `role.update_agents` | `system:role:ai-agent-auth` | high + HITL | 完整集合替换，旧/新 Agent 均须属于操作者潜在可委派集合 |

本期不新增 AI `role.delete` Tool；现有 `DELETE /system/role/{role_id}` 与 `POST /system/role/batch-delete` 分别保留既有 `system:role:delete/batch-delete`，并额外强制当前用户拥有启用 `R_SUPER`。`R_SUPER` 角色永不允许删除；任何仍有成员的角色返回 `ROLE_DELETE_REFERENCED`，不能通过 cascade 清除 `user_roles` 间接改写用户授权。单个/批量删除都按 §5 锁定目标角色、成员和关联部门；batch 要求非空，先去重并验证全部目标后原子删除无成员的 menu/dept/Agent 关联；任一目标不存在、受保护或被引用时整批零删除。AI delete Tool 后续另行定义 destructive HITL 与审计。

Role 元数据不应用 Department data scope：拥有 `system:role:list` 的用户可以查询 tenant 内全部角色的 ID/code/name/status/data-scope 最小摘要，但响应必须附 `delegable` 和稳定 `blockedReasonCode`，不得返回完整菜单/Agent/成员集合。`user.role_lookup` 是更窄的分配候选接口，只返回当前操作者可委派角色。只读结果不构成写入授权，所有写 Tool 仍重新执行下述 Policy。

#### 4.3.1 授权上界

普通操作者每次发起和批准执行时都构建同一个 `GrantAuthority`：

- `permission_codes/menu_ids`：操作者全部启用角色贡献的有效功能权限和菜单 ID；
- `visible_agent_ids`：仅用于聊天列表和路由；操作者启用角色已绑定、Agent 自身已启用且至少有一个可见 Tool 的 Agent；
- `grantable_agent_ids`：用于 Role dominance；操作者全部启用角色中 `RoleAiAgent.enabled=true` 的显式绑定贡献所有同租户 Agent，不受 `AiAgent.enabled` 影响。全局禁用 Agent 仍是未来可恢复的潜在能力，但 soft-disabled Role-Agent 行不授予当前委派能力；
- `scope_kinds`：操作者全部启用角色贡献的 data-scope 类型集合；不压缩成单个整数优先级；
- `accessible_dept_ids/accessible_user_scope`：与运行时查询使用同一个 `DataScopeContext` resolver，将全部启用角色物化结果取并集；全量用明确的 unbounded 标记表示，不能把 `None` 当空集合；
- `tenant_id`、操作者状态、启用角色集合及对应版本摘要。

功能权限和 Agent dominance 使用集合包含关系：候选 permission/menu 集合必须是操作者集合的子集，候选 Agent 集合必须是操作者 `grantable_agent_ids` 的子集。修改完整集合时比较 `旧集合 ∪ 新集合`；因此操作者可以移除自己已绑定但当前全局禁用的 Agent，却不能接管或移除自己从未绑定的潜在能力。

共享 DataScope resolver 必须替换旧的“选择优先级最高枚举”语义：`ALL` 产生 unbounded；否则分别物化每个启用角色的 SELF/DEPT/DEPT_AND_SUB/CUSTOM 集合后取并集。Role Policy 不得自行调用旧 `get_best_scope()` 猜测模板上界。

角色模板禁止使用 `ALL > DEPT_AND_SUB > DEPT > CUSTOM > SELF` 的整数优先级直接判断。`scope_kinds` 中每个类型按下表贡献可委派模板，多个类型的结果取并集；`CUSTOM(S)` 仍必须满足 `S ⊆ accessible_dept_ids`：

| `scope_kinds` 中存在 | 贡献的可委派角色 scope |
|---|---|
| `ALL` | `ALL`、`DEPT_AND_SUB`、`DEPT`、任意租户内 `CUSTOM`、`SELF` |
| `DEPT_AND_SUB` | `DEPT_AND_SUB`、`DEPT`、`SELF`，以及 `CUSTOM(S)` 且 `S ⊆ accessible_dept_ids` |
| `DEPT` | `DEPT`、`SELF`，以及 `CUSTOM(S)` 且 `S ⊆ accessible_dept_ids` |
| `CUSTOM` | `SELF`，以及 `CUSTOM(S)` 且 `S ⊆ accessible_dept_ids` |
| `SELF` | `SELF` |

模板偏序不是最终授权。角色分配给具体用户，或修改已有成员的角色定义时，必须基于该用户当前部门和完整角色集合物化变更前、变更后的实际可访问用户/部门集合；两者都必须是操作者可管理集合的子集。

从历史“只取最高优先级 scope”切换到并集会扩大一部分存量多角色账号的实际可见集合，因此不能作为无感 helper 替换发布。Phase 2 上线前必须提供并执行 `python scripts/audit_data_scope_union.py --output <protected-path>` 只读预检：在一致性快照中对每个启用的多角色 principal 分别按旧传统 API、旧 AI `DataScopeContext` 和新 resolver 物化范围，报告头使用服务端可信 tenant（当前固定为 `0`）并冻结 resolver/build SHA、角色定义/成员关系/部门父链与状态版本摘要，正文输出 principal、角色 code、三组部门/用户计数、稳定摘要及相对两个旧读取面的新增 ID 集合；报告仅向授权管理员开放。命令不得接受客户端 tenant 重标记；对任一读取面的非空扩大项返回非零并输出 canonical report SHA-256。

release job 只有在受控变量 `DATA_SCOPE_UNION_ACK_SHA256` 与当前报告 hash 精确一致时才可继续。实际切换使用 `--verify-ack`，并必须同时提供受控的 `--maintenance-command` 与 `--switch-command` JSON argv；脚本先获取 session 级 authorization-migration advisory lock，再由 maintenance command 停止 §5 所有在线 writer，在新的 repeatable-read 一致快照中重跑完整审计，仅在精确 ACK 通过后调用负责激活同一 build 并完成启动验证的 switch command。resolver/build、角色/成员/部门版本或任一旧/新 set digest 变化都会生成新 hash，使旧确认失效并阻断发布；maintenance、审计、校验、代码切换和验证完成前不得释放同一数据库 session lock。migration 不得读取确认变量修改角色或裁剪集合。ACK 或 switch 失败时保持维护态，由发布方按回滚步骤恢复旧构建。

#### 4.3.2 全局影响与保护对象

- MVP 的可执行保护集合固定为 `PROTECTED_ROLE_CODES={"R_SUPER"}`；用户名 `admin` 是保护用户而不是额外角色 code。保护角色及 `admin` 的授权链只能由当前超级管理员操作；`role_code` 创建后不可修改。后续若增加其他保护角色，必须先增加持久化 `is_protected` 或更新该显式集合及迁移，不能靠名称约定猜测。
- 普通管理员对任一角色写操作都要求目标角色现有定义与请求后定义同时处于其 `GrantAuthority` 内；不能接管、重命名、禁用或缩窄一个自己原本无权管理的高权限角色。
- 普通角色管理员可以创建符合上界的角色，也可以修改未分配角色。修改已分配角色的 status、data scope、custom departments、menus 或 Agents 前，必须一次性加载全部成员。
- 普通管理员只有在全部成员均处于其 user data scope、本人不是成员、成员中没有 `admin/R_SUPER`，且每个成员变更前后的最终有效权限、Agent 和数据集合都不超过操作者时，才能修改。任一成员越界时整体返回 `AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE`，不能过滤越界成员后继续。
- 普通管理员不得修改自己当前所属角色的授权字段，返回 `AI_ROLE_SELF_MUTATION_FORBIDDEN`；避免通过角色聚合根间接自提权或在 pending 期间撤销自己的授权依据。
- `role.update_menus` 规范化叶子菜单并由服务端派生必要祖先，同时比较 menu ID 与最终 permission code；`role.update_agents` 的旧/新集合都与 `grantable_agent_ids` 比较，不能因 Agent 当前禁用就跳过 dominance，也不能错误使用只含 enabled Agent 的 `visible_agent_ids`。
- 管理页面 API 和 AI Tool 必须调用同一个 Role Delegation Policy；页面拥有按钮权限不代表可以突破上述 grant authority。

#### 4.3.3 快照与执行

Role PreparedAction 至少冻结：操作者启用角色、permission/menu/Agent/data/user scope 摘要；目标角色 code/status/data scope/custom departments/menu/Agent/member 集合摘要；全部受影响成员变更前后的有效授权摘要；规范化后的完整请求集合。所有集合排序去重后使用 canonical JSON hash。

批准执行时按 §5 全局锁协议锁定目标角色、相关部门、操作者和受影响用户，重算快照与 `GrantAuthority`；任一版本、成员、权限、Agent 绑定或 data scope 变化均返回 `AI_PREPARED_ACTION_SNAPSHOT_STALE`。快照未漂移仍必须重新执行 dominance 和全局影响 Policy，业务写入、PreparedAction 终态与 operation log 在现有 Gateway 事务边界中收口。

---

## 5. 共享 Service 与事务边界

AI 工具不能简单调用当前 HTTP endpoint，也不能复制一套只对 AI 生效的安全规则。

- 用户部门/角色授权、权限 dominance、R_SUPER 保护和 data scope 校验下沉到可复用 Service/Policy。
- 管理页面 API 与 AI 工具调用同一领域规则。
- Service 不 `commit()`；Gateway 或 API 层拥有事务边界。
- dry-run 和 execute 共用解析/校验 helper，但 execute 必须重新加载当前事实。
- 所有准备后执行的写操作冻结稳定 ID；名称只用于展示。
- pending 批准时如果操作者权限、目标 owner/tenant、数据范围、目标版本或关联集合变化，操作 fail closed，在同一事务写 `failed/expired + error_code + finished_at` 后返回稳定错误；不能只返回 4xx/业务错误而保留 pending。
- 完整集合替换必须在同一事务内完成；任何校验或写入失败都不得留下部分删除、部分新增或只对 scope 内对象生效的局部结果。
- 页面 API 与 AI Tool 对同一聚合根采用相同锁顺序和 canonical snapshot 规则，避免一侧并发修改绕过另一侧的漂移检测。

删除会话必须先锁 conversation 及其全部非终态 PreparedAction；confirm/worker 的状态转换也必须锁同一 action row。`prepared/pending_confirmation/approved` action 在同一事务标记为 `expired`，写 `AI_CONVERSATION_DELETED`、`finished_at` 并清理 execution lease 后再软删除会话；只要存在 `running` action 就返回 409 + `AI_ACTION_RUNNING` 且会话不删除，过期 lease 先由带 fencing token 的既有恢复流程终态化，delete 不猜测 worker 是否存活。删除提交后 confirm 不再执行任何业务副作用，resume 只可读取终态安全投影；终态转换和会话删除任一失败时整体回滚。

所有会写 `user_roles`、`user_depts`、`role_menus`、`role_depts`、`role_ai_agent`、`sys_dept.parent_id/ancestors`、会影响授权的 `sys_dept.status`，或删除 `sys_role/sys_dept` 聚合根的页面、AI、导入和后台任务统一遵守授权聚合锁协议；部门结构/status/delete writer 不得先锁 dept 后再补锁受影响角色或用户：

1. 预读涉及的 role/dept/user ID；`dept.move` 要基于旧/新祖先链预读完整子树，`dept.update(status)` 要按启用/禁用两种状态预读引用关系，二者都要找出所有可能因 `DEPT`、`DEPT_AND_SUB`、`CUSTOM` 贡献而改变有效范围的角色和 principal；
2. 先按 role ID 升序锁 role rows，再按 dept ID 升序锁 dept rows，最后按 user ID 升序锁操作者、目标和受影响 user rows；
3. 获取锁后重新查询操作者角色、目标成员、部门祖先/子树/状态和全部关联集合；`dept.move` 与 `dept.update(status)` 必须重新物化受影响角色/principal 集合与变更前后 scope，发现新增待锁对象、集合扩大/缩小或版本变化时返回 stale，或释放全部锁后从预读阶段重试，不能边持锁边改变锁顺序；
4. 只有完成二次 Policy 后才能写关联表；所有运行时 writer 都必须先锁对应父 row，防止成员 phantom 刚通过 hash 复验就被另一入口插入；
5. seed/migration 只能在排他维护阶段写这些关联；用户导入等在线 writer 不能豁免该协议。

---

## 6. 验收标准

### 6.1 正向

**Scenario: 超级管理员使用三个 Agent**

**Given** AI 模块默认开启，超级管理员已登录且至少配置一个可用模型
**When** 管理员通过页面分别完成用户部门调整、部门创建/移动和角色更新
**Then** 三个 Agent 均能被 Supervisor 正确路由，HITL 显示可读且冻结的目标，批准后页面数据与审计日志一致

**Scenario: 委派管理员完成 Role 授权闭环**

**Given** 华东委派管理员拥有 `user_mgmt/role_mgmt` 绑定、Role 写权限和本部门及子部门数据范围，但不是超级管理员
**When** 通过 AI 创建较窄角色、授予自己拥有的菜单和 Agent，并把该角色分配给范围内用户
**Then** 所有操作均可进入 HITL 并成功；目标用户重新登录后只看到被绑定 Agent，Supervisor 能正确路由，页面与 AI Trace 显示一致结果

### 6.2 功能权限

**Scenario: 没有 AI 入口权限**

**Given** 普通用户已登录但没有 `ai:chat:use`
**When** 直接调用 §3.4 中要求入口权限的用户侧 AI API
**Then** 后端返回 403 + `AI_CHAT_PERMISSION_DENIED`，不能依赖前端隐藏菜单；独立拥有 `ai:trace:view` 的审计查询和后台管理 API 不被误伤

**Scenario: 模块紧急熔断**

**Given** `AI_MODULE_ENABLED=false`
**When** 任意用户访问 `/ai/**`
**Then** disabled guard 统一返回 503 + `AI_MODULE_DISABLED`，业务 router/service 未初始化且传统管理页面继续可用

**Scenario: 有聊天权限但没有 Agent 或 Tool 权限**

**Given** 用户拥有 `ai:chat:use`
**When** 用户没有目标 Agent 绑定，或缺少具体 tool permission
**Then** Agent 不可见，或工具不提供给 LLM；伪造 tool 调用仍在 Gateway 被拒绝

**Scenario: Agent 选择旁路全部 fail closed**

**Given** 用户只绑定 `user_mgmt`
**When** 通过手工 `agentCode`、旧会话粘滞值、Supervisor 关闭后的默认回退或伪造 Tool-Agent 调用选择 `role_mgmt`
**Then** 所有执行路径调用同一 Agent Policy 并拒绝，不能静默回退；若 pending 后解绑/禁用原 Agent，approve/执行型 resume 以稳定错误进入终态，reject/最小状态回放仍可收口且不执行 Tool/LLM

**Scenario: 伪造聊天模型**

**Given** 用户拥有聊天权限，但目标模型已禁用、不含 `text` capability 或不符合当前 Agent 约束
**When** 绕过模型下拉框直接在 chat/conversation 请求提交该 `modelId`
**Then** 统一 selector 返回 `AI_MODEL_NOT_AVAILABLE`，不得保存到会话、创建模型调用或 fallback

**Scenario: 模型失效不阻断拒绝或终态回放**

**Given** action 已经 pending，随后冻结的模型或 Provider 被禁用
**When** owner 拒绝 action，或 reload 后 `resume` 仅回放已持久化状态
**Then** reject 必须进入 rejected 终态，回放必须成功且都不调用模型；当前执行权限仍有效时可返回完整安全投影，否则只返回最小状态 DTO；只有新的 LLM continuation 被 selector 拒绝，任何分支都不能留下无法收口的 pending action

**Scenario: pending 后撤权仍可主动拒绝**

**Given** action 已经 pending，随后 owner 的 `ai:chat:use`、Role-Agent 绑定或 Tool 权限被撤销
**When** owner 分别尝试 approve、reject 与 resume
**Then** approve 不执行业务写入并以稳定 forbidden/stale 错误进入终态；reject 仍仅按认证 owner + tenant 写 rejected 终态；resume 只返回 confirmation/status/error/time 最小 DTO，不泄露 `result_data/result_ui`，三者都不调用未授权 Agent/Tool/model

**Scenario: 撤权后的历史结果在所有读取面一致脱敏**

**Given** owner 保留 `ai:chat:use`，但历史结果对应的 Agent、Tool 或目标 data scope 已被撤销
**When** 分别调用 resume、conversation detail/history、query-cache 和 owner operation-log，或前端从旧 store/cache 恢复会话
**Then** 所有后端入口复用 `authorize_result_projection()`；受影响 assistant/tool message 变为 tombstone，pending/operation-log 只返最小状态，query-cache 与不存在同为 404，任何入口都不返回 content/result/UI/args/presentation

**Scenario: Provider test 不能充当内网代理**

**Given** 用户拥有 Provider test 权限
**When** 提交临时 URL、loopback/private/link-local 地址、DNS rebinding、重定向到受限地址或超大/超时响应
**Then** 后端共享 egress Policy 在发起受限网络请求前拒绝或中止，只返回脱敏稳定错误；测试只能引用当前 tenant 已保存且允许的 Provider

**When** 存量 Provider 或 Model override 指向未授权内网地址，并通过 chat、Supervisor、Agent run 或 continuation 选中
**Then** runtime quarantine/统一 hardened transport 在连接前拒绝，模型不出现在 chat-safe options，不得因配置曾经保存成功而放行

**Scenario: 新增和导入不能旁路角色委派权限**

**Given** 操作者拥有 `system:user:add` 或 `system:user:import`，但没有 `system:user:role-auth`
**When** 页面创建请求提交显式 `roleIds`，或导入包含角色列
**Then** 请求在写入前返回 403，不能创建用户或产生部分导入；未显式提供角色时只能走服务端固定 `R_USER` 窄例外，且该角色超出操作者 `GrantAuthority` 时同样拒绝

### 6.3 数据权限

**Scenario: 部门管理员修改范围内与范围外用户**

**Given** 管理员只有本部门及子部门数据范围
**When** 对范围内用户执行 `user.update_dept`
**Then** 可以进入 HITL 并成功执行
**When** 目标用户、新部门或准备被移除的旧部门任一超出范围
**Then** dry-run/execute 均返回数据权限错误且不产生业务写入
**When** 目标用户和旧/新部门 ID 都可见，但新部门使目标用户的 `DEPT/DEPT_AND_SUB` 最终范围超出操作者
**Then** 返回 `AI_USER_DEPT_AUTHZ_IMPACT_OUT_OF_SCOPE`，证明校验的是目标用户变更前后的物化授权而不只是部门 ID

**Scenario: 部门读写和树移动使用同一 scope**

**Given** 华东部门管理员拥有部门查询、创建、编辑和移动权限
**When** 查询部门、在华东子树内创建或移动部门
**Then** count/list/tree/lookup 只包含可见节点，合法写入可以确认并成功
**When** 读取越界部门、跨 scope 移动、移动到根节点、移动包含越界节点的子树，或移动会扩大任一 scope 外 principal 的实际范围
**Then** 读取不泄露对象存在性，写操作在 pending 前拒绝且树结构不变

**Scenario: 部门状态变更分析间接授权影响**

**Given** 一个禁用部门仍被 scope 内外角色的 `CUSTOM` 或其他 DataScope 规则引用
**When** 普通部门管理员尝试重新启用该部门
**Then** 服务端先物化所有受影响 principal 的前后范围；存在范围外成员或越界扩大时整体返回 `AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE`，状态保持不变

**Scenario: 部门成员弹窗不泄露或局部改写 scope 外用户**

**Given** 普通部门管理员打开范围内部门的成员弹窗，且租户内存在 scope 外用户
**When** 查询或提交 `/system/dept/{dept_id}/users`
**Then** 候选仅包含 user scope 内最小字段；现有成员任一越界时整体拒绝而不返回身份，PUT 对旧/新全部用户逐人执行完整部门及前后授权 Policy，移除主部门必须改走用户部门接口，任一失败整批零写入

**Scenario: 传统部门删除不通过 cascade 改写授权**

**Given** 普通管理员拥有部门删除按钮权限，或超级管理员选择含子部门、用户或 Role custom-scope 引用的部门
**When** 调用单个或批量删除 API
**Then** 普通管理员 403；超级管理员收到 `DEPT_DELETE_REFERENCED` 且整批零删除，`role_depts/user_depts` 不被 FK cascade 静默清理

### 6.4 提权与漂移

**Scenario: 普通管理员合法委派与越权提权**

**Given** 管理员拥有 Role 委派权限但不是超级管理员
**When** 创建/修改的角色及目标用户最终权限、Agent 和实际数据集合均为自身授权子集
**Then** 操作可以进入 HITL 并成功
**When** 尝试分配 `R_SUPER`、更宽 data scope、自己不拥有的菜单/Agent，或移除自己无权管理的旧角色
**Then** 操作在创建 pending action 前被拒绝

**When** 目标用户仍在管理员 data scope 内，但已有角色包含管理员不拥有的权限或 Agent
**Then** 因 dominance 而不是 user data scope 被拒绝，证明完整旧角色集合也参与校验

**Scenario: 角色修改存在越界全局影响**

**Given** 同一角色同时分配给 scope 内和 scope 外用户
**When** 普通管理员修改该角色的状态、data scope、菜单或 Agent
**Then** 整体返回 `AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE`，不能只对 scope 内成员生效；修改自己所属角色同样被拒绝

**Scenario: 传统角色删除不绕过保护与成员影响**

**Given** 普通管理员拥有角色删除按钮权限，或超级管理员选择 `R_SUPER`/仍有成员的角色
**When** 调用单个或批量删除 API
**Then** 普通管理员 403；保护或被引用目标使整批零删除，不能依赖 cascade 移除 `user_roles`、菜单、部门或 Agent 授权

**Scenario: 审批后权限或目标发生变化**

**Given** 写操作已经 pending
**When** 操作者入口/Agent/Tool 权限、目标用户部门、角色定义/成员集合或目标父部门/子树在批准前变化
**Then** 执行端重新校验并拒绝漂移 action，不把批准应用到新目标

### 6.5 安装升级与审计

**Scenario: fresh install 与 upgrade 不扩大普通角色权限**

**Given** 分别初始化全新数据库和包含手工 Agent 配置/Role-Agent 绑定的存量数据库
**When** 执行 Phase 1 seed/migration
**Then** fresh 仅 `R_SUPER` 可用；upgrade 保留显式模块开关、Agent enabled 和已有业务绑定，为旧安装的 `R_SUPER` 幂等补全部已发布 Agent 绑定，只为已有业务绑定普通角色补入口权限，不恢复 shared 直通、不绑定新普通角色

**Scenario: AI Trace 闭环核对**

**Given** 三个 Agent 的写操作分别成功、拒绝和失败
**When** 拥有 `ai:trace:view` 的审计员通过管理页面按 trace 查看详情
**Then** 可以核对 actor、Agent/Tool、消息 ID/角色/时间元数据、冻结目标安全摘要、确认生命周期和终态；普通用户不能列出租户审计，页面及 API 不包含消息正文、raw prompt/args、密码、API Key 或未脱敏 PII

**Scenario: 多角色 data scope 升级先审计后切换**

**Given** 存量账号分别同时拥有 `DEPT + CUSTOM`、`DEPT_AND_SUB + CUSTOM` 角色，且新并集 resolver 会增加可见部门或用户
**When** 执行 Phase 2 升级预检
**Then** scope-diff 报告列出每个 principal 的变更前后范围和新增集合并绑定 resolver/build、角色/成员/部门版本；非空扩大项在管理员显式确认前阻断发布，维护锁下重跑 hash 漂移会使确认失效；确认上线后传统 API 与 AI 查询必须得到同一物化并集

**Scenario: 删除会话不遗留 pending action**

**Given** owner 的会话包含 pending action
**When** owner 删除会话
**Then** 会话和非终态 action 在同一事务锁定，action 先以 `AI_CONVERSATION_DELETED` 进入 expired 终态再软删除会话；若存在任意 running action，则整体 409 且不删除，之后 confirm 不能产生业务写入

### 6.6 明确延期能力

**Scenario: 消息编辑/重新生成保持关闭**

**Given** 当前 MVP 构建
**When** 用户查看历史消息或直接调用前端 store action
**Then** 不显示 edit/regenerate 入口，store 不修改消息、不清附件、不发送请求

---

## 7. 测试矩阵

### 7.1 账号/角色矩阵

| 测试身份 | AI 入口 | Agent 绑定 | 功能权限 | 数据范围 | 预期 |
|---|---:|---|---|---|---|
| 超级管理员 | 有 | 全部已启用 | 全部 | 全部 | 三 Agent 完整成功路径 |
| 华东委派角色管理员 | 有 | `user_mgmt/role_mgmt` | `system:user:list`、`system:user:edit`、`system:user:role-auth`、`system:role:list`、`system:role:add`、`system:role:edit`、`system:role:menu-auth`、`system:role:ai-agent-auth` | 华东及子部门 | 合法创建/授权/分配成功；菜单、Agent、数据和全局影响越界拒绝 |
| 华东部门管理员 | 有 | `user_mgmt/dept_mgmt` | `system:user:list`、`system:user:edit`、`system:dept:list`、`system:dept:add`、`system:dept:edit`、`system:dept:move` | 华东及子部门 | 范围内查询/创建/移动成功，范围外、间接扩权和根节点操作拒绝 |
| 华东目标用户 | 按测试步骤授予 | 按新角色绑定 | `system:user:list` 等委派子集 | 华东及子部门 | 重新登录后只见授权 Agent，查询不越界 |
| 总部范围外普通用户 | 无关 | 无关 | 普通权限 | 总部 | 只验证目标 user data scope 拒绝，不能与 dominance 用例合并 |
| 华东范围内高权限用户 | 有 | 含委派管理员没有的已启用 Agent | 含 `ai:provider:edit` 等越界权限 | 华东及子部门 | 验证不能借完整替换移除/替换操作者无权管理的旧角色 |
| 只读审计员 | 无 | 无业务 Agent | `ai:trace:view`（同时授权 `/ai/trace` 菜单） | 租户内 | 可看脱敏 AI Trace；不能聊天或执行写工具 |
| 只读业务查看角色 | 有 | 对应只读 Agent | list，无写权限 | 按角色 | 查询成功，所有写工具不可见/拒绝 |
| 普通 destructive 操作员 | 无关 | 无关 | `system:dept:delete/batch-delete`、`system:role:delete/batch-delete`，无 `R_SUPER` | 任意 | 传统单个/批量删除均 403，证明按钮权限不是 destructive 超管旁路 |
| 无 Agent 绑定用户 | 有 | 无 | 可有业务菜单 | 任意 | 无业务 Agent，不能伪造选择 |
| 无 AI 入口用户 | 无 | 任意 | 任意 | 任意 | §3.4 用户侧入口 API 403，独立后台权限不被混淆 |

另由超级管理员预置：一个同时包含华东和总部成员的 mixed role；会因部门移动或 status 改变实际范围的 `DEPT_AND_SUB/CUSTOM` 账号；分别具有 `DEPT + CUSTOM`、`DEPT_AND_SUB + CUSTOM` 的多角色账号；委派管理员已绑定和未绑定的两个 disabled Agent。它们分别验证 Role 全局影响、Department 间接授权影响、DataScope 升级 diff 以及潜在 Agent dominance。

### 7.2 分层测试

- Tool/Policy 单测：完整集合旧/新两侧、权限/Agent 集合包含、`visible_agent_ids/grantable_agent_ids` 分离、禁用 Agent 移除、物化 data scope、`DEPT + CUSTOM` 与 `DEPT_AND_SUB + CUSTOM` 并集、用户改部门/部门成员批量变更前后授权、树移动/状态变化、角色全局影响、部门/角色删除引用保护、R_SUPER、自改、空/重复 ID。
- Agent/Gateway 集成：显式选择、粘滞、Supervisor、默认回退、confirm/resume、Tool-Agent 不匹配、dry-run、HITL、审批后权限/目标漂移和事务回滚；模型或入口/Agent/Tool 权限失效后的 reject/最小状态回放不被阻断且不返回业务结果，approve 失败进入终态，新 LLM continuation 才复验冻结模型；统一结果投影 Policy 覆盖 resume/conversation/query-cache/owner-log/download，旧下载 URL 撤权后失效。
- API 测试：逐项覆盖 §3.4 的 401/403/owner/tenant/OR 权限、模块关闭统一 503、chat model capability/伪造 model ID；部门和用户传统 API 与 AI Tool 使用同一 Policy；拥有 user add/import 但没有 role-auth 时显式角色创建/导入零写入拒绝，只拥有 role-auth 而没有 user edit 时更新现有用户角色拒绝，固定 `R_USER` 窄例外单独验收；部门成员 GET 不返回 scope 外用户/邮箱/电话，PUT 的越界成员、主部门移除和前后授权影响整批失败；普通角色的 dept/role delete 403、超管引用保护原子失败。
- Seed/migration：fresh/upgrade 矩阵、旧 `R_SUPER` 无绑定 fixture、shared 无直通、已有 enabled/`ai:enabled_tools` 不被覆盖、已有业务绑定角色入口权限兼容；add-only/edit-only/import-only 历史角色补 role-auth 后各自仍只能使用原用户写入口；DataScope scope-diff 绑定 resolver/build 与角色/成员/部门版本，在维护锁下重跑，扩大项未确认或 hash 漂移时发布失败。
- Agent 管理：任意更新都要求 `R_SUPER + ai:agent:edit`；普通角色修改 name/description/display_order 或敏感字段均拒绝，不改变 Supervisor prompt/候选顺序，混合 payload 不部分写入。
- Provider 网络安全：仅已保存 Provider、协议/origin/IP/DNS/redirect 校验、DNS rebinding、超时、响应大小、并发限制及错误脱敏；默认阻断 loopback/private/link-local，部署 allowlist 例外不能由请求扩张；存量 Provider/Model override 经 audit 后在 test/chat/Supervisor/Agent/continuation 全路径 fail closed，adapter 不能绕过 hardened transport。
- 并发：user role replacement、dept membership replacement、role update、dept move/status/delete、role delete 和在线导入交叉执行；授权相关 writer 都从 role → dept → user 顺序起锁，锁后重新发现受影响对象时释放重试或返回 stale，验证无死锁且成员/关联 phantom 不可越过复验。
- 会话生命周期：删除含 prepared/pending/approved action 的会话时原子 expired，任意 running action 返回 409 且整体回滚；confirm/worker/delete 共用 action row lock，删除后 confirm 零副作用，resume 仅返回终态安全投影。
- 前端 Vitest：Agent 可见性、确认展示/i18n、错误码、Safety Gate、AI Trace 脱敏展示；后端 tombstone 到达后清除旧 store/cache 的 assistant/tool result/UI/args/presentation，不能闪回敏感内容。
- 真实浏览器 E2E：使用唯一 `AI_MVP_E2E_<run_id>` fixture；核心步骤必须通过页面登录、发指令、确认、查看业务页和审计页，不得用 API 代替。测试准备/清理可以使用隔离 fixture，但必须记录 ID、恢复被移动对象并清除本次数据。
- 确定性浏览器 E2E：`pnpm e2e` 使用固定 route/tool fixture 或本地 fake provider，覆盖所有权限、HITL、页面和回归场景，作为 PR/CI 阻断门禁；默认项目不访问外网。
- 真实 provider smoke：独立 Playwright project/tag 和 `pnpm e2e:provider`，至少对 user/dept/role 各执行一个只读指令和一个受控写指令；release job 必须提供受控凭据，缺凭据视为失败而不是 skip。证据记录 provider code、model ID、期望/实际 Agent、实际 Tool、trace ID 和最终业务断言；确定性 fixture 不冒充真实模型验收。
- AI Trace E2E：审计员可查看租户内脱敏 trace，普通用户不能列出全局 trace，跨 tenant 拒绝，成功/拒绝/失败终态均可核对。
- AI Trace migration/API：legacy tenant 回填、unknown Agent、不从 Conversation 猜测、组合索引、稳定分页/筛选、403/404 拒绝面和 target summary allowlist；DTO 序列化断言 message content/raw prompt/raw args/frozen args 始终不存在。

### 7.3 推荐 AI 指令

- 用户：`把 AI_MVP_E2E_张三的主部门调整为 AI_MVP_E2E_华东销售部，并保留 AI_MVP_E2E_项目组作为兼任部门。`
- 用户：`把 AI_MVP_E2E_李四的角色改为普通用户和 AI_MVP_E2E_华东审计员。`
- 部门：`在 AI_MVP_E2E_华东大区下面创建 AI_MVP_E2E_杭州销售二部。`
- 部门：`把 AI_MVP_E2E_杭州销售二部移动到 AI_MVP_E2E_销售中心下面。`
- 角色：`创建 AI_MVP_E2E_华东审计员，数据范围为本部门及子部门。`
- 角色菜单：`给 AI_MVP_E2E_华东审计员授权 AI 聊天入口和用户列表菜单。`
- 角色 Agent：`给 AI_MVP_E2E_华东审计员绑定用户管理 Agent。`
- 越权反例：`把我自己设置为超级管理员。`
- 菜单委派反例：`给 AI_MVP_E2E_华东审计员增加 Provider 编辑权限。`
- Agent 委派反例：`给 AI_MVP_E2E_华东审计员绑定当前已启用但我未绑定的部门管理 Agent。`
- 禁用 Agent 反例：`给 AI_MVP_E2E_华东审计员绑定尚未发布的 Provider Agent。`
- 数据范围反例：部门管理员指令 `把 AI_MVP_E2E_总部财务部的用户调到研发部。`
- 旧角色 dominance 反例：`把 AI_MVP_E2E_华东高权限用户的角色全部改为普通用户。`
- 全局影响反例：`禁用同时分配给华东和总部用户的 AI_MVP_E2E_MIXED 角色。`

---

## 8. 非功能要求

| 维度 | 要求 |
|---|---|
| 安全 | 后端入口权限、统一 Agent Policy、Tool-Agent 归属、工具权限、数据/tenant scope、HITL、二次校验、Provider egress Policy 和敏感字段脱敏缺一不可 |
| 审计 | 每个写工具保留 trace、Agent/Tool、操作者、冻结目标安全摘要、批准/拒绝和终态；AI Trace API/页面按 tenant 授权，不记录密码/API Key/未脱敏 PII |
| 一致性 | Service 不 commit；业务写入失败整体回滚；PreparedAction 终态可恢复且不能漂移，reject/最小状态回放不得因模型失效留下 pending |
| 性能 | 权限和数据范围校验使用批量查询，不得为角色成员、候选对象或树节点产生 N+1；列表/lookup 使用有限候选和稳定排序 |
| 可用性 | Provider/Redis 超时返回稳定错误；AI 熔断不影响传统管理页面 |
| 升级兼容 | fresh 与 upgrade 使用独立 seed 路径；保留部署方模块开关、Agent enabled 和显式绑定，不把 shared 历史直通转换为隐式授权；DataScope 并集切换必须先产出 scope-diff 并对扩大项执行管理员确认门禁 |
| 多租户 | 当前 `tenant_id=0` 仍必须从可信上下文注入并在 pending 批准时复验 |
| i18n | 工具名、确认字段、结果卡和错误码同时覆盖 zh-CN/en-US，不解析后端中文做业务判断 |

---

## 9. 决策记录

1. **用一份收口 spec 作为当前实施入口** — 当前 AI 文档已经按网关、消息、导入导出和视图拆成多个大型 spec，继续每次全量阅读成本高且容易把历史计划误当当前优先级；本文只路由到命中的专项文档。**反例**: 再写一份复制所有技术细节的“大一统 spec”，会与专项 spec 同时演化并产生双重真相源。**回归**: `AGENTS.md` 固定 AI MVP 首读本文；本文 §0 维护按任务阅读表。

2. **消息编辑/重新生成退出当前 MVP** — 该能力不增加管理领域覆盖，却引入修订 lineage、副作用重放和复杂并发状态机；Safety Gate 已保证关闭时无副作用。**反例**: 为追求聊天产品完整度先实现修订，会延迟权限与数据范围架构验证。**回归**: 现有 Safety Gate Vitest 持续通过，相关成功路径不进入当前发布门禁。

3. **MVP 固定验证 User + Dept + Role 三个管理领域** — 三者分别覆盖普通行级操作、树形组织数据和权限集合/数据范围，能验证架构是否真正可扩展。**反例**: 只继续扩充 user tool 数量，无法暴露层级 scope、权限 dominance 和 Agent 授权的架构问题。**回归**: E2E 必须对三个 Agent 各覆盖只读、写入、无权限和越界场景。

4. **`user.update_dept` 与 `user.update_roles` 保持独立工具** — 部门迁移和角色授予的授权对象、风险、审批展示及漂移条件不同，不能混入资料字段 PATCH。**反例**: 单个 `user.update` 接受任意字段会让一次确认难以说明实际授权内容，并扩大 LLM 可写面。**回归**: Registry、dry-run、i18n 和操作日志分别使用两个 tool code。

5. **AI 模块默认开启，安全默认值下沉到授权链** — HoHu 的产品定位要求 AI 开箱可用；模块级关闭只能用于部署能力和紧急熔断，日常访问由入口权限、Agent、Tool 和数据范围控制。**反例**: 生产默认关闭会掩盖授权链缺口，并让新安装看起来没有核心功能。**回归**: 配置默认值保持 true；无 `ai:chat:use`、无绑定、无 tool permission、越界数据四层拒绝均有后端测试。

6. **新增后端 `ai:chat:use` 并统一全部 Agent 选择路径** — 当前 chat 只校验登录，shared、手工/粘滞选择和默认回退存在不同可见性语义；菜单隐藏不能形成安全边界。**反例**: 只修 Agent 列表或 shared 直通，仍可通过显式 `agentCode`、旧粘滞值或 PreparedAction 恢复选择未授权 Agent。**回归**: §3.4 endpoint 矩阵及显式/粘滞/Supervisor/default/confirm/resume/Tool-Agent mismatch 测试全部通过。

7. **fresh 与 upgrade 使用不同 seed 语义** — fresh 需要 AI-first 开箱体验，upgrade 则不能覆盖部署方手工禁用或扩大普通角色权限；二者不能由一个无上下文 upsert 默认值表达。**反例**: 每次 seed 都把“已完成 Agent”翻回启用，会恢复管理员主动关闭的能力；完全不迁移又会让已有显式绑定角色突然失去入口。**回归**: §3.3 四类 seed/migration fixture 断言模块值、Agent enabled、入口权限、Role-Agent 和 shared 行为。

8. **Provider 首期只读/测试，密钥写入继续走 UI** — Provider Agent 依赖已有 Provider 才能运行，且聊天输入 API Key 会把密钥暴露给模型和消息持久化。**反例**: 用 AI 初始化自己的模型 Provider 形成启动循环，并扩大密钥泄漏面。**回归**: Provider tool schema 不含原始 API Key；敏感字段序列化和审计测试持续拒绝密钥输出。

9. **管理页面与 AI 共用授权 Service** — 权限 dominance、R_SUPER 和部门范围是业务规则，不应只在某个入口临时实现。**反例**: AI 拒绝提权但传统用户编辑 API 仍可越权，或两处规则更新不同步。**回归**: 同一 Policy/Service 的 API 与 Tool 测试分别覆盖相同拒绝场景。

10. **Role 写操作支持受限委派** — 只有支持普通管理员在自身授权上界内创建、授权和分配角色，才能验证功能权限、数据权限、Agent 路由和权限委派架构；`role.update_menus/update_agents` 不整体硬编码为仅超级管理员。**反例**: 所有 Role 写都让 `R_SUPER` 执行，只能证明超级管理员旁路可用，无法证明 dominance。**回归**: 华东委派管理员合法子集成功，菜单/Agent/data scope 越界、mixed-role 全局影响、自改和 `R_SUPER` 全部 fail closed。

11. **dominance 比较实际授权集合，多角色 data scope 按物化并集合并** — `CUSTOM` 与 `DEPT` 可能不可比较，Role 修改还会同时改变菜单、Agent 和全部成员的实际访问集合。**反例**: 用整数优先级只取一个“最大” scope，会丢失其他角色的 CUSTOM 权限，也可能把包含范围外部门的 CUSTOM 错判为更小。**回归**: 共享 resolver、§4.3 偏序贡献表、集合包含、具体用户物化 scope 和全部成员变更前/后测试通过。

12. **Department 读取应用 data scope，结构/状态变化分析间接授权影响** — 部门 Agent 必须验证层级查询和树结构写入；父级或状态变化还会改变 `DEPT_AND_SUB/CUSTOM` principal 的实际范围。**反例**: 页面 `dept.update(parent_id=...)` 绕过 move，或把 status 当普通展示字段而恢复 scope 外角色访问。**回归**: 页面/API/Tool scoped selector 一致，基础 update 拒绝结构字段，委派 move 范围内成功，跨范围/根节点/移动或状态间接扩权失败。

13. **聊天、Agent 配置与 Provider 管理使用独立模型列表，所有新 LLM run 共用 selector** — 同一 `/ai/provider/models` 服务三类用户会迫使实现选择过宽或过窄的权限；仅过滤列表也挡不住伪造 `modelId`。**反例**: `ai:agent:list` 借模型 options 读取 Provider Base URL，或提交不含 text capability 的启用模型绕过前端选项；反过来给 reject/状态回放强加模型校验又会卡住终态。**回归**: 三个模型端点和 conversation/chat/LLM continuation 按 §3.4 校验；reject/授权后的完整回放及最小状态回放在模型失效时仍收口且不调用模型。

14. **AI Trace 是带独立数据契约的 MVP 审计闭环** — 写工具只有直接持久化 tenant、Agent、可查询的安全摘要和终态，才能由独立审计角色核对页面结果、PreparedAction 与业务写入。**反例**: 从可变 Conversation 反推 Agent，或只保留已知 tool-call 的轮询端点，会让 autonomous/legacy 日志无法可靠审计。**回归**: migration/index、trace list/detail API、`/ai/trace` 页面、tenant/权限/脱敏测试及真实浏览器审计 E2E 通过。

15. **Role-Agent 委派不包含任何 Agent 全局定义修改** — 绑定 Agent 是角色可见性授权；name/description/display order 参与 Supervisor prompt/候选顺序，enabled、system prompt、模型和风险偏好影响执行，均不是普通委派字段。**反例**: 普通角色管理员通过描述 prompt injection、候选排序或降低 risk appetite 影响所有用户。**回归**: Agent update 所有可变字段均要求 `R_SUPER + ai:agent:edit`，普通角色和混合 payload 整体拒绝。

16. **多角色 DataScope 并集切换设置防漂移升级审计门禁** — 并集是可执行 dominance 的正确语义，但会让部分存量账号获得旧算法漏掉的可见集合，必须在生产切换前显式暴露并冻结授权差异。**反例**: 报告后成员或部门树变化却沿用旧确认，会把未经审阅的新扩大项带入上线。**回归**: scope-diff 绑定 resolver/build 和角色/成员/部门版本，发布时在维护锁下一致快照重跑；hash 不一致或扩大项未确认即失败，确认后页面/API/AI 并集一致。

17. **角色变更使用用户写权限与独立委派权限的交集** — `system:user:add/edit/import` 分别决定可操作的用户写入口，`system:user:role-auth` 再决定该入口能否写角色；固定 `R_USER` 仅作为无用户输入的兼容窄例外。**反例**: 只凭 add/import 分配高权角色，或给 add-only 角色补 role-auth 后让其获得现有用户角色编辑能力。**回归**: update 同时要求 edit + role-auth，create/import 分别要求 add/import + role-auth；迁移向三类历史 writer 补 role-auth 仍不扩张入口，固定 `R_USER` 继续 dominance。

18. **全部 Provider 出站调用统一 hardened egress Policy** — test、chat、Supervisor、Agent 和 continuation 最终都会使用 Provider/Model URL，任一路径直连 SDK 都可能把后端变成 SSRF 代理。**反例**: 只加固 test，却让存量 Model override 经正常聊天访问 metadata/loopback 地址。**回归**: 所有 adapter 注入统一 transport，协议/origin/IP/DNS/redirect/timeout/size/error-redaction 全路径测试通过，存量不合规配置被运行时隔离，请求不能扩张部署 allowlist。

19. **AI Trace 只暴露消息元数据和脱敏摘要** — 审计闭环需要关联 source message，但不需要把原始用户内容或 system prompt 返回给审计角色。**反例**: DTO 返回 `sourceMessage.content`，即使页面不渲染也可经 API 泄露敏感会话。**回归**: trace DTO 序列化不存在 message content/raw prompt/raw args/frozen args，仅保留 ID、角色、时间和 allowlist 摘要。

20. **Agent 路由可见集合与委派潜在能力集合分离** — disabled Agent 不能参与聊天路由，但 `RoleAiAgent.enabled=true` 的已有绑定会在 Agent 全局重新启用时恢复能力，Role dominance 必须把它计入潜在上界；soft-disabled Role-Agent 行不贡献当前委派能力。**反例**: 用仅含 enabled Agent 的集合检查 `旧 ∪ 新`，会让普通管理员连移除自己已绑定的 disabled Agent 都失败；把 `RoleAiAgent.enabled=false` 的历史行也计入上界，又会授予操作者当前 UI/API 都不承认的隐藏委派能力。**回归**: `tests/modules/ai/test_agent_authorization_service.py::test_grantable_agents_include_disabled_agent_but_not_disabled_binding`、`tests/modules/system/test_grant_authority.py::test_build_freezes_explicit_authority_and_materialized_scope`。

21. **用户部门调整校验目标用户前后物化授权** — 部门 ID 在操作者 scope 内不代表目标用户的 `DEPT_AND_SUB` 最终可见子树也在范围内。**反例**: CUSTOM 管理员把用户调入一个可见父部门，却让目标用户借角色获得操作者不可见的后代。**回归**: `user.update_dept` 对目标用户完整角色集合物化前后 user/dept scope，任一不为操作者子集时页面与 AI 都拒绝。

22. **无副作用的 reject/最小状态回放独立于执行权限，会话删除原子终态化 action** — 权限撤销必须阻止 approve 和业务结果读取，但不应阻止 owner 放弃操作或确认 action 已收口；删除聚合根也不能留下可执行孤儿。**反例**: confirm router 统一要求 chat 后撤权用户无法 reject，或 owner-only resume 仍返回 `result_data/result_ui`。**回归**: 撤权后 approve 零写入并终态失败、reject 成功、resume 只返最小状态；会话删除将非运行 action 原子 expired，running 409 回滚。

23. **Phase 1 与 Phase 2 只构成原子集成门禁，生产发布等待 Phase 4** — Phase 1 会移除旧 Role-Agent 旁路，普通管理员安全写入依赖 Phase 2；传统 destructive writer、三个 Agent 完整实现和不可可靠回填的 Trace 字段又分别依赖 Phase 3/4。**反例**: 把 Phase 1+2 当生产发布会暴露尚未收口的 delete cascade，并产生缺 tenant/Agent/target snapshot 的新日志。**回归**: CI 拒绝只含 Phase 1 的集成，部署门禁拒绝未完成 Phase 3/4 的构建；最终发布时委派、传统 writer、Trace 与 E2E 同时通过。

24. **部门成员页面是用户部门授权入口而非普通部门字段** — 单部门成员列表会跨多个用户写 `user_depts`，必须逐人重建完整部门集合并分析最终授权。**反例**: 只按可见候选做 diff 会删除被过滤的 scope 外成员，或返回全租户邮箱/电话。**回归**: GET 候选 user-scoped 且最小化，隐藏旧成员使整体阻断；PUT 要求 dept edit + user edit，对全部旧/新用户执行完整 Policy，主部门移除和任一越界整批失败。

25. **延期 AI delete Tool 不等于保留传统删除旁路** — destructive 委派尚未设计完成时，现有 Dept/Role 单个与批量删除必须先收紧到 `R_SUPER + 原权限` 并阻止 cascade 改授权。**反例**: 普通按钮权限删除被 Role custom scope 引用的部门或有成员的角色，绕过所有 dominance。**回归**: 普通管理员 403，保护/子级/成员/引用检查在全局锁内完成，batch 任一失败零删除且关联表不被 cascade 清理。

26. **历史业务结果在所有读取面实时重授权** — conversation、resume、query-cache 和 owner log 是同一持久化结果的不同投影，不能只加固其中一个。**反例**: 撤销 Role-Agent 后 resume 返回 tombstone，但 conversation detail 仍回放旧 tool result/UI。**回归**: 不可变 Agent/Tool/subject lineage 驱动统一 `authorize_result_projection()`；失权或 legacy 不可证明时所有读取面一致 tombstone/最小状态，前端清除旧缓存，审计角色只走独立 allowlist DTO。

27. **功能权限只有一个收集器，菜单禁用只控制路由可见性** — API dependency、前端按钮权限和 AI Tool 权限统一使用启用角色贡献的显式 `menu.permission` 集合；为兼容现有全局语义，`menu.status=2` 不立即撤销已关联角色的 API 权限。**反例**: AI 单独复制权限汇总并过滤禁用菜单，会让页面按钮、普通 API 与 AI Tool 对同一角色给出不同结论。**回归**: 三条调用链委托同一 collector；禁用角色不贡献权限，禁用菜单仍贡献权限，并由权限矩阵测试锁定。

28. **P1-B 以统一 Agent/Model Policy 消除选择路径旁路** — 显式、粘滞、Supervisor、默认和恢复执行若分别查询 Agent/模型，会在撤权、禁用或伪造 ID 时产生不同结果；Agent 列表、选择与恢复统一委托 `AgentAuthorizationService`，新 LLM run 统一委托 `ModelAuthorizationService`。**反例**: 列表隐藏 shared，但 Gateway 仍允许其它运行时 Agent 调用 `file.parse`；或下拉框过滤禁用模型，chat payload 仍能直接提交。**回归**: Role-Agent/Agent/Tool 任一层缺失均稳定拒绝，Tool 与运行时 Agent 必须精确相同，三模型端点只返回安全字段，显式无效模型零写入且不 fallback。

29. **阶段发布集合只包含当前已闭环 Agent，upgrade 不翻转显式状态** — Phase 2/3 尚未补齐的三个业务 Agent 不能因最终 MVP 目标而在 P1-B 中间构建提前启用；upgrade 也只能补缺失绑定，不能把管理员显式禁用的 Agent 或 Role-Agent 重新开启。**反例**: fresh seed 现在就启用 `user_mgmt/dept_mgmt/role_mgmt`，会宣称尚未具备的完整集合/受限委派能力；migration 把 `enabled=false` 的 R_SUPER 绑定改回 true 会覆盖部署决策。**回归**: P1-B 发布集合仅含已闭环的 `shared`，未发布描述明确标注“尚未发布”；fresh 只启用阶段集合，upgrade 保留 Agent、Role-Agent 与 `ai:enabled_tools` 的既有值。

30. **模型与 Agent 拒绝语义不能被 truthiness 或 fallback 吞没** — chat raw payload 必须按字段是否存在区分 omitted 与显式 falsy 值；显式 `modelId=null/0/""` 必须进入 selector 并拒绝，显式空值或非字符串 `agentCode` 必须返回 `AI_AGENT_FORBIDDEN`，只有字段 omitted 才能使用会话/Agent 默认。统一 selector 的稳定业务拒绝必须原样传播，不能伪装成 Agent 路由歧义。**反例**: `modelId=null` 被当成未提交并执行会话默认模型，`agentCode=false` 被静默改为默认 Agent，或 Supervisor 把 `AI_MODEL_NOT_AVAILABLE` 转成 `clarification_required`。**回归**: `tests/modules/ai/test_chat_supervisor.py::test_explicit_falsy_model_id_is_rejected_without_fallback`、`test_explicit_invalid_falsy_agent_code_is_rejected_without_fallback`、`tests/modules/ai/test_chat_service.py::test_create_agent_preserves_explicit_falsy_model_ref`、`tests/modules/ai/agents/supervisor/test_router.py::test_route_propagates_model_authorization_failure`。

31. **legacy approve 的执行资格拒绝共用终态收口** — rolling-upgrade 遗留的 Redis-only action 在入口撤权或用户自动禁用后都必须先把 operation log/pending projection 收口为 expired、以 rejected 唤醒 waiter，并在离线时释放 guard 和删除 pending，再返回稳定 403。**反例**: `AI_USER_DISABLED` 只返回错误而让 confirmation 保持 pending 到 TTL。**回归**: `tests/modules/ai/test_confirm.py::TestUserDisabled::test_disabled_user_blocked`。

32. **Provider 出站同时校验存储配置与每次最终请求** — 保存时校验只能阻止新坏配置，不能永久信任 DNS 或兼容存量；Provider/Model 两级 URL、网络配置键和 SDK 最终 request 必须共用部署方 allowlist，连接固定到当次全部校验通过的 IP，重试重新解析。**反例**: 只加固 test endpoint、允许 body 传临时 `baseUrl`，或让 OpenAI/Anthropic SDK 自建读取环境代理的 client，仍可从 chat/Supervisor/continuation 绕过 SSRF 边界；把 `modelId` 接受为 JSON number 还会引入 Snowflake 精度风险。**回归**: `tests/modules/ai/test_provider_egress.py`、`tests/modules/ai/test_provider_service.py`、`tests/modules/ai/test_provider_error_handling.py`、`tests/scripts/test_audit_ai_provider_egress.py`。

33. **固定 IP 不能牺牲 TLS origin 身份、取消归还或解压后大小边界** — httpcore 按实际 URL origin 复用连接，若不同 hostname 共用固定 IP 和同一连接池，后一个请求可能复用前一个 hostname 的 TLS 会话；每个原始精确 origin 必须拥有独立底层池。获得并发 permit 后的取消属于 `BaseException` 路径，必须同步归还；响应大小门禁必须在 SDK 解压前成立，因此请求强制 `Accept-Encoding: identity`，Provider 仍返回压缩成功响应时 fail closed。**反例**: 只断言 request extension 含 SNI，却共享按 IP 分组的真实连接池；客户端断流累计耗尽 semaphore；用 132 字节 gzip 解压出 100 KB 绕过 1 KB 限制。**回归**: `tests/modules/ai/test_provider_egress.py::test_transport_isolates_connection_pools_by_original_origin`、`test_transport_releases_concurrency_permit_when_request_is_cancelled`、`test_transport_rejects_compressed_response_before_sdk_decoding`。

34. **业务结果 lineage 只能由可信 Tool 投影产生，终态读取每次重新授权** — assistant message、PreparedAction、query-cache 与下载 token 共用规范化 Agent/Tool/subject/scope lineage；PreparedAction 在确认前冻结输入目标，成功终态再以 `ToolResult.projection` 原子替换为实际结果目标，legacy/incomplete 不反解析 result 猜授权。**反例**: 从 `result_data` 抽取一个 user ID 当作完整目标，或让旧下载 URL 在撤权后继续作为 bearer capability。**回归**: finite subject refs 逐项走领域 Policy，聚合结果比较 resolver + canonical scope hash；conversation/resume/pending/query-cache/owner-log/download 使用同一 Policy，失权分别 tombstone/最小状态/同面 404；下载 token 绑定 owner、tenant、resource 与 projection hash，5 分钟失效且读取时再次授权，历史加载只在授权后刷新 URL。

35. **Bearer 能力按用途分域并在副作用前与最终读取点重验** — API access JWT、AI 下载 token 和实时投影授权承担不同能力，不能共享可互认的 token 域，也不能把等待前或预检时的授权结论当作最终凭据。**反例**: 下载 token 被通用认证当作 access token，tokenized URL 进入外部 LLM，上次 data scope hash 已漂移仍批准执行，或 resume 等待期间撤权后仍回放成功结果；导出先写文件再发现无权会留下孤儿。**回归**: `tests/modules/auth/test_refresh_token.py`、`tests/middleware/test_audit_user_cache.py`、`tests/modules/ai/test_result_projection_service.py`、`tests/modules/ai/test_prepared_action_service.py`、`tests/modules/ai/test_confirm.py`、`tests/modules/ai/test_resume.py`、`tests/modules/system/test_ai_tools_user_export.py` 分别锁定 exact access type、签名分域、scope 漂移、终态重授权、UI-only token 与导出预授权/补偿删除。

36. **Web 以最新后端安全投影替换旧内存，不合并保留历史业务结果** — tombstone 和最小 pending status 表示当前读取授权已撤销，前端必须把 conversation detail 视为权威投影并清除旧消息结果、工具卡、SSE recovery buffer 与 confirmation presentation；首次挂载和 `KeepAlive` 再激活都必须重新获取当前 detail，撤权投影还要使主 SSE、resume、polling 及其 generation 同步失效。**反例**: detail 返回 tombstone 后，stream handoff 仍因找不到 durable assistant 而保留带业务数据的 temp message，最小 pending status 与旧 drawer presentation 合并，晚到的 SSE chunk 回填被撤销结果，或页面缓存重新激活时直接展示旧 store。**回归**: `src/store/modules/ai/__tests__/projection-revocation.spec.ts`、`src/views/ai/chat/__tests__/chat-route-reauthorization.spec.ts`、`src/views/ai/chat/modules/__tests__/chat-tool-call-embed.spec.ts` 锁定重新授权、异步生产者失效、旧内存清除、稳定 tombstone 和零工具卡恢复。

37. **安全模型 option 使用稳定 modelId，展示 label 不参与写契约** — `/ai/chat/models` 与 `/ai/admin/agents/model-options` 固定只返回最小字段，Web 分别调用且直接提交字符串 `modelId`；Agent update 在 rolling upgrade 中同时接受稳定正整数 `modelId` 和 legacy `provider:model`。Agent Drawer 必须先完成 detail 再加载 option，并以加载序号拒绝旧响应，确保当前 legacy preference 的 fallback 不受网络响应顺序影响。**反例**: Agent 抽屉继续调用 Provider 管理列表读取 Base URL，从 `Provider Name / Model` 展示 label 反解析模型名，或 options 先返回而漏掉当前 legacy preference。**回归**: `src/service/api/__tests__/ai-phase1-contract.spec.ts`、`src/views/ai/agent/__tests__/agent-operate-drawer.spec.ts`、`tests/modules/ai/test_agent_admin.py::test_model_preference_accepts_safe_option_model_id`。

38. **Provider test 只验证已保存快照，未保存修改必须先落库** — `POST /ai/provider/{provider_id}/test` 只接受持久化 Provider/Model ID；Web 编辑 Drawer 只要 Provider code、名称、凭据、Base URL、启用状态或 config 与打开时快照不同，就禁用测试并提示先保存，不能让旧数据库配置的测试结果伪装成新表单配置结果。**反例**: 管理员修改 API key 或受阻 Base URL 后直接点测试，页面实际探测旧配置却显示为当前表单的结果。**回归**: `src/views/ai/provider/__tests__/provider-operate-drawer.spec.ts`。

39. **R_SUPER 的 AI Tool 能力也只来自显式权限集合** — 超级管理员可继续使用传统 API 的既有 bypass，但 AI Gateway 不得把 Registry 全部 required permissions 合成为隐式授权；Agent 可见性、Tool schema 和执行校验都使用统一权限 collector。**反例**: R_SUPER 只绑定 `ai:file:parse`，却因代码合成全部 Registry 权限看到并调用角色或部门 Tool。**回归**: `tests/modules/ai/test_agent_authorization_service.py::test_super_role_tool_permissions_use_explicit_collector`，并删除 `all_registry_perms()` 生产入口。

40. **跨轮输出采用保守的传递投影谱系** — 客户端 history 不能把历史业务结果变成新的授权根；每轮冻结会话内全部既有 active assistant message ID，message、PreparedAction、query-cache v3 和下载 token 共享该依赖集合并在读取时递归重验。**反例**: 旧 assistant 的 Role-Agent 已撤销，但新输出只记录本轮 Tool，随后以新 lineage 重新显示旧结果。**回归**: `tests/modules/ai/test_result_projection_service.py`、`test_prepared_action_service.py`、`test_query_cache.py` 和 `test_result_download_api.py` 覆盖依赖冻结、legacy fail closed、token 绑定和实时重验。

41. **Web 运行时拒绝必须同时撤销内存与异步生产者** — `AI_MODULE_DISABLED`、`AI_CHAT_PERMISSION_DENIED`、`AI_AGENT_FORBIDDEN`、`AI_AGENT_NOT_AVAILABLE` 和 `AI_MODEL_NOT_AVAILABLE` 统一进入 fail-closed availability handler；按错误作用域清除 conversation/result/presentation 或 model/Agent cache，并让 SSE、resume、polling、detail/list/model/Agent 请求 generation 全部失效。**反例**: 新 init 已返回 403，旧 init 的 models/list 成功响应随后覆盖拒绝状态；或 confirm 403 后旧 presentation 和晚到 SSE 仍可见。**回归**: `src/store/modules/ai/__tests__/runtime-revocation.spec.ts` 与 `projection-revocation.spec.ts`。

42. **DataScope 升级审计分别冻结旧 API 与旧 AI 语义** — Phase 2 前传统 User filter 与 AI `DataScopeContext` 对非 SELF scope 是否包含 actor 并不完全相同，不能用一份伪造的 legacy 集合代替两者；tenant 只能来自服务端可信常量。**反例**: 把旧 AI 已可见的 actor 报告为新增用户，或允许 `--tenant-id` 把全局数据重新标记成任意租户后生成可 ACK hash。**回归**: `tests/scripts/test_audit_data_scope_union.py::test_report_separates_legacy_api_and_ai_self_semantics`、`test_release_args_reject_untrusted_tenant_and_incomplete_commands`。

43. **精确 ACK 与代码切换共用一个 session 维护锁生命周期** — transaction advisory lock 会在审计函数返回时释放，不能证明随后切换的仍是刚确认的授权事实；发布脚本持有 session lock，依次执行停止 writer、repeatable-read 重审计、ACK、同 build 激活和验证。**反例**: `--verify-ack` 返回 0 后释放锁，再由另一步重启服务，期间授权 writer 改变角色或部门集合。**回归**: `tests/modules/system/test_authorization_lock.py::test_session_migration_lock_uses_bound_lock_and_unlock_calls`、`tests/scripts/test_audit_data_scope_union.py::test_locked_release_holds_lock_through_switch`。

44. **角色写请求只接受 canonical Snowflake 字符串且显式 null 失败** — `roleIds` 在 JSON/OpenAPI 中固定为正十进制字符串数组；create 只有字段真正省略时才走固定 `R_USER`，显式 `null`、JSON number、零值和前导零均拒绝。普通资料更新不接受 `password`，密码只能走独立 reset/change endpoint。**反例**: 把 `null` 当省略会绕过 role-auth，把 BigInteger 暴露为 JSON number 会在浏览器端丢精度，接受后静默忽略 password 会制造虚假成功。**回归**: `tests/modules/system/test_user_role_contracts.py`、`tests/modules/system/test_user_api_atomicity.py`。

45. **导入角色授权冻结解析事实并在锁内整批复验** — preview/execute 都使用共享 `GrantAuthority` dominance，不再以“操作者当前持有相同 role ID”作为委派规则；execute 预读每行 role/dept/user 解析结果与目标关联，按 role → dept → user 锁定已发现父 row，锁后重新解析并比较，新增可解析对象、目标用户或关联变化统一 stale。锁内重验 import/role-auth，任一行授权越界时不写任何 `sys_user/user_roles/user_depts`；解析后的稳定 ID 直接进入写入，名称不再二次解析。**反例**: 合法委派角色因 actor 未直接持有而被拒绝；权限在 adapter 检查后撤销仍可落库；未解析对象在锁后出现并改变分类；一行越权但其他行继续创建。**回归**: `tests/modules/system/test_user_import_dry_run.py`、`tests/modules/system/test_user_import_execute.py`、`tests/modules/system/test_user_role_assignment_service.py`。

46. **用户部门写入是完整替换授权操作** — 页面 `PUT /system/user/{id}/departments` 只接受 canonical Snowflake 字符串与严格 boolean 组成的完整集合，API dependency 和共享 Service 都校验 `system:user:edit + system:dept:list`；Service 在任何关联写入前同时校验目标用户、旧/新部门、主部门配置和目标完整角色集合的前后物化授权。预读得到的角色、自定义部门、用户部门和 `DEPT_AND_SUB` 结构影响集合按 role → dept → user 加锁，锁后重载；新发现依赖或主部门关联漂移统一 `AUTHORIZATION_SNAPSHOT_STALE`。**反例**: 只检查新部门 ID 会允许删除 scope 外旧关联，或把用户调入可见父部门后借 `DEPT_AND_SUB` 获得操作者不可见的子树；先删后验会在失败时留下部分集合。**回归**: `tests/modules/system/test_user_department_assignment_service.py`、`tests/modules/system/test_user_role_contracts.py`、`tests/modules/system/test_user_api_atomicity.py`。

47. **策略配置和假设作用域必须使用当前事务事实** — `user_require_primary_dept` 属于授权写入门禁，必须绕过 300 秒 best-effort cache 并在事务内锁定配置行；以候选部门计算目标用户授权时，数据库查询返回的旧主体必须先移除，再仅按候选部门与 `include_self` 语义重建。**反例**: 配置已从 false 改为 true 但旧缓存仍允许清空部门；CUSTOM 角色绑定旧部门 A、用户拟迁往 B 时，旧 A 查询残留该用户并把越权影响误判为可接受。**回归**: `tests/modules/system/test_user_department_assignment_service.py::test_replace_departments_bypasses_stale_primary_policy_cache`、`tests/modules/system/test_user_role_assignment_service.py::test_hypothetical_custom_scope_drops_subject_from_removed_department`。

48. **所有在线用户部门 writer 统一进入共享 Policy** — 页面 create、AI `user.create`、import create/overwrite 和部门中心成员更新都必须使用同一个部门完整集合授权边界；导入在整批 role → dept → user 锁内复验组合后的最终角色/部门事实，再调用仅允许已验证锁内写入的关联 helper。**反例**: import overwrite 或 AI create 继续直接写 `user_depts`，会绕过隐藏旧关联、主部门配置和目标前后物化授权。**回归**: `tests/modules/system/test_user_api_atomicity.py`、`tests/modules/system/test_ai_tools_user_create_reset_password.py`、`tests/modules/system/test_user_import_dry_run.py`、`tests/modules/system/test_user_import_execute.py`，且运行时代码中 `user_depts` 写入只保留在 `UserDepartmentAssignmentService`。

49. **部门中心成员更新先验证完整集合，再批量物化授权** — GET 必须先发现全部旧成员，任一隐藏成员使整体 403；PUT 对全部变更用户批量加载角色、部门、Role-Agent 和 data scope，锁后再次批量物化并在任何写入前完成逐用户 dominance。**反例**: 过滤 scope 外旧成员后提交可见子集会静默删除隐藏关联；逐用户查询又会让大部门产生 N+1。**回归**: `tests/modules/system/test_department_membership_service.py`、`tests/modules/system/test_dept_membership_contracts.py`、`tests/modules/system/test_user_role_assignment_service.py::test_bulk_authority_materialization_matches_single_policy`。

50. **Web 只在授权且关联集合变化时调用独立 writer** — 新建用户只有拥有 `system:user:role-auth` 才提交显式 `roleIds`，只有拥有 `system:dept:list` 才加载/提交部门；编辑页面把 profile、roles、departments 分别提交到独立 API，未变化的关联不重写。部门成员弹窗分页获取完整最小候选，主部门成员不可从该入口移除。**反例**: 沿用宽 payload 会得到虚假保存成功；普通资料编辑时无条件重写关联会把未授权或隐藏旧集合错误带入写请求。**回归**: `src/views/system/user/modules/__tests__/user-operate-drawer.spec.ts`、`src/views/system/dept/modules/__tests__/dept-users-modal.spec.ts`。

51. **导入按唯一目标和传递范围冻结最终授权组合** — 同一现有用户被多行解析命中时，两行都以 `AI_IMPORT_DUPLICATE_TARGET` 进入冲突集合，不允许把前一行角色与后一行部门组合成未经 dominance 的终态；只要批次涉及启用的 `DEPT_AND_SUB`，全局锁集合必须展开所有直接/自定义部门的完整后代，再在锁内分类和写入。**反例**: 第一行写角色 B/部门 X、第二行角色留空/部门 Y 会形成未验证的 B+Y；只锁父部门又允许并发向子部门新增 scope 外成员。**回归**: `tests/modules/system/test_user_import_execute.py::test_import_classification_rejects_duplicate_existing_targets`、`tests/modules/system/test_user_role_assignment_service.py::test_import_lock_includes_descendant_department_dependencies`。

52. **完整集合页面在不完整读取和锁后授权漂移时 fail closed** — 部门成员 PUT 比较锁前/后的操作者 version、角色定义、menu/custom dept、active Role-Agent、物化用户/部门集合及部门结构快照，任一变化返回 `AUTHORIZATION_SNAPSHOT_STALE`；Web 成员候选任一分页失败都保持不可提交，用户编辑多个独立 writer 中后续失败时必须明确提示部分成功、关闭旧表单并刷新服务端事实。**反例**: 后页失败后提交空集会删除全部非主部门成员；角色写已 commit 后部门失败却继续展示旧表单会造成虚假原子保存。**回归**: `tests/modules/system/test_department_membership_service.py::test_member_replacement_rejects_role_definition_drift_after_preload`、`dept-users-modal.spec.ts`、`user-operate-drawer.spec.ts`。

53. **Task 12 lookup 不得为路径展示扩大读取面** — `user.dept_lookup` 使用 `system:dept:list` 与当前部门 scope，名称/路径候选仅由可见且启用节点拼接，scope 外祖先不查询、不返回。**反例**: 返回完整祖先链会让可见子部门泄露不可见总部名称；沿用 `system:user:add` 会阻断 edit-only 委派者。**回归**: `tests/modules/ai/test_user_assignment_tools.py`。

54. **Task 12 批准前复验服务端部门业务快照** — direct HITL 冻结规范化完整部门集合及操作者 authority version、目标身份/状态/角色、主部门配置、角色定义和前后物化授权 hash；snapshot 仅存 PostgreSQL，批准时重建，任一漂移统一 `AI_PREPARED_ACTION_SNAPSHOT_STALE`，执行时共享 Service 再按 role → dept → user 锁协议复验。**反例**: 仅依赖冻结 args 或 chat scope hash，无法发现旧部门、目标角色、主部门配置或间接授权影响变化。**回归**: `tests/modules/ai/test_user_assignment_tools.py`、`tests/modules/ai/test_prepared_action_service.py`。

55. **Task 12 不新增第二套部门 writer** — `user.update_dept` 只编排 `UserDepartmentAssignmentService.replace_departments()`，继承页面完整旧/新集合、scope、dominance、主部门与保护账号规则；Tool/Gateway/Service 均不 commit。本工作包不新增数据库 schema/migration、权限或菜单 seed、新权限码；只同步内置 `user_mgmt` prompt 的已知默认值升级集合。**反例**: Tool 直接写 `user_depts` 会绕过已完成的 P2-B2/B3 Policy。**回归**: `tests/modules/ai/test_user_assignment_tools.py` 与现有部门 Service 回归。

56. **Task 12 模型输入、当前集合和 dry-run 拒绝必须形成同一 fail-closed 链路** — `user.update_dept.dept_assignments` 的模型 schema 固定为禁止额外字段的 `{dept_id: positive integer, is_primary: boolean}[]`；`user.lookup` 只有在调用者同时拥有 `system:dept:list` 且全部现有关联都位于当前部门 scope 时才返回 `departmentAssignmentsComplete=true` 和完整集合，否则返回空集合与 `false`，无部门读取权限时保持旧响应形态。`user.dept_lookup` 先按末级名称筛选可见启用候选，再仅向上构造候选路径并限制结果物化。dry-run 抛出的领域/授权异常是 terminal Tool failure，Gateway 保留原始 `errorCode`、写入失败审计并停止在 confirmation 之前。**反例**: 任意 dict schema 允许模型提交 camelCase/多余字段；只返回可见部门子集会让完整替换静默删除隐藏关联；把 scope 拒绝改写成“预估失败”并继续创建 pending 会批准一个没有业务快照的动作；先物化全部部门再在 Python 截断会让 selector 成为线性扫描。**回归**: `tests/modules/ai/test_user_assignment_tools.py`、`tests/modules/ai/test_executor_integration.py`。

57. **Task 13 角色 lookup 与当前全集只暴露可委派对象** — `user.role_lookup(query, limit)` 只要求 `system:user:role-auth`，复用页面 `list_assignable_roles` 的 Role Delegation Policy，仅返回启用且 permission/menu/Agent/scope 模板均受操作者 `GrantAuthority` 支配的 `{roleId, roleCode, roleName, dataScope}`；`query` 按 code/name、`limit` 固定 `1..20`，零或多命中必须澄清。`user.lookup` 只有在调用者持有 role-auth、目标仍在当前 user scope、当前角色集合非空且每个角色都可委派时才返回 `roleAssignmentsComplete=true` 和完整集合；否则返回空集合与 `false`，不得泄露或伪装部分旧角色。**反例**: 复用 `role.list` 会错误要求 `system:role:list` 并返回不可委派角色；把可委派子集当当前全集会借替换删除调用者无权管理的旧角色。**回归**: `tests/modules/ai/test_user_role_assignment_tools.py` 覆盖权限、同名/零命中、不可委派角色和完整集合降级。

58. **Task 13 角色调整冻结严格全集和最终有效授权快照** — `user.update_roles` 只接受 `user_id + role_ids: positive integer[]`，数组必须非空、去重且禁止字符串强制转换；要求 `system:user:edit + system:user:role-auth`、`high + hitl_always + dry_run`，并只编排 `UserRoleAssignmentService.replace_roles()`。dry-run 冻结排序后的执行 ID、目标状态/当前部门及主部门、完整旧/新角色事实、操作者 authority version，以及目标变更前后 permission/menu/Agent/物化 user/dept scope hash；批准时 `PreparedActionService` 重建同一快照，执行时共享 Service 在 role → dept → user 锁内再次比较，任一差异统一 `AI_PREPARED_ACTION_SNAPSHOT_STALE`。**反例**: 只冻结新角色 ID 会漏掉旧角色、目标部门、Role-Agent/menu 或操作者授权漂移；按名称批准后重查会出现展示 A、执行 B；Tool 直接写 `user_roles` 会绕过页面 Policy。**回归**: `tests/modules/ai/test_user_role_assignment_tools.py`、`tests/modules/ai/test_prepared_action_service.py`、`tests/modules/system/test_user_role_assignment_service.py`。

59. **Task 13 角色结果投影必须重新执行委派 Policy** — `user.role_lookup`、`user.update_roles` 与 `user.lookup` 的角色数据使用独立 `delegable_role`、`complete_user_role_assignment`、`user_role_assignment_access` subject 类型；历史读取重新要求 role-auth、目标 user scope、全部冻结角色仍可委派，并对完整集合重新物化授权。lookup 的精确 `matchCount` 必须把超过 `limit` 的全部贡献角色冻结为内部 refs，不能只记录展示行。Agent prompt 对零命中和多命中强制停止并澄清。**反例**: 沿用通用 `role` existence check 会在 role-auth 或 permission/menu/Agent/scope 委派能力撤销后继续回放历史结果；只冻结前 20 行会让未引用角色继续贡献可见旧计数。**回归**: `tests/modules/ai/test_result_projection_service.py`、`tests/modules/ai/test_user_role_assignment_tools.py`、`tests/modules/system/test_user_role_assignment_service.py`。

---

## 10. 实施计划

### Phase 0：文档基线

- [x] 回写 Gateway 默认值、授权层和当前入口。
- [x] 将消息编辑/重新生成标记为 Deferred，并从工具卡当前门禁移除。
- [x] 将用户部门/角色工具提升为 Plan 2。
- [x] 新增本文并在 `AGENTS.md` 固定阅读路由。
- [x] ✅ Phase 0 已完成（2026-08-14）：同步 Gateway SR-22、模块关闭 503 契约、DataScope 多角色并集语义、shared/超管可见性与 Tool-Agent 精确归属、PreparedAction 冻结模型/终态规则、历史结果投影实时重授权、`ai:file:parse` required permission、Provider egress Policy、`docs/AI-SECURITY.md` 和 `docs/AI-DEPLOYMENT.md`；同步用户管理 Plan 2 的 lookup/完整集合/新权限语义，并落盘分阶段实施计划。

### Phase 1：默认开启的权限地基

- [x] ✅ P1-A 已完成（2026-08-14；审查修复 2026-08-15）：`AI_MODULE_ENABLED=false` 时 fresh process 只注册 `/ai`/`/ai/**` 503 guard，覆盖包括 `TRACE`/`CONNECT` 在内的标准 HTTP 方法，启动与 shutdown 不加载 AI router、Service、Provider、Gateway、Registry 或 lifecycle；新增显式 `ai:chat:use` dependency，覆盖 Agent list、chat、conversation、query-cache 和 routing-feedback 用户入口，Provider 管理模型列表固定要求 `ai:provider:list`；confirm/resume/owner operation-log 保持 authentication-only router，并在 owner + tenant 绑定校验后区分 approve/reject、完整/最小状态分支。Postgres PreparedAction 是终态权威来源，即使 Redis pending 已清理也可按当前权限返回最小状态或安全 SSE replay，且绝不重跑 Tool。权限收集统一复用 auth collector；`ai_operation_log` 已持久化 tenant；fresh 初始化显式把 `ai:chat:use` 绑定给 `R_SUPER`，upgrade 数据迁移幂等补给 `R_SUPER` 与已有非 shared Role-Agent 绑定角色。Agent/Tool/data-scope 结果投影、完整 Agent/file seed 和三模型端点拆分仍未完成，不能独立部署。
- [x] P1-A 验证证据：`ruff check .`、`ruff format --check .`、`python scripts/check_ai_tools.py`（19 tools / 12 checks）、AI 模块 918 项测试及全量 `pytest` 1873 项通过；总覆盖率 72.42%，满足 70% 门禁，仅保留 2 条既有 SQLAlchemy transaction warning。审查回归覆盖权限单一来源、禁用菜单兼容语义、无 Redis 终态恢复、operation-log tenant 隔离以及 fresh/upgrade 幂等权限迁移。
- [x] TDD 新增 `ai:chat:use` 并按 §3.4 覆盖当前用户侧 AI API；chat/Agent-admin/Provider models 三端点拆分仍待 P1-B。
- [x] ✅ P1-B 已完成（2026-08-15；审查修复 2026-08-15）：统一 Agent Policy 覆盖列表、显式、粘滞、Supervisor、默认、confirm/resume；移除 R_SUPER/shared Agent 绑定旁路，Gateway、PreparedAction 与 legacy resume 均要求 Tool 精确归属运行时 Agent。统一模型 selector 覆盖 chat、Supervisor、Agent run 与 conversation create/update，拆分 `/ai/chat/models`、`/ai/admin/agents/model-options` 和 Provider 管理 models；显式 falsy `modelId` 不再 fallback，Supervisor 保留 `AI_MODEL_NOT_AVAILABLE` 拒绝语义，legacy approve 的入口撤权与自动禁用均终态收口。Agent 全局配置要求启用 `R_SUPER + ai:agent:edit`，identity 字段混入时整包拒绝。fresh/upgrade 补齐 chat/file/Agent 菜单、R_SUPER 权限、shared 显式绑定及 `ai:enabled_tools` 兼容迁移；仅当前已闭环的 shared 默认启用，三个业务 Agent 等待 Phase 2/3 后再进入发布集合。
- [x] P1-B 验证证据：`ruff check .`、`ruff format --check .`、`python scripts/check_ai_tools.py`（19 tools / 12 checks）、AI 模块 952 项和全量 1911 项测试通过；总覆盖率 72.74%，Alembic current/head 均为单一 `b8e4c7d2a1f0`，仅保留 2 条既有 SQLAlchemy transaction warning。P1-C egress、P1-D lineage/result projection、Web 切换和 Phase 2 委派仍未完成，本中间构建禁止部署。
- [x] ✅ P1-C 已完成（2026-08-15；P1 审查修复 2026-08-15）：删除任意字典 `/ai/provider/test-model`，新增严格 `{modelId: string}` 的已保存对象 `POST /ai/provider/{provider_id}/test`；Provider/Model 保存、管理状态、chat-safe selector、旧 `resolve_model`、test、Supervisor、Agent 与 continuation 均接入统一 egress Policy。OpenAI、Anthropic、DeepSeek/兼容 adapter 共享禁环境代理 client，并按原始精确 origin 隔离底层连接池；最终 URL 校验精确 origin/port、全部 DNS/IP，连接固定且保持 Host/TLS SNI。禁 redirect，限制连接/读取/总超时、并发、重试及响应大小；取消路径归还 permit，压缩成功响应在 SDK 解压前 fail closed，上游 body/异常统一脱敏为稳定错误。upgrade 审计只报告 `EGRESS_POLICY_BLOCKED`，运行时 quarantine 不修改 Provider/Model `enabled`。
- [x] P1-C 验证证据：新增 3 项 P1 transport 回归后，transport 单测 21 项、AI 模块 983 项及全量 1943 项测试通过；`ruff check .`、`ruff format --check .`、19 tools / 12 checks 通过，总覆盖率 73.37%，仅保留 2 条既有 SQLAlchemy warning。开发库审计检查 2 个 Provider/5 个 Model，准确报告 4 个 blocked 对象且零数据改写；这些存量对象在部署方补精确 allowlist 或修改 URL 前保持 quarantine。P1-D、Web endpoint 切换与 Phase 2 仍未完成，本中间构建禁止部署。
- [x] ✅ P1-D 已完成（2026-08-15；全面审查修复 2026-08-15）：`ai_message`、`ai_prepared_action` 与 query-cache v3 冻结 tenant、Agent、完整 Tool 集合、规范化 subject refs/hash、scope hash、resolver version 和传递 projection dependency message IDs；PreparedAction 同时冻结发起轮次 model/provider ID，成功终态由可信 Tool projection 固化实际结果目标。统一 `authorize_result_projection()` 已覆盖 conversation/history、pending presentation、resume/SSE replay、query-cache、owner operation-log 与独立 AI 下载端点，并递归重验跨轮依赖；legacy/incomplete 分别降级 tombstone、最小状态或同面 404。下载 token 绑定 owner/tenant/resource/完整 projection hash、TTL 5 分钟并使用独立派生签名域，API 认证 exact-match `type=access`；tokenized URL 仅进入 UI projection，不进入 LLM data。scope-bound approve 在执行前校验当前 scope hash，resume 在长等待后的最终读取点再次授权，用户导出在文件副作用前预授权并补偿期间撤权产生的文件。reject/最小状态与已持久化 replay 不复验模型；当前无新的 LLM continuation 路径。
- [x] P1-D 验证证据（含全面审查修复）：跨轮 dependency、PreparedAction、query-cache、download、conversation 与 chat 定向回归 104 项、后端全量 1984 项通过；`ruff check .`、`ruff format --check .` 和 `python scripts/check_ai_tools.py`（19 tools / 12 checks）通过，仅保留 2 条既有 SQLAlchemy transaction warning。Alembic 新增 `d4a6e8f1c3b2` 与 `e6b7f9a2d4c1`，开发库 current/head 单一；legacy dependency `NULL` 明确 fail closed。`alembic check` 仍只因本工作包之外既有 comments/index/FK metadata 漂移失败，本次新增列本身无待生成差异；Phase 2 及 Phase 4 E2E 仍未完成，本中间构建禁止部署。
- [x] 实现统一 Agent Policy，覆盖显式、粘滞、Supervisor、默认回退、confirm/resume 和 Tool-Agent 归属。
- [x] 为 assistant/tool message、PreparedAction 和 query-cache 持久化不可变 Agent/Tool/subject refs，实现统一结果投影 Policy，覆盖 resume/conversation/query-cache/owner-log/download 及 legacy fail-closed tombstone。
- [x] 实现 chat/Supervisor/Agent/conversation 共用的 chat model selector、三端点拆分和 Agent 全局配置超管 Policy；显式无效模型不 fallback，新聊天在 Agent 授权后采用其模型偏好。
- [x] P1-D 为 PreparedAction 冻结 model/provider ID，仅在新 LLM continuation 前复验；reject/最小状态回放独立收口、完整回放重新授权但不查模型。
- [x] 收口 Provider test 为已保存 Provider；所有 SDK/adapter 注入共享 hardened transport，在 test/chat/Supervisor/Agent/continuation 全路径落地协议、origin、Provider/Model URL、解析 IP、DNS rebinding、redirect、timeout、响应大小和错误脱敏 egress Policy，并审计/隔离不合规存量配置。
- [x] 移除 shared/超管 Agent 可见性旁路和 shared 跨 Agent 执行豁免；同步 Registry、Gateway executor、PreparedAction 复验、`check_ai_tools.py`，落地显式 Role-Agent + `ai:file:parse`。
- [x] 完成 P1-B fresh/upgrade 独立 seed/migration：补齐 Agent/file/管理权限与 R_SUPER 显式 shared 绑定，保留部署方 Agent/Role-Agent/`ai:enabled_tools` 状态；业务 Agent 待自身阶段闭环后再加入发布集合。
- [x] ✅ Phase 1 Web 已完成（2026-08-15；全面审查修复 2026-08-15）：聊天、Agent 管理和 Provider 管理使用独立模型 endpoint；Provider test 只提交已保存 Provider/Model ID，未保存 Provider 修改时要求先保存，quarantine 显示稳定 `EGRESS_POLICY_BLOCKED`；Agent option 在 detail 后有序加载；无入口、模块关闭、Agent forbidden/不可用、无模型、模型失效及 tombstone 使用稳定状态。首次挂载与 `KeepAlive` 再激活均重取当前安全投影；所有运行时 availability 拒绝统一清除相应旧 list/detail/result/presentation cache，中止 SSE/resume/polling，并通过 list/model/Agent/select generation 拒绝旧 init 响应覆盖新拒绝。非 `R_SUPER` 不显示 Agent 编辑入口。Web `pnpm fmt`、`pnpm typecheck`、`pnpm test`（30 files / 112 tests）和 `pnpm build` 通过，lint 零 error、仅 30 条既有 warning。仓库级 Web 覆盖率仍为 statements 31.84%、branches 26.25%、functions 19.58%、lines 33.30%，必须在 Phase 4 补齐全局 ≥70% 门禁；Phase 1 只形成代码里程碑，不得绕过 Phase 2/4 门禁独立部署。

### Phase 2：用户部门与角色调整

- [x] ✅ P2-A 已完成（2026-08-17；审查修复 2026-08-17）：新增 `system:user:role-auth`，向历史 add/edit/import 任一用户角色 writer 幂等补权且不横向扩张原入口；交付共享 DataScope 并集 resolver、`GrantAuthority`（分离 visible/grantable Agent）、集合 dominance、§5 稳定锁协议，以及旧 API/旧 AI/新 resolver 只读 scope-diff、可信单租户报告和精确 ACK 门禁。审查修复后，soft-disabled Role-Agent 不再贡献隐藏委派能力，维护/审计/同 build 切换与验证由同一 session advisory lock 覆盖。传统 filter、导入预检、AI `DataScopeContext`/lineage 已共用 resolver；本工作包未接入用户/部门/角色业务 writer，未修改 Web，也未执行生产 scope-diff/ACK。
- [x] P2-A 验证证据：授权核心、会话锁与 scope 审计定向测试 27 项通过（包含 PostgreSQL 双连接验证 session lock 跨 commit 持有）；`ruff check .`、`ruff format --check .`、19 tools / 12 checks 通过；后端全量 2010 项测试通过，总覆盖率 73.24%，仅保留 2 条既有 SQLAlchemy transaction warning。数据库 schema 无变化；fresh/sync seed 和受 advisory lock 保护的存量权限迁移路径均有回归测试。
- [x] ✅ P2-B1 已完成（2026-08-17；审查修复 2026-08-17）：拆分用户资料与角色请求契约，旧角色/部门/password 字段 fail closed；`roleIds` 固定为 canonical Snowflake `string[]`，显式 null 不再等同省略。新增页面完整角色替换和最小可委派角色候选 API。页面 create、AI create 与 import 新建统一使用旧/新角色 dominance 和固定 `R_USER` 窄例外；导入用表头叠加 role-auth，锁内复验权限与完整角色集合，冻结 role/dept/user 名称解析并在全局锁后重新发现，任一授权越界整批零业务写入。本工作包无数据库迁移、seed 或 Web 改动。
- [x] P2-B1 验证证据：角色契约/Policy 及相关页面、AI、import 定向回归 126 项通过；`ruff check .`、`ruff format --check .`、19 tools / 12 checks 通过；后端全量 2028 项测试通过，总覆盖率 73.57%，仅保留 2 条既有 SQLAlchemy transaction warning。AI 模块关闭 fresh process 继续不加载 Tool Registry。
- [x] ✅ P2-B2 已完成（2026-08-17；审查修复 2026-08-17）：新增页面 `PUT /system/user/{user_id}/departments` 与严格 `{deptAssignments}` 完整替换契约，API 和 Service 双层要求 `system:user:edit + system:dept:list`。独立 `UserDepartmentAssignmentService` 校验目标用户及旧/新部门完整 scope、启用可分配状态、动态主部门配置、admin/R_SUPER 保护，并以目标完整启用角色分别物化变更前后 permission/menu/Agent/部门/用户授权，任一不受操作者 `GrantAuthority` dominance 即原子拒绝。预读角色、部门结构影响和用户关联后按 role → dept → user 加锁，锁后重载并拒绝关联漂移或新结构依赖。审查修复后，主部门策略绕过 best-effort cache 并锁定配置行，候选部门作用域移除数据库旧主体后按候选事实精确重建；隐藏旧关联删除、admin/R_SUPER 目标保护均有显式回归。本工作包无数据库迁移、seed、AI Tool、import overwrite、部门成员页或 Web 改动。
- [x] P2-B2 验证证据：部门契约、页面事务和共享 Policy 定向回归 35 项，角色兼容合计 48 项，system 模块 539 项全部通过；`ruff check .`、`ruff format --check .`、19 tools / 12 checks 通过；后端全量 2053 项测试通过，总覆盖率 73.81%，仅保留 2 条既有 SQLAlchemy transaction warning。
- [x] ✅ P2-B3 已完成（2026-08-18；审查修复 2026-08-18）：页面 create、AI `user.create`、import create/overwrite 和部门成员页全部接入共享角色/部门 Policy；移除 `DeptService` 的旧无授权关联 writer，运行时 `user_depts` 写入统一收口到 `UserDepartmentAssignmentService`。导入拒绝重复现有目标并为 `DEPT_AND_SUB` 展开完整后代锁；部门成员 GET 使用 user-scoped 最小分页并对隐藏旧成员整体阻断，PUT 对完整旧/新成员逐用户执行主部门、scope、前后授权 dominance 和锁前/锁后完整授权快照比较，以批量物化避免 N+1。Web 新建按 role-auth/dept-list 条件提交显式集合，编辑使用独立 profile/roles/departments API 且不重写未变化关联，并在部分 writer 已提交后显式刷新；部门成员弹窗只有全部分页成功才允许提交完整候选。本工作包无数据库迁移或 seed 变更。
- [x] P2-B3 验证证据（含 CI 隔离修复）：后端 `ruff check .`、`ruff format --check .`、19 tools / 12 checks 与全量 2067 项测试通过，总覆盖率 74.18%，仅保留 30 条 import fixture SQLAlchemy warning 和 2 条既有 transaction warning；Supervisor safety-order 测试显式隔离 Agent/Model 可用性并按本次 trace 断言 routing log，不再依赖 fresh seed 未承诺开放的业务 Agent 或历史日志。Web lint 0 error / 30 条既有 warning、typecheck、31 files / 119 tests 与 production build 全部通过。
- [x] ✅ Task 12 已完成（2026-08-18；审查修复 2026-08-18）：`user.dept_lookup` 使用 `system:dept:list` 与当前部门 scope，支持名称/可见路径及 `1..20` limit，并先筛末级名称候选、再仅为候选构造可见路径；`user.update_dept` 使用严格完整集合 schema、双权限与强制 HITL/dry-run，冻结规范化参数及操作者 authority、目标状态/角色、旧新部门、主部门配置、角色定义和前后物化授权 hash。`user.lookup` 只在部门全集可证明完整时返回可组合集合；dry-run 领域拒绝保留原始错误码并在 confirmation 前终止。批准前重建业务快照，执行时共享部门 Service 在全局锁内再次复验，任一漂移统一 fail closed；Web 确认、错误和结果展示已完成 i18n。本工作包无数据库 migration、权限/menu seed 或新权限码，仅同步内置 Agent prompt 默认值。
- [x] Task 12 验证证据：后端 `ruff check .`、`ruff format --check .`、20 tools / 12 static checks、Task 12/Gateway/共享 Policy 扩展回归 226 项与全量 2091 项测试通过，总覆盖率 74.39%，仅保留 30 条 import fixture 与 2 条既有 transaction warning；Web oxlint、format、typecheck、31 files / 120 tests 与 production build 通过，ESLint 0 error / 30 条既有 warning。
- [x] ✅ Task 13 已完成（2026-08-18；审查修复 2026-08-18）：`user.role_lookup` 使用独立 role-auth 与共享可委派角色 Policy，按 code/name 查询且只返回启用、受 `GrantAuthority` 支配的最小候选；`user.lookup` 仅在目标可见、完整角色全集及其实际物化授权均可证明受支配时返回 `roleAssignmentsComplete=true`。`user.update_roles` 使用严格完整正整数集合、双权限、强制 HITL/dry-run，并只编排共享角色 Service；审批冻结并两次复验目标部门、旧/新角色、角色定义、Role-Agent/menu、操作者 authority 与前后有效授权 hash，漂移统一 fail closed。审查修复后，角色条件数据与精确 match count 使用完整领域 refs，历史读取重新执行 role-auth、目标集合与可委派角色 Policy；零/多命中 prompt 强制澄清，操作者、菜单、主部门和 Role-Agent 真实漂移均有数据库回归。部门和角色审批共用 canonical 授权快照 helper；Web i18n 与静态 inventory 同步完成。本工作包无数据库 migration、权限/menu seed 或新权限码。
- [x] Task 13 验证证据：后端 `ruff check .`、`ruff format --check .`、22 tools / 12 static checks、角色/部门共享 Policy、结果投影与 PreparedAction 定向回归 145 项及全量 2121 项测试通过，总覆盖率 74.59%，仅保留 32 条既有 warning；Web lint 0 error / 30 条既有 warning、typecheck、31 files / 121 tests 与 production build 通过。
- [x] 重构 `/system/dept/{id}/users`：候选最小化且 user-scoped，完整旧/新成员及逐用户部门/授权 Policy、主部门拒绝、全局锁和整批原子失败全部落地。
- [ ] `/ai/role-agent` 普通管理员写入口接入完整 Role Delegation Policy；Phase 1 + Phase 2 安全门禁全部通过后只允许进入集成分支，不允许生产部署。

### Phase 3：部门与角色 Agent

- [ ] 新写 Dept Agent 短 spec，新增 `system:dept:move` 和独立页面 move API，收口传统 Dept 读/更新 API，并完成 scoped lookup/create/update/move、status/结构间接授权影响分析以及 role → dept → user 全局锁协议。
- [ ] 新写 Role Agent 短 spec，在 Phase 2 授权地基上完成 lookup/create/update/menu/Agent 聚合根 Policy、受限委派和全局成员影响分析。
- [ ] AI dept/role delete Tool 保持延期；现有页面单个/批量删除入口改为 `R_SUPER + 原权限`，补引用保护、全局锁、原子批量和无 cascade 授权变更测试。
- [ ] 更新 prompt、i18n、tool result 和 Agent 管理页 inventory。

### Phase 4：发布闭环

- [ ] 完成工具卡 HITL resume/download/tool-only/reload 当前范围 E2E，并覆盖撤权后 reject/最小状态回放、所有历史结果读取面的统一 tombstone、前端缓存清除及删除会话原子终态化。
- [ ] 完成 AI Trace：operation-log 的 `tenant_id` migration/index 与写入/查询隔离已提前完成；仍需 agent/target 字段、Trace list/detail API、`/ai/trace` 菜单页面、消息元数据 DTO allowlist、脱敏测试和浏览器审计 E2E。
- [ ] 使用 §7.1 全部身份执行真实浏览器权限/数据范围 E2E。
- [ ] 新增独立 `e2e:provider` Playwright project；release 环境使用真实 provider 完成三个 Agent smoke，缺凭据失败，不以 route fixture 代替。
- [ ] 后端 `ruff check . && ruff format --check . && pytest --cov=app --cov-report=term-missing --cov-fail-under=70` 及 `python scripts/check_ai_tools.py` 通过。
- [ ] 前端补齐 `test:coverage`、`e2e:provider`（阈值 ≥ 70%），并通过 `pnpm lint && pnpm typecheck && pnpm test:coverage && pnpm build && pnpm e2e`；release 另通过 `pnpm e2e:provider`。
- [ ] 回写所有专项 spec 状态、测试证据和 ship date，将本文状态翻转为已发布。

---

## 11. MVP 完成定义

只有同时满足以下条件才能宣布 AI MVP 闭环：

- 模块默认开启且 endpoint/Agent/Tool/数据四层后端授权在所有选择与恢复路径都有拒绝测试；
- 模块关闭时 `/ai/**` 统一 503；chat model 列表和所有新 LLM run 使用同一 capability selector，PreparedAction 冻结模型，reject/状态回放不被模型状态阻断且终态可恢复，失权回放不返回业务结果；
- fresh/upgrade seed 不覆盖部署方显式配置，shared 无直通，旧 `R_SUPER` 和普通角色绑定符合 §3.3；
- Phase 1 与 Phase 2 作为原子安全集成单元，普通 `/ai/role-agent` 写入具备完整委派上界与全局影响 Policy；整个 MVP 在 Phase 3 writer 收口和 Phase 4 Trace/E2E 完成前未生产部署；
- Agent 全局定义的所有可变字段仅 `R_SUPER + ai:agent:edit` 可写，Role-Agent 受限委派不能绕过该边界；
- user/dept/role 三个 Agent 均有真实可用的读写纵向切片；
- `user.update_dept/update_roles` 的旧/新完整集合、提权、范围和审批漂移均 fail closed；
- 页面 update/create/import 分别使用 `edit/add/import + system:user:role-auth`，无法横向扩张原入口；固定 `R_USER` 例外不可接收任意角色且仍通过 dominance；
- 部门成员页面不泄露 scope 外用户，并对完整成员及逐用户部门/前后授权执行原子 Policy；
- Department 读写 scope、树移动和 status 间接影响、Role 受限委派及全局影响均由页面/API/Tool 共用 Policy；
- Dept/Role 传统单个与批量删除在 AI delete Tool 延期期间仅 `R_SUPER + 原权限` 可用，引用保护阻止 cascade 授权变更；
- 多角色 DataScope 并集的存量扩大项已由防漂移 scope-diff 暴露并经管理员显式确认，维护锁下重跑 hash 一致，传统 API 与 AI 使用同一 resolver；
- 不同角色通过真实浏览器页面完成正向、反向和合法委派验收；
- 至少一个真实 Provider 完成三个 Agent 的模型工具选择 smoke，Provider test/chat/Supervisor/Agent/continuation 与存量配置均通过 SSRF/egress 安全门禁；
- AI Trace API/页面可以按 tenant 脱敏核对成功、拒绝和失败终态，DTO 不返回消息正文或 raw prompt/args；
- HITL reload/resume/download/tool-only 消息投影可以收口；撤权不阻断 reject/最小状态回放，conversation/resume/query-cache/owner-log 全部实时重授权且前端不恢复旧结果，approve 失败和会话删除都不会遗留 pending action；
- 消息 edit/regenerate 保持关闭且不阻塞发布；
- 前后端 lint/typecheck/build/test/确定性 E2E 和覆盖率 ≥ 70% 门禁通过，release 的真实 provider E2E 单独通过，spec 已按真实结果回写。
