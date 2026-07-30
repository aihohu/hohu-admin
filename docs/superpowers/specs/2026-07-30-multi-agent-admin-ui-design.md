# Multi-Agent 管理后台 UI 设计

**Status**: ⚠️ Plan 待实现 | 创建日期：2026-07-30
**关联 spec**：
- [`2026-07-24-multi-agent-supervisor-routing-design.md`](./2026-07-24-multi-agent-supervisor-routing-design.md) §10.1 — 管理后台 gap
- [`2026-07-02-ai-tool-gateway-design.md`](../specs/2026-07-02-ai-tool-gateway-design.md) §4.2 / §4.3 / §5.4 — `ai_agent` / `role_ai_agent` 表定义 + shared Agent 直通机制

**Ship 记录块**：待补（merge 后写）

---

## 1. 背景与目标

### 1.1 背景

Multi-Agent + Supervisor 路由 v4 已于 2026-07-26 ship（后端 PR#7 + 前端 PR#3）。落地后存在运维 gap：

- Agent 运行时配置（`enabled` / `systemPrompt` / `modelPreference` / `dailyQuotaPerUser` / `riskAppetite` / `description` 调优）只能通过 `scripts/seed_ai_agents.py` + 直接 SQL 维护
- Role ↔ Agent 关联（`role_ai_agent` 表）无 UI，绑定操作走 SQL
- 路由反馈数据（`ai_routing_feedback` 表，v4 新增）无消费侧 UI，无法回答"路由准不准"

关联 spec `2026-07-24-multi-agent-supervisor-routing-design.md` §10.1 明确标记 `⚠️ Plan supervisor-routing gap`，本期补齐。

### 1.2 目标

提供 3 块管理 UI：

1. **Agent 管理**（CRUD 中的 Update + Read，无 Create/Delete）
2. **Role-Agent 绑定**（仿 `menu-auth-modal` 模式）
3. **路由反馈分析仪表盘**（KPI + 排行 + 明细表）

### 1.3 非目标

- routing-test 端点（"测试 query → 预测路由"）—— 留 v1.6+
- matrix 热力图 —— 留 v1.6+
- 新增 / 删除非内置 Agent —— 走代码（`@ai_tool`）+ seed 脚本
- 反馈仪表盘导出 CSV —— 留 v1.6+
- Agent 乐观锁（update_time If-Match）—— 留 v1.6+

---

## 2. 范围

### 2.1 In scope

**后端**：
- `app/modules/ai/api/agent.py` 扩展：admin 端点（list / detail / update）
- 新增 `app/modules/ai/api/routing_feedback.py`：summary + list 端点
- 新增 `app/modules/ai/api/role_agent.py`：role-agent 绑定 get + put
- 新增对应 service / schemas（见 §5 文件树）
- `scripts/sync_menus.py` 加 2 个菜单 + 4 个权限码

**前端**：
- `src/views/ai/agent/index.vue` + `agent-operate-drawer.vue`
- `src/views/ai/routing-feedback/index.vue`
- `src/views/system/role/modules/ai-agent-auth-modal.vue`
- 配套 `service/api/*.ts` + `typings/api/*.ts` + i18n

**测试**：后端 pytest（3 个 test 文件，覆盖率 ≥ 70%）+ 前端 vitest（2 个 spec）+ E2E（4 个场景）

### 2.2 Out of scope

见 §1.3。

---

## 3. 术语表

| 术语 | 含义 |
|---|---|
| **Agent** | AI Agent 注册表中的一行（`ai_agent` 表），对应一组 tool 的逻辑分组。`code` 与 `@ai_tool(agent=...)` 装饰器强约束 |
| **内置 Agent** | `is_builtin=True` 的 Agent，开源项目自带 7 个（shared / user_mgmt / role_mgmt / config_mgmt / dept_mgmt / provider_mgmt / job_mgmt），UI 不允许删除 |
| **shared Agent** | 特殊 code，所有用户直通，无需走 Role 绑定 |
| **Role-Agent 绑定** | `role_ai_agent` 表，定义"哪个 Role 能用哪些 Agent"，与 `sys_role_menu` 同构 |
| **路由反馈** | 用户在 chat 界面点"选错 Agent？"按钮后产生的反馈记录（`ai_routing_feedback` 表），feedback ∈ {correct, wrong} |
| **错路由率** | `wrong / (correct + wrong)`，反馈仪表盘核心 KPI |
| **topWrongAgents** | 错路由排行，按 Agent 维度统计 wrong 数 top 10 |

---

## 4. 架构总览

### 4.1 端点矩阵

| 端点 | 方法 | 权限码 | 用途 |
|---|---|---|---|
| `/ai/admin/agents` | GET | `ai:agent:list` | 全量列表（内置 7 行） |
| `/ai/admin/agents/{agentId}` | GET | `ai:agent:list` | 详情（编辑 drawer 回填，含 systemPrompt） |
| `/ai/admin/agents/{agentId}` | PUT | `ai:agent:edit` | 更新（无 add/delete） |
| `/ai/routing-feedback/summary` | GET | `ai:routing-feedback:list` | KPI + Agent 排行（query: `days=7\|30`） |
| `/ai/routing-feedback/list` | GET | `ai:routing-feedback:list` | 明细分页（query: `days`, `current`, `size`, `originalAgent?`, `correctedAgent?`, `feedback=wrong\|all`） |
| `/ai/role-agent/{roleId}` | GET | `system:role:ai-agent-auth` | 该 Role 已绑 Agent 列表 + 全量 Agent 树 |
| `/ai/role-agent/{roleId}` | PUT | `system:role:ai-agent-auth` | 全量覆盖绑定 |

> **设计说明**：admin 端点走 `/ai/admin/agents` 而不是 `/ai/agents`，避免与现有 `GET /ai/agents`（用户视角，仅可见 Agent）混淆。

### 4.2 菜单 + 权限码

**新增菜单**（`scripts/sync_menus.py` 加 seed）：
- `AI > Agent 管理`，path: `/ai/agent`，icon: `carbon:bot`（待定，复用现有图标库）
- `AI > 路由反馈分析`，path: `/ai/routing-feedback`，icon: `carbon:analytics`

**新增按钮权限码**（默认绑 `R_SUPER`，跟现有 `ai:provider:*` 一致）：
- `ai:agent:list`
- `ai:agent:edit`
- `ai:routing-feedback:list`
- `system:role:ai-agent-auth`（命名与 `system:role:menu-auth` 同构）

> **`ai:agent:add` / `ai:agent:delete` 处置**：这两条权限码已由 `scripts/sync_menus.py:1204` / `:1222` seed（源自 `2026-07-02-ai-tool-gateway-design.md` §10.2），本期**保留种子但不挂任何 endpoint / UI 按钮**——决策 #1 砍 Create/Delete 后无对应 API。管理员在权限分配 UI 能看到这两个码，但勾上无效（无 endpoint 兑现）。v1.6+ 上线 Create/Delete 时复用，避免重新 seed + DB 迁移。CLAUDE.md 硬规则 #11 警告"button code 与 endpoint 不一致 silently breaks the gate"——此处是**反向留白**（码存在但 endpoint 故意缺），由决策 #1 显式声明范围保证不误导。

---

## 5. 文件结构

### 5.1 后端文件树（新增标 `+`）

```
hohu-admin/app/modules/ai/
├── api/
│   ├── agent.py                      # 扩展：加 admin GET/PUT 端点
│   ├── routing_feedback.py           # 扩展：加 admin GET summary/list 端点（与现有 POST submit 同文件，共享 router prefix）
│   └── role_agent.py                 # + 新建：role↔agent 绑定 GET/PUT
├── service/
│   ├── agent_admin.py                # + 新建：AgentAdminService
│   ├── routing_feedback_query.py     # + 新建：聚合查询（KPI / 排行 / 明细），与现有 routing_feedback_service.py（POST submit）分离，见决策 #22
│   └── role_agent.py                 # + 新建：RoleAgentService
└── schemas/
    ├── agent_admin.py                # + 新建：AgentAdminListItem / AgentAdminDetailItem / AgentAdminUpdateReq
    ├── routing_feedback.py           # 扩展：加 FeedbackSummary / FeedbackListItem / FeedbackListQuery（与现有 RoutingFeedbackRequest 同文件）
    └── role_agent.py                 # + 新建：RoleAgentBinding / RoleAgentBindReq

hohu-admin/
├── alembic/versions/                 # 无新迁移（所有表已存在）
└── scripts/
    └── sync_menus.py                 # 加 2 菜单 + 4 权限码
```

### 5.2 前端文件树（新增标 `+`）

```
hohu-admin-web/src/
├── views/ai/
│   ├── agent/                        + 新页面
│   │   ├── index.vue
│   │   └── modules/
│   │       └── agent-operate-drawer.vue
│   └── routing-feedback/             + 新页面
│       └── index.vue
├── views/system/role/modules/
│   └── ai-agent-auth-modal.vue       + 仿 menu-auth-modal.vue
├── service/api/
│   └── ai-agent.ts                   + admin + role-agent 端点封装
│   └── ai-routing-feedback.ts        + summary + list 封装
├── typings/api/
│   └── ai-agent.ts                   + Agent / RoleAgent / Feedback 类型
│   └── ai-routing-feedback.ts        +
└── locales/langs/
    ├── en-us.ts                      + 加翻译
    └── zh-cn.ts                      +
```

---

## 6. 端点契约详细

### 6.1 Agent 管理（admin 视角）

**`GET /ai/admin/agents`** — 权限 `ai:agent:list`

响应：`{ code: 200, data: AgentAdminListItem[] }`

> **无 query 参数，无分页**（决策 #23）：内置 Agent 数量稳定在 7 个，新增/删除走代码 + seed 脚本（决策 #1），列表量级在 10 行内。前端 §7.1 的"关键字 + 启用状态筛选"做客户端过滤（computed property 即可），无需后端支持。

```typescript
AgentAdminListItem = {
  agentId: string                  // Snowflake → 字符串
  code: string                     // 只读
  name: string
  description: string
  enabled: boolean
  isBuiltin: boolean
  displayOrder: number
  modelPreference: string | null
  dailyQuotaPerUser: number | null
  riskAppetite: "conservative" | "balanced" | "aggressive"
  createTime: string
  updateTime: string
}
```

> **systemPrompt 不在 list 返回** —— 避免 list payload 过大 + 减少 PII 暴露面（systemPrompt 可能含业务领域知识）。仅 `GET /ai/admin/agents/{id}` detail 端点返回。

**`GET /ai/admin/agents/{agentId}`** — 权限 `ai:agent:list`

响应：`AgentAdminDetailItem`（含 systemPrompt）

```typescript
AgentAdminDetailItem = AgentAdminListItem & {
  systemPrompt: string
}
```

**`PUT /ai/admin/agents/{agentId}`** — 权限 `ai:agent:edit`

请求 body（**只允许这些字段**，其他字段如 `code` / `isBuiltin` 即使传也忽略，不报错）：

```typescript
AgentAdminUpdateReq = {
  name?: string                       // 1-128 字
  description?: string                // 50-200 字（spec §10.1 要求）
  enabled?: boolean
  displayOrder?: number               // ≥ 0
  systemPrompt?: string               // ≤ 32 KB（应用层限制）
  modelPreference?: string | null     // "provider:model" 或 null
  dailyQuotaPerUser?: number | null   // ≥ 1 或 null
  riskAppetite?: "conservative" | "balanced" | "aggressive"
}
```

**字段校验细则**：

- **`description` 长度算法**：按 Python `len(s)` 计 code point，不区分中英文（详见决策 #20）。**当 description 字段被提供时**（partial update 语义），50 ≤ `len(description)` ≤ 200，否则返 `AI_AGENT_DESC_LENGTH_INVALID`；未提供该字段时跳过校验，保持原值不变（PUT 是 partial update，未传字段不动）。
- **`displayOrder` 不强制唯一**：允许重复，重复时按 `agent_id ASC` 二级排序；测试 fixture 用唯一 displayOrder 避免排序 flaky。
- **`dailyQuotaPerUser`**：传 `null` 表示"仅全局 L2 限额"，传正整数 ≥ 1 表示 per-user 上限。传 ≤ 0 返 `AI_AGENT_QUOTA_INVALID`。
- **`modelPreference`**：格式 `provider:model`（如 `openai:gpt-4o`），传 `null` 表示用全局默认。前端下拉候选来自现有 `GET /ai/provider/models`（`app/main.py:216` 注册的 prefix 是 `/ai/provider` 单数，端点定义在 `provider.py:28`，无需新建）。

**错误码**：

| code | 触发 | HTTP |
|---|---|---|
| `AI_AGENT_NOT_FOUND` | agentId 不存在 | 404 |
| `AI_AGENT_DESC_LENGTH_INVALID` | description 不在 50-200 字 | 400 |
| `AI_AGENT_RISK_APPETITE_INVALID` | risk_appetite 非 3 枚举值 | 400 |
| `AI_AGENT_QUOTA_INVALID` | daily_quota_per_user ≤ 0 | 400 |
| `AI_AGENT_SYSTEM_PROMPT_TOO_LARGE` | systemPrompt > 32KB | 400 |
| `AI_AGENT_MODEL_PREFERENCE_INVALID` | modelPreference 非 `provider:model` 格式 | 400 |
| `AI_AGENT_NAME_LENGTH_INVALID` | name 不在 1-128 字 | 400 |
| `AI_AGENT_DISPLAY_ORDER_INVALID` | displayOrder < 0 | 400 |

### 6.2 路由反馈仪表盘

**`GET /ai/routing-feedback/summary?days=7`** — 权限 `ai:routing-feedback:list`

响应：

```typescript
FeedbackSummary = {
  days: 7 | 30
  total: number                  // 该时段所有 feedback（correct + wrong）
  correct: number
  wrong: number
  wrongRate: number              // wrong / total，保留 4 位小数
  topWrongAgents: [              // 按 wrong 数降序 top 10
    {
      agentCode: string
      agentName: string
      wrongCount: number
      topCorrected: {            // 该 Agent 最常被纠正到哪个 Agent（众数）；并列时按 corrected_agent code ASC 取首，见决策 #21
        code: string
        name: string
        count: number
      } | null                   // 防御性 null：CHECK 约束保证 feedback='wrong' 时 corrected_agent 必填，
                                  // 实际不会触发；保留 null 类型应对未来约束放松或 SQL 聚合空集
    }
  ]
}
```

**`GET /ai/routing-feedback/list?days=7&current=1&size=20&feedback=wrong&originalAgent=&correctedAgent=`** — 权限 `ai:routing-feedback:list`

响应：`PageResult<FeedbackListItem>`

```typescript
FeedbackListItem = {
  feedbackId: string
  messageId: string
  userId: string
  userName: string               // join sys_user.username
  originalAgent: string          // code
  originalAgentName: string
  feedback: "correct" | "wrong"
  correctedAgent: string | null
  correctedAgentName: string | null
  traceId: string | null
  createTime: string
}
```

**`feedback` 参数**：默认 `wrong`，前端可切到 `all`。`correct` 不作为独立选项（噪声大）。

### 6.3 Role-Agent 绑定

**`GET /ai/role-agent/{roleId}`** — 权限 `system:role:ai-agent-auth`

响应：

```typescript
RoleAgentBinding = {
  roleId: string
  allAgents: [                   // 全部 Agent（含 builtin + 自建），display_order 升序
    {
      agentId: string
      code: string
      name: string
      description: string
      enabled: boolean           // ai_agent.enabled（全局开关）
      isBuiltin: boolean
      isShared: boolean          // code === SHARED_AGENT_CODE（"shared"），前端据此 disable 行，避免硬编码字符串
    }
  ]
  boundAgentIds: string[]        // 该 role 当前已绑且 role_ai_agent.enabled=True（前端 modal 默认勾选）
}
```

> **空绑定情况**：role 无任何绑定时 `boundAgentIds=[]`，前端正常展示空勾选状态。
>
> **不暴露 `softDisabledAgentIds`**：`role_ai_agent.enabled=False` 软禁用态是 SQL 直改的运维兜底（`role_ai_agent.py:17` 注释），UI 不维护此概念（决策 #19），GET 不返回该段。PUT 全量覆盖时无论旧绑定 enabled 状态如何，未在新列表中的全部 DELETE、在新列表中的全部 INSERT 为 enabled=True（normalize）。SQL 运维要看软禁用态直接查表，不走 admin API。

**`PUT /ai/role-agent/{roleId}`** — 权限 `system:role:ai-agent-auth`

请求 body：

```typescript
RoleAgentBindReq = {
  agentIds: string[]             // 全量覆盖：未在列表里的现有绑定会 delete
}
```

逻辑（事务内）：
1. 校验 roleId 存在
2. 校验每个 agentId 存在 + 非 `shared` Agent（shared 直通，spec §5.4）
3. **计算审计增量**：查现有绑定（含 enabled=False 软禁用行）→ 对比新 `agentIds` 列表 → 得到 `added` / `removed` 集合 → 暂存用于步骤 6 写审计
4. **DELETE FROM role_ai_agent WHERE role_id = :roleId**（一次性删全部，含软禁用行；不做 WHERE id IN (...) 差集删除）
5. **INSERT 新关联**（统一 enabled=True，软禁用态不保留，见决策 #19）
6. 写审计日志（详情见 §8.1）
7. commit

> **实现策略说明**：步骤 4-5 是 "delete-all + insert-all" 全量覆盖，**不**做按 id 增量的 UPDATE/DELETE。步骤 3 的 `added`/`removed` 仅用于审计日志记录"哪些行实际状态变了"，不参与 SQL 写策略——这样避免 "diff-based update" 与 "soft-disabled normalize" 两套语义打架（保留 enabled=False → UI 状态错乱，决策 #19）。代价是 role 绑定大量 Agent 时（如 50 个）单次事务体积略大，但内置 Agent 仅 7 个、上限可控。

> **空数组语义**：`agentIds: []` 等价于解绑该 role 的所有 Agent（DELETE WHERE role_id = :roleId 后无 INSERT），返回 200。

**错误码**：

| code | 触发 | HTTP |
|---|---|---|
| `AI_ROLE_NOT_FOUND` | roleId 不存在（跨模块校验 system.role，沿用 AI 前缀，见决策 #18） | 404 |
| `AI_AGENT_NOT_FOUND` | agentIds 含不存在的 id | 404 |
| `AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN` | agentIds 含 shared Agent | 400 |

---

## 7. 前端页面结构

### 7.1 Agent 管理页（`/ai/agent`）

**布局**（参考 `views/ai/provider/index.vue`）：NDataTable + 顶部搜索表单（code/name 关键字 + 启用状态筛选）。无"新增 / 删除 / 批量"按钮（内置 Agent 只允许编辑）。

**列表列**：code / name / description（截断 + tooltip）/ enabled（NSwitch 只读展示）/ isBuiltin（NTag）/ displayOrder / 操作（编辑）

**编辑 Drawer**（点行"编辑"按钮触发）：

字段：
- `code` 只读展示
- `name` 输入框
- `displayOrder` 数字输入
- `enabled` NSwitch
- `riskAppetite` NSelect（3 选 1）
- `dailyQuotaPerUser` 数字输入（空 = 仅全局 L2）
- `modelPreference` NSelect（候选来自现有 `GET /ai/provider/models` 端点，第一项"用全局默认 = null"；该端点已存在，无需新建）
- `description` NInput textarea，**实时字符计数**（前端用 JS `string.length` 计 code unit，与后端 Python `len()` 在 BMP 字符范围内一致；未到 50 或超 200 时计数器变红 + 保存按钮 disabled，详见决策 #20）
- `systemPrompt` NInput textarea 大号

### 7.2 路由反馈仪表盘（`/ai/routing-feedback`）

**布局**：
1. 顶部时间范围切换（NRadio：近 7 天 / 近 30 天）
2. **KPI 卡 × 4**（NGrid + NStatistic）：总反馈 / 正确反馈 / 错路由 / 错路由率
3. **错路由 Agent 排行**（NDataTable，无分页，top 10）：Agent / 错路由数 / 最常被纠正到
4. **明细表**（NDataTable + 分页）：
   - 筛选：originalAgent NSelect / correctedAgent NSelect
   - 列：时间 / 用户 / 原 Agent → 纠正 Agent / traceId（点击 → 新 tab 打开 `/monitor/operation-log?traceId=xxx`）

切换时间范围或筛选时，summary + list 并行重新拉取。

### 7.3 Role-Agent 绑定 modal

**入口**：Role 列表行加按钮"AI Agent 授权"，与现有"菜单权限"按钮并列（v-permission `'system:role:ai-agent-auth'`）。

**Modal 内容**（仿 `menu-auth-modal.vue`，复用 `useBoolean` + `visible` v-model 模式）：
- 顶部搜索框（按 name/code 过滤）
- `NCheckbox` 组（**不用 NTree**，因为 Agent 是平铺列表无层级）
- shared Agent 行**始终 disabled + 灰显**——前端按 `agent.isShared === true` 判断（不硬编码 `code === 'shared'`，避免 `SHARED_AGENT_CODE` 改名时 modal 静默失效）
- 底部说明文字："shared 直通所有用户，无需勾选"

**提交**：收集勾选的 agentIds（排除 shared）→ `PUT /ai/role-agent/{roleId}` → toast 成功 + 关闭 modal + emit refresh。

---

## 8. 安全 / 审计 / 并发

### 8.1 审计写入

**所有 admin 写操作复用 `AuditLogMiddleware` 自动写入 `sys_operation_log`**（`app/middleware/audit_middleware.py`），**不在 service 层手写**：

| 端点 | middleware 自动捕获 |
|---|---|
| `PUT /ai/admin/agents/{id}` | `module="ai"` / `action="update"` / `path="/ai/admin/agents/{id}"` / `request_params=<脱敏后的整个 body>` / `user_id` + `username`（JWT 自动解析） |
| `PUT /ai/role-agent/{roleId}` | `module="ai"` / `action="update"` / `path="/ai/role-agent/{roleId}"` / `request_params='{"agentIds": ["123","456"]}'` / `user_id` + `username` |

> **设计说明**（决策 #27）：项目已有 `AuditLogMiddleware`（`audit_middleware.py`）拦截所有 PUT/POST/DELETE 请求自动写入 `sys_operation_log`，字段是 `user_id` / `username` / `module` / `action`（HTTP 方法映射：PUT→update）/ `path` / `request_params` / `status_code` / `ip` / `duration`。本期 admin 端点不在 EXCLUDED_PATHS（只 `/ai/chat` / `/ai/confirm` 等被排除），自动被审计。手写 service 层审计会双写冗余、字段定义冲突（spec 早期版本误用 `operator_type` / `operation` 字段名，实际表无此字段）。`request_params` 是脱敏后的整个 request body（含 Agent 全量配置 / Role-Agent 新绑定列表），从全量列表反推增量的工作由运维查表完成（v1.5 不在 UI 展示增量）。

> **`ai_operation_log` 表不用于管理员配置变更审计**：该表（`app/modules/ai/models/operation_log.py`）字段是 `tool_name` / `tool_call_id` / `args_hash` / `risk_level` / `execution_mode` / `status` 等专为 **AI tool 调用过程审计**设计（Gateway Executor 写入，spec §9.1 状态机），与管理员配置变更语义不匹配。

### 8.2 敏感字段

- `systemPrompt` 仅 detail 端点返回（list 不返回）
- `ai_routing_feedback` 关联的 `ai_message.content` **绝不**进 list 响应（只展示 `messageId` + `traceId`，要看原 query 走 `GET /ai/operation-log?traceId=...` 单点跳转）

### 8.3 并发兜底

| 场景 | 兜底 |
|---|---|
| 两个管理员同时改同一 Agent | 后写入覆盖前写入，`update_time` 自动更新，审计日志双行可追溯 |
| 两个管理员同时改同一 Role 的 Agent 绑定 | 全量覆盖语义，后写入胜出；DELETE+INSERT 在事务内，无中间态泄漏 |
| 管理员改 Agent 时用户正在路由 | Agent `enabled` / `description` 查询时读，下一轮路由即生效；本轮进行中不受影响 |

### 8.4 Tenant scope

本模块所有查询不带 `tenant_id`（多租户基建未上线，按项目现状）。未来开多租户时，`ai_agent` / `role_ai_agent` / `ai_routing_feedback` 表加 tenant_id 列，本模块全查询加 scope。详见决策 #16。

---

## 9. 测试策略

### 9.1 后端 pytest（覆盖率 ≥ 70%）

| 文件 | 覆盖 |
|---|---|
| `tests/modules/ai/test_agent_admin.py` | list 全量返回 7 行 / **list 无 query 参数无分页**（决策 #23）/ list 不返回 systemPrompt / detail 含 systemPrompt / detail 不存在返 `AI_AGENT_NOT_FOUND` / update 成功 / **update 仅传 enabled 不传 description → 200 OK，description 保持原值**（partial update 语义）/ update description < 50 字返 400 / update description > 200 字返 400 / update risk_appetite 非枚举返 400 / update daily_quota ≤ 0 返 400 / update model_preference 非 `x:y` 返 400 / **update model_preference 用假 provider `xxx:yyy` 仍 200**（决策 #25，只校验格式不校验存在性）/ update systemPrompt 超 32KB 返 400 / update name 空返 400 / update displayOrder 负返 400 / update code 字段被忽略（不报错）/ update isBuiltin 字段被忽略 / **PUT 后 `sys_operation_log` 多一行，module='ai' / action='update' / path 匹配 / request_params 含 PUT body 全量**（决策 #27，复用 middleware）/ **description 长度算法用 code point 不用字节**（决策 #20）/ **displayOrder 重复时按 agent_id ASC 二级排序** |
| `tests/modules/ai/test_routing_feedback_query.py` | summary 7 天 vs 30 天窗口正确 / wrongRate 计算保留 4 位 / total=0 时 wrongRate=0（不除零）/ topWrongAgents 排序正确（按 wrong 数降序）/ topWrongAgents top 10 截断 / topCorrected 取该 Agent 错路由里 corrected_agent 众数 / **topCorrected 并列时按 corrected_agent code ASC 取首**（决策 #21）/ topCorrected 全部 null 时返 null / list 默认 feedback=wrong / list 支持 feedback=all / list 支持 originalAgent 过滤 / list 支持 correctedAgent 过滤 / list 不返回 message content / list join sys_user.username 正确 / list 用户被删时 userName=""（LEFT JOIN）/ **list originalAgent code 在 ai_agent 表被删/未 seed 时 originalAgentName 返原 code（防御性回退，不 500）** / list 按 feedback_id DESC 显式排序（不依赖 create_time DESC） |
| `tests/modules/ai/test_role_agent.py` | GET 返回 allAgents + boundAgentIds 两段 / GET allAgents 含 shared 且 `isShared=true` / GET allAgents 非 shared 行 `isShared=false` / GET boundAgentIds 不含 shared / PUT 全量覆盖（先 DELETE 后 INSERT）/ PUT 含不存在的 agentId 返 404 / **PUT role 不存在返 `AI_ROLE_NOT_FOUND` 校验 AI 前缀**（决策 #18）/ PUT 含 shared Agent 返 `AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN` / PUT 重复 agentIds 去重 / PUT 空数组等于解绑全部 / **PUT normalize 软禁用：原 enabled=False 行未在新列表中则 DELETE，在新列表中则覆盖为 enabled=True**（决策 #19）/ **PUT 后 `sys_operation_log` 多一行，module='ai' / action='update' / path=`/ai/role-agent/{roleId}` / request_params 含 agentIds 全量列表**（决策 #10 + #27，复用 middleware，不手写增量）/ 并发 PUT 后写入胜出（事务隔离）/ **端点 URL 走 `/ai/role-agent/{roleId}` 而非 `/system/role/...`**（决策 #17） |

### 9.2 前端 vitest

| 文件 | 覆盖 |
|---|---|
| `views/ai/agent/__tests__/agent-operate-drawer.spec.ts` | description 字符计数实时变红 / 50 字以下保存禁用 / 200 字以上保存禁用 / model preference 下拉从 `GET /ai/provider/models` 加载 / 提交调用 `updateAgentAdmin` |
| `views/ai/routing-feedback/__tests__/index.spec.ts` | 时间范围切换 7↔30 天并行触发 summary + list 拉取（不串行）/ KPI 卡 wrongRate 保留 4 位小数展示 / topWrongAgents 按 wrong 数降序渲染 / 明细表 traceId 点击调用 `window.open` 新 tab 跳 `/monitor/operation-log?traceId=xxx` / originalAgent + correctedAgent 筛选触发 list 重新拉取（summary 不重拉） |
| `views/system/role/modules/__tests__/ai-agent-auth-modal.spec.ts` | shared Agent 行 disabled / **shared 行识别走 `isShared` 标志，不依赖 `code === 'shared'` 字符串匹配** / 勾选状态从 `boundAgentIds` 初始化 / 提交 body 排除 shared / 提交成功后 modal 关闭 + emit refresh |

### 9.3 E2E（Playwright）

| 场景 | 步骤 |
|---|---|
| 管理员编辑 Agent description | 登录 R_SUPER → /ai/agent → 点 user_mgmt 编辑 → 改 description → 保存 → toast 成功 → 列表刷新显示新 description |
| 管理员切换 Agent enabled | 编辑 shared → 切换 enabled → 保存 → 重新打开编辑确认状态保持 |
| 管理员绑定 Role → Agent | /system/role → editor 行"AI Agent 授权" → 勾 user_mgmt + config_mgmt → 保存 → 用 editor 角色用户登录 → /ai/chat 看到 user_mgmt + config_mgmt + shared |
| 反馈仪表盘 | 预置 routing_feedback 数据 → /ai/routing-feedback → 切 7/30 天 → KPI 变 → 点 traceId → 新 tab 跳 operation-log |

---

## 10. 决策记录

1. **本期范围：CRUD 只允许 Update，不允许 Create/Delete** — `is_builtin=True` 的 7 个内置 Agent 是项目自带，UI 增删会让 `code` 与 `@ai_tool(agent=...)` 装饰器失去强约束（启动时 ToolRegistry 校验失败）。新增 Agent 必须走代码 + seed 脚本的开发流程。**反例**: UI 允许创建非内置 Agent → 用户填 `crm_mgmt` 但代码里没 `@ai_tool(agent="crm_mgmt")`，LLM 路由到它会返回空 tool 集合 → 业务故障。**回归**: 范围外不测，由 `is_builtin` 字段语义保证。**关联**: `ai:agent:add` / `ai:agent:delete` 权限码已在 `scripts/sync_menus.py:1204` / `:1222` seed，本期保留种子但不挂 endpoint（见 §4.2 处置说明），v1.6+ 复用。

2. **admin 端点走 `/ai/admin/agents` 而非 `/ai/agents/{id}` 扩展** — 现有 `GET /ai/agents` 是用户视角（仅返回可见 Agent，按 display_order 升序，只返 5 字段），admin 视角返回全字段全量列表，语义不同源。**反例**: 同一路径混用 user/admin 视角 → 权限校验复杂、Schema 演化耦合。**回归**: `test_agent_admin.py::test_admin_endpoint_separate_from_user_endpoint`。

3. **description 强制 50-200 字** — spec `2026-07-24-multi-agent-supervisor-routing-design.md` §10.1 明确要求。LLM-only 路由（v4 决策 18 砍规则阶段）准确率完全由 description 决定：过短导致边界模糊（多个 Agent 都"匹配"），过长导致 LLM 注意力分散。**反例**: 不限长度 → 部署方填 "用户管理"4 字 → 路由器无法区分 user_mgmt 和 dept_mgmt 的边界。**回归**: `test_agent_admin.py::test_description_length_boundary`。

4. **`system:role:ai-agent-auth` 命名（参考 `system:role:menu-auth`，建立 `system:role:<X>-auth` 命名族）** — 参考项目现有唯一同构权限码 `system:role:menu-auth`（被操作实体是 role，在 role 列表行点按钮触发），未来 role 相关授权统一走 `<被操作实体所属模块>:<被操作实体>:<授权对象>-auth` 模式。**反例**: 用 `ai:role-agent:auth` → 前缀换成 ai 但实际操作的实体是 role，与现有 `system:role:menu-auth` 不一致；前端 v-permission 字符串也跟 role 模块的兄弟按钮（菜单授权）分组散开。**回归**: `scripts/sync_menus.py` 静态校验权限码命名。

5. **`systemPrompt` 不在 list 返回** — systemPrompt 可能含业务领域知识、客户内部术语、甚至脱敏后的真实业务字段名；list 端点用于表格展示，不需要这字段，返回会增加 PII 暴露面 + list payload 过大。**反例**: list 返回 systemPrompt → 表格不展示但仍走网络 → 抓包可见。**回归**: `test_agent_admin.py::test_list_excludes_system_prompt`。

6. **路由反馈 list 默认 `feedback=wrong`，不支持 `feedback=correct` 单独过滤** — `correct` 反馈是噪声（默认 null + 不强制点），列表 95% correct 行难找错路由样本。需要看 correct 数据时切 `feedback=all`。**反例**: 默认 all → 错路由样本被淹没。**回归**: `test_routing_feedback_query.py::test_default_filter_wrong_only`。

7. **`traceId` 作为跳转 operation_log 的关联键**（不直接展示原 message 内容） — `ai_message.content` 含 PII（用户原始 query 可能含手机号 / 身份证号），列表层暴露不合规。管理员要看原 query 走 `/monitor/operation-log?traceId=...` 单点跳转。**反例**: 直接 join ai_message 暴露 content → 审计层泄漏 PII。**回归**: `test_routing_feedback_query.py::test_no_message_content_leak`。

8. **Role-Agent 绑定全量覆盖语义（不是增量 add/remove）** — 跟 `PUT /system/role/{id}/menu`（menu-auth）一致。前端勾选树一次性 PUT。**反例**: 增量接口 → 并发勾选时状态错乱、回滚难、客户端需自己维护 diff。**回归**: `test_role_agent.py::test_full_replace_semantics`。

9. **禁止绑定 shared Agent** — spec §5.4：shared 直通所有用户（`SHARED_AGENT_CODE = "shared"`），绑它无意义且会让前端 modal 展示混乱（"已绑"但实际权限层忽略）。后端硬拦 + 前端 disabled。**反例**: 允许绑 → UI 显示"已绑"但运行时忽略 → 管理员困惑。**回归**: `test_role_agent.py::test_shared_binding_rejected`。

10. **Role-Agent 绑定审计：复用 middleware 自动审计（全量 request_params），不在 service 手写增量** — 原计划在 service 手写 `{added, removed}` 增量日志，修订后改为完全依赖 `AuditLogMiddleware` 自动捕获 PUT body（含全量 `agentIds` 列表）写入 `sys_operation_log.request_params`。**理由**: 项目已有统一审计中间件（`audit_middleware.py`），service 层手写会双写冗余 + 字段定义冲突（早期 spec 误用 `operator_type` / `operation` 字段，实际表无此字段）。运维要反推增量时对比相邻两行 `request_params` 全量列表即可——v1.5 内置 Agent 仅 7 个，反推成本可控。**反例**: service 手写 added/removed → 与 middleware 写入冲突 / 字段命名偏离表 schema / 后续每加一个 admin 端点都要重复实现一遍。**回归**: `test_role_agent.py::test_put_triggers_audit_middleware`（验证 PUT 后 `sys_operation_log` 多一行，`module='ai'` + `action='update'` + `path='/ai/role-agent/{roleId}'` + `request_params` 含 agentIds 全量）。

11. **首期不做乐观锁（update_time If-Match）** — YAGNI。两个管理员同时改同一 Agent 概率极低，且改的字段往往不同（一个改 description 一个改 systemPrompt），后写入覆盖前写入的影响可通过审计日志追溯。**反例**: 加乐观锁 → 前端要处理 409 冲突合并 UI、增加复杂度，收益不抵成本。**Plan v1.6+ gap**：如真出现并发覆盖事故再加。**回归**: 范围外不测。

12. **Role-Agent 绑定 modal 用 NCheckbox 组，不用 NTree** — Agent 是平铺列表，没有 menu 那种目录层级，Tree 是过度设计；Checkbox 视觉更紧凑、交互更直接。**反例**: 强行套 NTree → 单层树看起来像列表但 cascade/indeterminate 逻辑增加复杂度。**回归**: `ai-agent-auth-modal.spec.ts::test_no_tree_structure`。

13. **shared Agent 在 Role-Agent modal 中 disabled 灰显（不过滤掉）** — 让管理员看见"shared 是直通的，无法配置"，避免"为啥没有 shared？"的反复刷列表怀疑 bug。**反例**: 从列表过滤掉 → 管理员反复刷新怀疑 bug。**回归**: `ai-agent-auth-modal.spec.ts::test_shared_visible_but_disabled`。

14. **routing-test 端点不在本期** — 反馈仪表盘（`ai_routing_feedback` 表）已在收集真实用户数据，比人工测试更真实；routing-test 调一次 LLM 会扣 quota + 写 `ai_routing_log`，污染审计数据，需要 `is_test=true` 打标隔离，复杂度不抵收益。**反例**: 强上 routing-test → 审计数据被测试调用污染、前端面板需 mock LLM 测试用例、用户实际场景下 rarely 改 description。**Plan v1.6+ gap**：等反馈仪表盘数据丰富后评估。**回归**: 范围外不测。

15. **list 端点显式 `ORDER BY feedback_id DESC`**（不依赖 create_time DESC） — 遵循 `CLAUDE.md` 跨项目硬规则 #7：测试不依赖 `created_at DESC` 排序，用显式 version/id。feedback_id 是 Snowflake 单调递增，作为排序键稳定且语义清晰。**反例**: `ORDER BY create_time DESC` → 测试 fixture 同秒插入时排序不确定。**回归**: `test_routing_feedback_query.py::test_explicit_order_by_feedback_id`。

16. **Tenant scope 豁免（本期不带 tenant_id）** — `ai_agent` / `role_ai_agent` / `ai_routing_feedback` 表当前均无 `tenant_id` 列，本模块所有查询不强制 scope。**理由**: 多租户基建整体未上线（CLAUDE.md 硬规则 #10 是面向未来的护栏；目前全项目此规则统一豁免，新增表才默认带 tenant_id 列），单点引入隔离会破坏现状一致性。**反例**: 单为本期端点加 tenant_id 过滤 → 现有表无此列直接报 SQL 错误；或只为本期 3 张表加 tenant_id 列 → 全项目其他模块仍无，数据迁移成本爆炸。**Plan v2 gap**: 多租户基建上线时，统一在 ai 模块所有表加 tenant_id 列 + 本模块所有查询补 scope，需要数据回填（default tenant_id=0）。**回归**: 范围外不测，由 §8.4 显式声明约束。

17. **Role-Agent 绑定 URL 走 `/ai/role-agent/{roleId}` 而非 `/system/role/{roleId}/ai-agent`** — 把端点挂在 ai 模块下，与 `ai_agent` / `role_ai_agent` 表的所有权一致，service 层（`RoleAgentService`）也归 ai 模块。**理由**: 虽然"被操作实体语义上是 role"（决策 #4），但实际数据写入的是 ai 模块的关联表（`role_ai_agent`），业务逻辑（shared Agent 拦截、Agent 存在性校验）全在 ai 模块。挂在 `/system/role/` 会让 system 模块依赖 ai 模块的 service，违反模块边界。现有 `system:role:menu-auth` 的端点 `/system/role/{id}/menu` 走 system 是因为 menu 表本身归 system；此处表归 ai，端点也归 ai。**反例**: 走 `/system/role/{roleId}/ai-agent` → system.api.role.py 文件膨胀 + system → ai 跨模块依赖。**回归**: `test_role_agent.py::test_endpoint_under_ai_module`。

18. **跨模块校验 role 不存在抛 `AI_ROLE_NOT_FOUND`（沿用 AI 前缀）** — Role 实体本属于 system 模块，但本端点 URL 在 `/ai/role-agent/{roleId}` 下（决策 #17），错误码沿用 `AI_*` 前缀保持模块 error_code 命名一致性。**理由**: 前端 errorCode i18n 按模块前缀分组（`errorCode.AI_*`），混用 `SYSTEM_ROLE_NOT_FOUND` 会让前端需要在 ai 端点的 catch 块里同时处理两个模块的错误码字典。错误码命名跟随端点 URL 归属，不跟随实体归属。**反例**: 抛 `SYSTEM_ROLE_NOT_FOUND` → 前端 ai 模块的错误处理需要 import system 模块的 i18n 字典 → 耦合。**回归**: `test_role_agent.py::test_role_not_found_error_code_prefix`。

19. **`role_ai_agent.enabled=False` 软禁用绑定：PUT 全量覆盖 normalize 到 enabled=True，GET 不暴露该段** — PUT 全量覆盖时，未在 `agentIds` 列表里的现有绑定（含软禁用）全部 DELETE，列表里的全部 INSERT 为 enabled=True。GET 只返回 `boundAgentIds`（enabled=True），不返回软禁用段。**理由**: UI 不维护"软禁用"概念（避免 modal 里出现三种状态：未绑 / 已绑启用 / 已绑禁用，UX 复杂度爆炸）；软禁用是 SQL 直改的运维兜底态（`role_ai_agent.py:17` 注释明确"软禁用 = 保留绑定关系，临时关闭"），管理员走 UI 重新保存等于"重置为标准态"；GET 暴露软禁用段无消费者（前端不展示），SQL 运维要看软禁用态直接查表。**反例**: PUT 保留 enabled=False 状态 → 管理员看 modal 勾选了某 Agent 但实际"软禁用"未生效，UI 与运行时行为脱节；GET 返回 `softDisabledAgentIds` 但前端不用 → API 表面积冗余。**回归**: `test_role_agent.py::test_put_normalizes_soft_disabled_to_enabled` + `test_role_agent.py::test_get_excludes_soft_disabled_segment`。

20. **`description` "50-200 字"按 Python `len()` 计 code point** — 中英文统一按字符（code point）计，不区分 CJK / Latin，不按字节。50 ≤ `len(description)` ≤ 200。**理由**: 测试可重现、前后端算法一致（前端 JS `string.length` 也是 UTF-16 code unit，对 BMP 内字符与 Python `len()` 结果一致；emoji 等代理对长度计算差异在 description 场景可忽略）；按字节会让中文 3 倍权重，对中文用户不公平。**反例**: 按 UTF-8 字节 → 中文 description 200 字 = 600 字节，触发 200 上限实际只允许 ~66 个汉字；按 CJK 字符单独计 → 前后端实现分歧大、测试需引入 unicode 数据库。**回归**: `test_agent_admin.py::test_description_length_algorithm_uses_code_points`。

21. **`topCorrected` 众数并列时按 `corrected_agent` code 字典序升序取首** — 当某 Agent 的错路由记录里，多个 `corrected_agent` 计数相同（众数并列），按 `corrected_agent` ASC 取第一个。**理由**: 测试需要确定性结果，否则 SQL `MAX()` 或 `LIMIT 1` 在并列时行为依赖存储引擎 / 索引顺序，结果不稳定。**反例**: 不指定 tie-breaker → 同一份数据在不同 PG 版本 / 不同统计周期下 topCorrected 跳变，仪表盘 KPI 闪烁。**回归**: `test_routing_feedback_query.py::test_top_corrected_tie_breaker`。

22. **`routing_feedback` 文件命名分离：POST submit 走 `routing_feedback_service.py`，GET 聚合查询走 `routing_feedback_query.py`** — 现有 `api/routing_feedback.py` + `service/routing_feedback_service.py` 实现 POST 提交反馈端点（用户视角，已在 supervisor-routing v4 ship）；本期 GET summary/list 端点（admin 视角）service 新建 `routing_feedback_query.py`，API 端点扩进现有 `api/routing_feedback.py` 同文件（路由组共享 prefix `/ai/routing-feedback`）。**理由**: API 同文件可复用 router prefix + 权限装饰器；service 分离是因为 submit（append-only 写）和 query（复杂聚合 SQL）职责正交，混在同一个 service 会让单文件超 500 行且难测。**反例**: service 也合并 → 一个 service 同时负责 append-only 写 + 复杂聚合查询，难测、难维护。**回归**: 文件存在性由实施期 lint 保证，service 命名由 `app/modules/ai/service/__init__.py` 显式 export 校验。

23. **`GET /ai/admin/agents` 无 query 参数、无分页** — 内置 Agent 数量稳定在 7 个（决策 #1 砍 Create/Delete，新增走代码 + seed），列表量级 < 10 行，分页 / 筛选纯属过度设计。前端 §7.1 的"关键字 + 启用状态筛选"用客户端 computed property 实现。**反例**: 加 query 参数 → 后端为 7 行数据写分页 + 过滤逻辑、前端要管 `current` / `size` / `keyword` 状态，复杂度收益完全不匹配。**回归**: `test_agent_admin.py::test_list_returns_all_agents_without_query_params`。

24. **`scripts/seed_ai_agents.py` 7 个内置 Agent description 全部满足 50-200 字基线** — 已核对：shared / user_mgmt / role_mgmt / config_mgmt / dept_mgmt / provider_mgmt / job_mgmt 的 seed description 字符长度均落在 80-150 字之间（`scripts/seed_ai_agents.py:30-101`），编辑页打开任意内置 Agent 时原值合规。partial update 语义（决策 #3 / #20）保证未传 description 字段时不触发 50-200 校验，原值保留——即使个别未来 seed 描述短于 50 字，管理员只编辑其他字段（如 enabled / systemPrompt）也能保存。**反例**: 没有 seed 基线声明 → 实施期发现 seed 短于 50 字时 review 卡壳、回滚决策 #3 浪费 churn。**回归**: 实施期 fixture 校验 `seed_ai_agents.py` 各 description 长度（防回归）。

25. **`model_preference` 校验只看格式（`provider:model`），不校验 provider / model 存在性** — 后端用正则 `^[a-z0-9_-]+:[a-z0-9_-]+$` 校验格式，不查 `ai_provider` / `ai_model` 表存在性；前端下拉候选来自 `GET /ai/provider/models`（已启用模型），UI 引导用户从候选里选，自然不会写脏数据。**理由**: 存在性校验要 join 两张表、且 provider 启用/禁用状态会动态变（今天存在明天被禁用），校验时通过不代表运行时通过——把存在性留给运行时（ChatAgent 创建 model 时 fail-fast 报业务错误），后端 PUT 只保证格式。**反例**: PUT 时校验存在性 → provider 禁用后管理员无法保存旧配置、UI 与运行时状态耦合。**回归**: `test_agent_admin.py::test_model_preference_format_only_no_existence_check`（用假 provider `xxx:yyy` 应保存成功，运行时再由 ChatAgent 报错）。

26. **`topWrongAgents` 聚合查询不补索引，v1.5 评估数据量再决定** — `ai_routing_feedback` 当前索引：单列 `message_id` / `user_id` / `trace_id` / `create_time`（`routing_feedback.py:38-45`）。summary 端点 7/30 天窗口的 `GROUP BY original_agent` + `topCorrected` 众数子查询，数据量小时（< 1 万行 / 30 天）走 seq scan + hash aggregate 可接受（< 100ms）。**理由**: 现在反馈数据稀疏（admin tool 用户量 < 100，wrong 反馈每日个位数），补 `(feedback, create_time)` 或 `(original_agent, feedback, create_time)` 复合索引属于过早优化；数据量真上来时（§14 v1.6+ 路由准确率 SLO 触发条件：> 1000 次/日）统一评估。**反例**: 现在补索引 → 增加写入路径开销、占空间、收益不显著。**Plan v1.6+ gap**: 数据量上来后评估补复合索引 + 物化视图加速 summary。**回归**: 范围外不测。

27. **审计复用 `AuditLogMiddleware`，不在 service 手写**（修订 §8.1 + 决策 #10） — 项目已有 `app/middleware/audit_middleware.py` 拦截所有 PUT/POST/DELETE 请求（EXCLUDED_PATHS 仅排除 `/ai/chat` / `/ai/confirm` 等）自动写入 `sys_operation_log`，字段是 `user_id` / `username` / `module`（URL 第一段）/ `action`（METHOD_ACTION_MAP：PUT→update）/ `path` / `request_params`（脱敏后整个 body）。本期 admin 端点（`/ai/admin/agents/{id}` PUT、`/ai/role-agent/{roleId}` PUT）自动被审计，无需 service 手写。**理由**: 复用单一审计入口避免字段命名漂移（早期 spec 误用 `operator_type` / `operation`，实际表无此字段）；`request_params` 全量列表可由运维反推增量（v1.5 不在 UI 展示）。`ai_operation_log` 表是 AI tool 调用专用（`tool_name` / `tool_call_id` / `args_hash` 等字段），与管理员配置变更语义不匹配，**不复用**。**反例**: service 手写 → 双写冗余、字段命名偏离 schema、每加一个 admin 端点都要重复实现。**回归**: `test_role_agent.py::test_put_triggers_audit_middleware` + `test_agent_admin.py::test_put_triggers_audit_middleware`。


---

## 11. 实施步骤（粗粒度）

详细 task 拆分见 plan 文档（writing-plans skill 生成）。粗粒度顺序：

> **依赖关系**：步骤 5（菜单 / 权限码 seed）必须在步骤 7（前端 Agent 页）/ 8（Role-Agent modal）之前完成，否则前端 `v-permission` 字符串找不到对应权限码、按钮 silently 不显示（CLAUDE.md 硬规则 #11）。步骤 1-4（后端）与步骤 6-10（前端）可并行，但步骤 11（前端测试）依赖 7-9 完成。步骤 12（E2E）依赖所有功能 + 步骤 5 完成。

1. **后端 schemas** — `agent_admin.py` / `routing_feedback.py` / `role_agent.py`
2. **后端 service** — `AgentAdminService` / `RoutingFeedbackQueryService` / `RoleAgentService`（含审计写入）
3. **后端 api** — 扩展 `agent.py` + `routing_feedback.py`、新建 `role_agent.py`（见 §5.1 文件树）
4. **后端测试** — 3 个 test 文件，跑通 + lint + 覆盖率 ≥ 70%
5. **菜单 / 权限码 seed** — `scripts/sync_menus.py` 加 2 菜单 + 4 权限码
6. **前端 types + api 封装** — `typings/api/ai-agent.ts` + `service/api/ai-agent.ts` 等
7. **前端 Agent 管理页** — `views/ai/agent/index.vue` + drawer
8. **前端 Role-Agent modal** — `views/system/role/modules/ai-agent-auth-modal.vue` + Role 列表加按钮
9. **前端反馈仪表盘** — `views/ai/routing-feedback/index.vue`
10. **前端 i18n** — `zh-cn.ts` / `en-us.ts`
11. **前端测试** — vitest 3 个 spec（Agent drawer / 反馈仪表盘 / Role-Agent modal）
12. **E2E** — Playwright 4 场景
13. **回写 spec** — Status 改 `✅ Plan 已完成（YYYY-MM-DD）` + 加 Ship 记录块

---

## 12. 参考借鉴

| 来源 | 用途 |
|---|---|
| [`2026-07-24-multi-agent-supervisor-routing-design.md`](./2026-07-24-multi-agent-supervisor-routing-design.md) §10.1 | gap 来源 / 决策依据 |
| [`2026-07-02-ai-tool-gateway-design.md`](../specs/2026-07-02-ai-tool-gateway-design.md) §4.2 / §4.3 / §5.4 | `ai_agent` / `role_ai_agent` 表定义 + shared 直通机制 |
| `hohu-admin/app/modules/ai/api/agent.py` | 现有 `GET /ai/agents`（用户视角），admin 端点参考其分层 |
| `hohu-admin/app/modules/ai/api/routing_feedback.py` | 现有 POST submit 端点（用户视角），本期 GET summary/list 扩展进同文件（决策 #22） |
| `hohu-admin/app/modules/ai/service/routing_feedback_service.py` | 现有 POST submit service（append-only 写），本期 GET 聚合查询走 `routing_feedback_query.py` 分离（决策 #22） |
| `hohu-admin/app/modules/ai/models/agent.py` | `AiAgent` ORM，admin 端点直接读 |
| `hohu-admin/app/modules/ai/models/routing_feedback.py` | `AiRoutingFeedback` ORM，反馈仪表盘数据源 |
| `hohu-admin/app/modules/ai/models/role_ai_agent.py` | `RoleAiAgent` ORM，绑定端点直接读写（含 `enabled` 软禁用态，决策 #19） |
| `hohu-admin/app/modules/system/api/role.py:97,168` | `system:role:menu-auth` 权限码用法 |
| `hohu-admin/app/modules/ai/api/provider.py:28` + `app/main.py:216` | 现有 `GET /ai/provider/models` 端点（prefix `/ai/provider` 单数；modelPreference 下拉候选来源，无需新建） |
| `hohu-admin-web/src/views/system/role/modules/menu-auth-modal.vue` | Role 关联实体授权 modal 样板（复用 useBoolean + visible 模式） |
| `hohu-admin-web/src/views/ai/provider/index.vue` + `provider-operate-drawer.vue` | Agent 管理页 list + drawer 样板 |
| `hohu-admin/scripts/sync_menus.py:380` | `system:role:menu-auth` seed 写法 |
| `hohu-admin/scripts/sync_menus.py:1204,1222` | `ai:agent:add` / `ai:agent:delete` 已 seed 位置（决策 #1 处置依据） |

---

## 13. Plan 状态块

⚠️ Plan v1.5+ gap：Multi-Agent 管理后台 UI（来自 supervisor-routing spec §10.1） — 本 spec 实施

### Phase 1：v1.5 首期实现（本 spec 范围）

- [ ] 后端 schemas：`agent_admin.py` 新建 / `routing_feedback.py` 扩展 / `role_agent.py` 新建
- [ ] 后端 service：`AgentAdminService` / `RoutingFeedbackQueryService` / `RoleAgentService`（含审计写入）
- [ ] 后端 api：扩展 `agent.py` + `routing_feedback.py`，新建 `role_agent.py`
- [ ] 后端测试：3 个 test 文件 ≥ 70% 覆盖率（含决策 #16-#27 回归）
- [ ] 菜单 + 权限码 seed：`scripts/sync_menus.py` 加 2 菜单 + 4 权限码
- [ ] 前端 types + api：`ai-agent.ts` + `ai-routing-feedback.ts`
- [ ] 前端 Agent 管理页：`views/ai/agent/{index.vue,modules/agent-operate-drawer.vue}`
- [ ] 前端 Role-Agent modal：`views/system/role/modules/ai-agent-auth-modal.vue` + Role 列表加按钮
- [ ] 前端反馈仪表盘：`views/ai/routing-feedback/index.vue`
- [ ] 前端 i18n：`zh-cn.ts` / `en-us.ts` 同步更新
- [ ] 前端测试：3 个 vitest spec（Agent drawer / 反馈仪表盘 / Role-Agent modal）
- [ ] E2E：Playwright 4 场景
- [ ] 回写 spec：Status 改 `✅ Plan 已完成（YYYY-MM-DD）` + 加 Ship 记录块

---

## 14. 未来工作（v1.6+）

- **routing-test 端点** — Agent 编辑 Drawer 内联"测试 query → 预测路由"面板，调 `/ai/agents/routing-test`。需考虑：`is_test=true` 打标隔离审计、LLM mock 测试、测试 query 预设集
- **错路由 matrix 热力图** — 行=originalAgent，列=correctedAgent，单元格颜色深度=计数，NDataTable 自定义染色
- **新增 / 删除非内置 Agent** — UI 允许 code 字段编辑（脱钩 `@ai_tool` 装饰器，纯运行时分组），需重新评估 ToolRegistry 启动校验逻辑
- **反馈仪表盘导出 CSV** — 大客户审计场景
- **Agent 乐观锁** — `update_time` + `If-Match` 头，409 冲突时前端合并 UI
- **traceId 跳转带 originalAgent 过滤** — 跳 operation_log 时自动加 `?originalAgent=xxx` 减少手动筛选
