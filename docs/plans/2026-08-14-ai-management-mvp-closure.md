# AI 管理能力 MVP 收口实施计划

> 状态：Phase 1 / P1-C 已完成；P1-D 与 Web 待开发，中间构建禁止部署
> 日期：2026-08-14
> 唯一需求基线：[`../specs/2026-08-14-ai-management-mvp-closure.md`](../specs/2026-08-14-ai-management-mvp-closure.md)
> 影响项目：`hohu-admin`、`hohu-admin-web`

## 1. 执行原则

1. 本计划只拆解唯一需求基线，不新增产品范围；发生冲突时以基线 spec 为准。
2. 每个工作包执行“失败测试 → 最小实现 → 定向测试 → 全量门禁 → 回写 spec”。
3. Phase 1 与 Phase 2 是一个原子安全集成单元，可以分提交开发，但不得单独部署或承接生产流量。
4. Phase 3 收口传统 writer/destructive 旁路，Phase 4 完成 Trace、真实浏览器和真实 Provider 验收；Phase 4 前禁止生产发布。
5. Service 不提交事务；API 层统一 commit。全部授权 writer 按 `role → dept → user`、同类主键升序加锁并在锁后重读、重算、重验。
6. fresh install 与 upgrade 使用独立数据迁移语义；upgrade 不覆盖部署方的模块开关、Agent enabled、Role-Agent 绑定或 `ai:enabled_tools`。

## 2. 当前基线

- Alembic 当前单一 head：`b8e4c7d2a1f0`。
- `AI_MODULE_ENABLED` 默认值为 `true`；P1-A 已实现关闭态 `/ai`/`/ai/**` 统一 503 且 AI 业务依赖不 import/初始化。
- P1-A 已完成用户入口和分支权限；P1-B 已完成统一 Agent/Tool Policy、三模型端点、当前 LLM run selector、Agent 全局编辑边界及阶段安全 seed；P1-C 已完成 Provider 全链路 hardened egress、已保存对象测试端点和存量 quarantine 审计。完整历史结果投影仍待 P1-D。
- DataScope 当前按最高优先级角色计算，用户/部门/角色传统写入口尚未复用统一 GrantAuthority。
- Web 已有 AI 和系统管理页面，但仍使用旧的合并写入、模型列表、Provider test、部门移动和 Role-Agent shared 契约。
- 后端 CI 已有 70% 覆盖率门禁；Web 尚无 `test:coverage`、多角色 E2E 和独立真实 Provider project。

## 3. 交付顺序

```text
Phase 0 文档基线
  → Phase 1 AI 权限/模型/Provider/结果投影
  → Phase 2 DataScope/GrantAuthority/User/Role-Agent
  → Phase 1+2 原子集成门禁
  → Phase 3 Dept/Role Agent 与传统删除收口
  → Phase 4 Trace/会话终态/E2E/发布
```

## 4. Phase 0：文档基线

### 修改文件

- `AGENTS.md`
- `docs/specs/2026-08-14-ai-management-mvp-closure.md`
- `docs/specs/2026-07-02-ai-tool-gateway-design.md`
- `docs/specs/2026-08-11-ai-user-management-tools.md`
- `docs/specs/2026-08-05-chat-tool-card-embed-in-message.md`
- `docs/specs/2026-08-06-ai-message-edit-semantics.md`
- `docs/AI-SECURITY.md`
- `docs/AI-DEPLOYMENT.md`
- 本计划文件

### 数据库与权限

- 无数据库写入、迁移或 seed 执行。
- 仅记录后续迁移顺序与 fresh/upgrade 兼容规则。

### 验收

- 唯一基线进入版本管理，专项 spec 只保留实现细节和历史记录。
- Gateway 同步模块关闭 503、无 shared/R_SUPER Agent 旁路、Tool-Agent 精确归属、DataScope 并集、PreparedAction 冻结模型、结果投影和 Provider egress 契约。
- 安全与部署文档不再把生产默认关闭或 404 当成目标行为。
- Phase 0 完成后停止，不修改业务代码。

## 5. Phase 1：默认开启的权限地基

### 5.1 后端工作包

#### P1-A 模块与 endpoint 权限矩阵

修改：

- `app/main.py`
- `app/core/config.py`
- `app/core/auth.py`
- `app/modules/auth/permission_collect.py`
- `app/core/exceptions.py`
- `app/modules/ai/api/agent.py`
- `app/modules/ai/api/chat.py`
- `app/modules/ai/api/confirm.py`
- `app/modules/ai/api/resume.py`
- `app/modules/ai/api/conversation.py`
- `app/modules/ai/api/query_cache.py`
- `app/modules/ai/api/operation_log.py`
- `app/modules/ai/api/provider.py`
- `app/modules/ai/api/routing_feedback.py`
- `app/modules/ai/constants.py`
- `app/modules/ai/models/operation_log.py`
- `app/modules/ai/service/chat_service.py`
- `app/modules/ai/service/operation_log_service.py`
- `app/modules/ai/agents/gateway/executor.py`
- `app/modules/ai/lifecycle.py`
- `app/modules/ai/schemas/operation_log.py`
- `app/modules/ai/schemas/resume.py`
- `alembic/versions/b8e4c7d2a1f0_add_tenant_scope_to_ai_operation_log.py`
- `scripts/init_db.py`
- `scripts/sync_menus.py`
- `scripts/migrate_ai_mvp_permissions.py`
- `tests/modules/ai/test_module_disabled.py`
- `tests/modules/ai/test_endpoint_permissions.py`
- `tests/modules/ai/test_operation_log_api.py`
- `tests/modules/ai/conftest.py`
- `tests/modules/ai/test_confirm.py`
- `tests/modules/ai/test_resume.py`
- `tests/modules/ai/test_permission_migration.py`

实现：

- 模块关闭时只注册 `/ai/{path:path}` 熔断入口，对包括 `TRACE`/`CONNECT` 在内的标准 HTTP 方法统一 503 + `AI_MODULE_DISABLED`；不 import/初始化 AI 业务 router、Provider、Gateway 和 Registry。
- 增加 `ai:chat:use`，逐 endpoint 实现基线 §3.4 权限矩阵。
- confirm/resume router 只认证，Service 在 owner + tenant 无泄露加载并锁定后区分 reject、最小状态、完整回放和执行分支。
- 权限 dependency、前端按钮和 AI Tool 统一使用同一权限 collector；保留“禁用菜单只隐藏、不撤销既有 API 授权”的全局兼容语义。
- Postgres PreparedAction 作为终态恢复权威来源；Redis pending 缺失时不重跑 Tool，按当前权限返回最小状态或安全 SSE replay。
- operation-log 新增 tenant 列与复合索引，所有写入和 owner 查询显式 tenant scope。
- fresh 创建 `ai:chat:use` F 节点并绑定 R_SUPER；upgrade 脚本幂等授权 R_SUPER 与已有非 shared Role-Agent 绑定角色。

状态：✅ 已完成（2026-08-14；审查修复 2026-08-15）。`ai:chat:use` 已有可执行的 fresh/upgrade 数据路径，且 `R_SUPER` 不保留代码旁路；完整 Agent/Tool/file 权限 seed 仍待后续工作包。本中间构建继续受 Phase 1+2 原子集成与 Phase 4 发布门禁约束。P1-B 负责模型三端点拆分和 Agent/Tool Policy，P1-D 负责完整 lineage/result projection；P1-A 只对现有可证明字段返回最小状态。

验证：`ruff check .`、`ruff format --check .`、`python scripts/check_ai_tools.py`（19 tools / 12 checks）、AI 模块 918 项测试和全量 `pytest` 1873 项均通过；总覆盖率 72.42%，满足 70% 门禁，仅保留 2 条既有 SQLAlchemy transaction warning。新增回归覆盖权限单一来源、禁用菜单兼容语义、无 Redis 终态恢复、operation-log tenant 隔离以及 fresh/upgrade 幂等权限迁移。

#### P1-B Agent、Tool 与模型统一授权

修改：

- `app/modules/ai/service/agent_visibility.py`
- `app/modules/ai/service/agent_admin.py`
- `app/modules/ai/service/chat_service.py`
- `app/modules/ai/service/chat_run_service.py`
- `app/modules/ai/service/model_service.py`
- `app/modules/ai/service/prepared_action_service.py`
- `app/modules/ai/agents/supervisor/router.py`
- `app/modules/ai/agents/supervisor/stickiness.py`
- `app/modules/ai/agents/gateway/executor.py`
- `app/modules/ai/agents/tools/registry.py`
- `app/modules/ai/agents/tools/decorator.py`
- `app/modules/ai/agents/tools/meta.py`
- `app/modules/ai/agents/tools/file_tools.py`
- 新增 `app/modules/ai/service/agent_authorization_service.py`
- 新增 `app/modules/ai/service/model_authorization_service.py`

实现：

- `authorize_agent_access()` 固定校验 tenant、enabled、显式 Role-Agent 和至少一个可见 Tool；任何身份均无旁路。
- 显式、粘滞、Supervisor、默认、confirm/resume 使用同一 Policy；无授权 Agent 时返回稳定空状态，不静默回退。
- Gateway/Registry 要求 Tool 精确属于运行时 Agent，删除 shared 跨 Agent 豁免。
- `file.parse` 固定 `required_perms=("ai:file:parse",)` 且 `default_enabled=false`。
- 拆分 `/ai/chat/models`、`/ai/admin/agents/model-options`、`/ai/provider/models`；所有新 LLM run 复用 `authorize_chat_model()`。
- Agent 全局可变字段必须同时满足启用 `R_SUPER` 和 `ai:agent:edit`，混合非法字段 payload 整体拒绝。

状态：✅ 已完成（2026-08-15；审查修复 2026-08-15）。Agent 列表、显式/粘滞/Supervisor/default、confirm/resume 与 Gateway 共用授权规则；R_SUPER/shared 不再绕过 Role-Agent，Tool 与运行时 Agent 精确归属。显式 falsy `modelId` 不再 fallback，Supervisor 保留模型授权错误，legacy approve 的入口撤权和自动禁用统一终态收口。当前阶段只有已闭环的 shared 进入发布集合，`user_mgmt/dept_mgmt/role_mgmt` 在 Phase 2/3 完成前保持 fresh 默认禁用；upgrade 只补缺失绑定并保留显式 disabled 状态。P1-C egress 已完成，P1-D lineage/result projection 和 Web endpoint 切换仍待开发，本构建不可部署。

验证：`ruff check .`、`ruff format --check .`、`python scripts/check_ai_tools.py`（19 tools / 12 checks）、Agent/endpoint/model/seed/migration/Gateway 定向回归、AI 模块 952 项和全量 1911 项测试均通过；总覆盖率 72.74%，Alembic current/head 均为单一 `b8e4c7d2a1f0`，仅保留 2 条既有 SQLAlchemy transaction warning。

#### P1-C Provider egress

修改：

- `app/modules/ai/api/provider.py`
- `app/modules/ai/api/chat.py`
- `app/modules/ai/agents/supervisor/router.py`
- `app/modules/ai/schemas/provider.py`
- `app/modules/ai/schemas/model.py`
- `app/modules/ai/service/provider_service.py`
- `app/modules/ai/service/model_service.py`
- `app/modules/ai/service/model_authorization_service.py`
- `app/modules/ai/core/provider_registry.py`
- `app/core/config.py`
- `app/main.py`
- `app/utils/safe_http.py`
- `.env.example`
- 新增 `app/modules/ai/core/provider_egress.py`
- 新增 `scripts/audit_ai_provider_egress.py`

实现：

- 删除 `/ai/provider/test-model`，新增 `POST /ai/provider/{provider_id}/test`，body 仅 `{modelId}`。
- 保存、test、chat、Supervisor、Agent、continuation 共用 hardened transport；禁止 payload 扩张 allowlist 或环境代理绕过。
- 校验 effective URL、协议、origin/port、全部 DNS 结果、IP 类型、redirect、超时、响应大小和重试；稳定错误脱敏。
- upgrade 审计不合规存量配置并运行时 quarantine，不改写原 enabled 值。

状态：✅ 已完成（2026-08-15；P1 审查修复 2026-08-15）。Provider test 只接受已保存同域 Provider/Model 与严格字符串 `modelId`；Provider/Model 保存和所有模型选择/运行路径同时检查两级 URL 与网络配置键。OpenAI、Anthropic、DeepSeek/兼容 adapter 注入同一 hardened client/transport，禁环境代理并落实精确 origin、全部 DNS/IP、连接固定、TLS SNI/Host、redirect、timeout、响应大小、并发、重试和错误脱敏。固定 IP 后按原始精确 origin 隔离真实连接池，取消路径可靠归还并发 permit；请求只接受 identity 编码，压缩成功响应在 SDK 解压前 fail closed。无数据库 schema 迁移；upgrade 使用只读审计脚本，保留原 `enabled` 并以 `EGRESS_POLICY_BLOCKED` quarantine。

验证：新增 3 项 P1 transport 回归后，transport 单测 21 项、AI 模块 983 项、全量 1943 项通过；覆盖率 73.37%，`ruff check .`、`ruff format --check .`、19 tools / 12 checks 通过，仅有 2 条既有 SQLAlchemy warning。开发库审计 2 Provider/5 Model，报告 4 个 blocked 对象且零改写；该结果证明 quarantine 生效，不代表这些对象已满足发布 allowlist。

#### P1-D lineage 与结果投影

修改：

- `app/modules/ai/models/message.py`
- `app/modules/ai/models/prepared_action.py`
- `app/modules/ai/schemas/message.py`
- `app/modules/ai/schemas/confirm.py`
- `app/modules/ai/schemas/query_cache.py`
- `app/modules/ai/service/conversation_service.py`
- `app/modules/ai/service/operation_log_service.py`
- `app/modules/ai/agents/hitl/query_cache.py`
- 新增 `app/modules/ai/service/result_projection_service.py`

实现：

- assistant/tool message、PreparedAction、query-cache 冻结 tenant、Agent、Tool 集合、完整 subject refs、scope/hash/resolver version。
- `authorize_result_projection()` 覆盖 resume/SSE replay、conversation、pending presentation、query-cache、owner log、download/file retrieval。
- legacy 缺完整引用统一 tombstone/最小状态/404，禁止从持久化结果反推授权目标。
- PreparedAction 冻结 resolved model/provider；reject 和最小状态不受模型状态阻断，新 continuation 才复验模型。

### 5.2 Web 工作包

修改：

- `src/service/api/ai.ts`
- `src/service/api/ai-agent.ts`
- `src/typings/api/ai.d.ts`
- `src/typings/api/ai-agent.ts`
- `src/store/modules/ai/index.ts`
- `src/store/modules/ai/tool-card-projection.ts`
- `src/views/ai/agent/index.vue`
- `src/views/ai/agent/modules/agent-operate-drawer.vue`
- `src/views/ai/provider/index.vue`
- `src/views/ai/provider/modules/provider-operate-drawer.vue`
- `src/views/ai/chat/modules/chat-main.vue`
- `src/views/ai/chat/modules/chat-message.vue`
- `src/views/ai/chat/modules/chat-tool-call.vue`
- `src/views/ai/chat/modules/chat-confirmation-drawer.vue`

实现：

- 切换三个模型 endpoint；Provider 必须先保存再 test。
- 无入口、无可见 Agent、模型不可用和 tombstone 均使用稳定 UI 状态。
- 后端判定不可见时清除 store 中对应历史结果、工具卡和 presentation，不能从旧内存恢复。
- 非 R_SUPER 不显示 Agent 编辑入口；后端仍是最终安全边界。

### 5.3 数据库与数据升级

新增迁移 `<rev>_add_ai_authorization_lineage.py`，父 revision 为 `b8e4c7d2a1f0`：

- `ai_prepared_action.resolved_model_id/resolved_provider_id BIGINT NULL`，无级联删除。
- `ai_message` 增加 tenant、Tool 集合、subject refs、scope hash 和 resolver version 字段；legacy 可空，新业务结果应用层必填。
- Redis query-cache 升级 namespace/schema version，旧 key 按不存在处理。

修改/新增：

- `scripts/init_db.py`
- `scripts/sync_menus.py`
- `scripts/seed_ai_agents.py`
- `scripts/seed_config.py`
- 新增 `scripts/migrate_ai_mvp_permissions.py`

fresh：已创建 `ai:chat:use`、`ai:file:parse`、`ai:agent:list/edit`，R_SUPER 显式获得权限并绑定当前已发布的 shared；`file.parse` 写入 `ai:enabled_tools`。三个 MVP 业务 Agent 在 Phase 2/3 完成前保持默认禁用，后续随各自闭环加入发布集合。

upgrade：保留模块开关、Agent enabled、Role-Agent enabled 和 enabled tools；`scripts/migrate_ai_mvp_permissions.py` 幂等向 R_SUPER 和有非 shared 历史绑定的角色补 `ai:chat:use`，只为 R_SUPER 补缺失的阶段已发布 Agent 绑定；普通角色不自动得到 file.parse 权限或 shared 绑定。

### 5.4 测试与退出标准

- 新增模块关闭、endpoint 权限矩阵、Agent 全选择路径、Tool-Agent mismatch、模型 selector、Provider egress、结果投影和 fresh/upgrade 测试。
- 更新 `test_authz_matrix.py`、`test_role_agent.py`、`test_gateway.py`、`test_tool_registry.py`、`test_confirm.py`、`test_resume.py`、`test_conversation_api.py`、`test_query_cache.py`。
- Web 补 Agent/Provider Drawer、无 Agent 状态、tombstone 和 store 清理 Vitest。
- 验收：模块关闭统一 503；所有选择/恢复路径无旁路；撤权后所有历史读取面不返回业务结果；不合规 Provider 无任何出站请求。
- 退出：只允许进入 Phase 2 集成，不得部署。

## 6. Phase 2：用户部门/角色调整与统一授权内核

### 6.1 修改文件

共享授权：

- `app/utils/data_scope.py`
- `app/modules/ai/core/data_scope_loader.py`
- 新增 `app/modules/system/service/grant_authority.py`
- 新增 `app/modules/system/service/authorization_lock.py`
- 新增 `scripts/audit_data_scope_union.py`

用户与 Tool：

- `app/modules/system/api/user.py`
- `app/modules/system/schemas/user.py`
- `app/modules/system/service/user_service.py`
- `app/modules/system/service/dept_service.py`
- `app/modules/system/service/role_service.py`
- `app/modules/system/user/import_validator.py`
- `app/modules/system/ai_tools.py`

Role-Agent：

- `app/modules/ai/api/role_agent.py`
- `app/modules/ai/schemas/role_agent.py`
- `app/modules/ai/service/role_agent.py`

Web：

- `src/service/api/system.ts`
- `src/typings/api/system-manage.d.ts`
- `src/views/system/user/index.vue`
- `src/views/system/user/modules/user-operate-drawer.vue`
- `src/views/system/user/modules/user-import-modal.vue`
- `src/views/system/user/modules/use-import-flow.ts`
- `src/views/system/dept/modules/dept-users-modal.vue`
- `src/views/system/role/index.vue`
- `src/views/system/role/modules/ai-agent-auth-modal.vue`

### 6.2 实现与权限兼容

- 多角色 DataScope 改为物化并集；传统 API 与 AI 使用同一 resolver。
- `GrantAuthority` 冻结 permission/menu、visible/grantable Agent、scope kinds、部门/用户物化集合、tenant 和版本摘要。
- 新增 `system:user:role-auth`；upgrade 向历史 add/edit/import 任一 writer 幂等补权，但不扩张原用户写入口。
- 用户基础资料、角色、部门 API 拆分；提交旧 schema 的角色/部门字段直接拒绝。
- `user.role_lookup/update_roles` 与 `user.dept_lookup/update_dept` 使用完整旧/新集合、dominance、前后 scope 影响和快照复验。
- `/system/dept/{id}/users` 改为分页最小字段候选和完整最终集合；隐藏成员、越界成员、主部门移除整批失败。
- `/ai/role-agent` 接入完整委派 Policy，shared 可显式绑定，visible/grantable Agent 分离。

### 6.3 预检和锁

- `audit_data_scope_union.py --output <protected-path>` 在一致性快照中比较旧/新范围，扩大项返回非零并输出 canonical SHA-256。
- 发布要求 `DATA_SCOPE_UNION_ACK_SHA256`；维护锁下重跑，hash 或版本摘要漂移即拒绝。
- writer 统一按 role → dept → user 加锁；锁后重读全部授权事实并阻止 phantom。

### 6.4 测试与退出标准

- DataScope 并集、CUSTOM/DEPT 不可比较、多角色实际集合测试。
- role-auth add-only/edit-only/import-only 兼容矩阵和固定 R_USER 窄例外。
- 用户部门/角色 API 与 AI Tool 等价拒绝测试；越界、提权、隐藏成员、快照漂移零写入。
- PostgreSQL 并发、锁顺序、retry/stale 和 phantom 测试。
- Phase 1+2 全部通过后只允许合入集成分支；生产门禁继续关闭。

## 7. Phase 3：Dept/Role Agent 与传统 writer/destructive 收口

### 7.1 修改文件

- 新增 `docs/specs/2026-08-14-ai-dept-management-tools.md`
- 新增 `docs/specs/2026-08-14-ai-role-management-tools.md`
- `app/modules/system/api/dept.py`
- `app/modules/system/api/role.py`
- `app/modules/system/schemas/dept.py`
- `app/modules/system/schemas/role.py`
- `app/modules/system/service/dept_service.py`
- `app/modules/system/service/role_service.py`
- `app/modules/system/ai_tools.py`
- `app/modules/ai/service/role_agent.py`
- `app/db/base.py`
- `scripts/sync_menus.py`
- `scripts/seed_ai_agents.py`
- `scripts/seed_agent_prompts.py`
- `src/service/api/system.ts`
- `src/typings/api/system-manage.d.ts`
- `src/views/system/dept/index.vue`
- `src/views/system/dept/modules/dept-operate-drawer.vue`
- `src/views/system/dept/modules/dept-users-modal.vue`
- `src/views/system/role/index.vue`
- `src/views/system/role/modules/role-operate-drawer.vue`
- `src/views/system/role/modules/menu-auth-modal.vue`
- `src/views/system/role/modules/ai-agent-auth-modal.vue`

### 7.2 数据库与权限

- 新增 `system:dept:move`；upgrade 不给普通角色自动扩权。
- 新增 `<rev>_restrict_authorization_association_deletes.py`，将关键授权关联 FK 从隐式 CASCADE 改为 RESTRICT/Service 显式清理。
- 迁移前发现孤儿或受保护引用时中止，不自动清理授权数据。
- Dept/Role 单删与批删都要求启用 R_SUPER + 原权限；AI delete Tool 继续延期。

### 7.3 测试与退出标准

- Dept scoped tree/lookup/create/update/move/status 与间接授权影响。
- Role lookup/create/update/menu/Agent、delegable/blockedReason、成员全局影响和 role_code 不可变。
- 普通删除 403；超管遇到子部门、用户、custom-scope、角色成员引用时稳定错误且整批零删除。
- 无 FK cascade 改写 principal 授权；锁顺序和并发测试通过。
- 三个 Agent 均有真实只读和受控写入纵向切片；仍等待 Phase 4 才能发布。

## 8. Phase 4：Trace、会话终态、E2E 与发布闭环

### 8.1 修改文件

后端：

- `app/modules/ai/models/operation_log.py`
- `app/modules/ai/schemas/operation_log.py`
- `app/modules/ai/schemas/message.py`
- `app/modules/ai/service/operation_log_service.py`
- `app/modules/ai/service/conversation_service.py`
- `app/modules/ai/service/prepared_action_service.py`
- `app/modules/ai/api/operation_log.py`
- `app/modules/ai/api/conversation.py`
- `app/modules/ai/api/confirm.py`
- `app/modules/ai/api/resume.py`
- `scripts/sync_menus.py`

Web：

- 新增 `src/service/api/ai-trace.ts`
- 新增 `src/typings/api/ai-trace.ts`
- 新增 `src/views/ai/trace/index.vue` 及搜索/详情组件
- `src/service/api/index.ts`
- `src/locales/langs/zh-cn.ts`
- `src/locales/langs/en-us.ts`
- `src/store/modules/ai/index.ts`
- Chat message/tool-card 组件
- `package.json`
- `vitest.config.ts`
- `playwright.config.ts`
- Web CI workflow

### 8.2 数据库与权限

P1-A 已新增 `b8e4c7d2a1f0_add_tenant_scope_to_ai_operation_log.py`：

- `tenant_id BIGINT NOT NULL`，历史单租户数据回填 `0`。
- 索引 `(tenant_id, trace_id)`、`(tenant_id, queued_at, log_id)`。

Phase 4 再新增 `<rev>_add_ai_operation_log_trace_fields.py`：

- `agent_code VARCHAR(64) NULL`；仅可靠关联 PreparedAction 的旧日志回填，其他显示 unknown。
- `target_summary TEXT NULL`，只保存 allowlist 后冻结目标摘要。

新增 `ai:trace:view` 与 `/ai/trace` C 菜单；fresh/upgrade 默认仅授予 R_SUPER，不自动扩大普通角色审计权限。

### 8.3 会话和 Trace 验收

- Trace list/detail 使用独立脱敏 DTO，跨 tenant/不存在统一 404；禁止 content、raw prompt、raw/frozen args、密钥和未脱敏 PII。
- 删除会话时锁定 PreparedAction；非运行 action 终态化为 expired + `AI_CONVERSATION_DELETED`，运行中返回 `AI_ACTION_RUNNING` 并整体回滚。
- HITL resume/download/tool-only/reload、撤权后 reject/最小回放、所有历史结果 tombstone 和前端缓存清除通过。

### 8.4 自动化门禁

后端：

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=70
uv run python scripts/check_ai_tools.py
```

Web：

```bash
pnpm lint
pnpm typecheck
pnpm test:coverage
pnpm build
pnpm e2e
pnpm e2e:provider
```

- Web 增加 `test:coverage`，覆盖率门禁 ≥70%。
- Playwright 使用 R_SUPER、无入口、无绑定、不同 Agent、不同 DataScope 和多角色账号；核心步骤通过页面执行，API 仅用于隔离 fixture 准备/清理。
- `e2e:provider` 对 user/dept/role 各执行一个真实只读和受控写指令；release 缺凭据必须失败，不得 skip。

## 9. 最终发布检查

- [ ] Alembic fresh upgrade、存量 upgrade 和必要 downgrade 演练通过，保持单一 head。
- [ ] fresh/upgrade seed 不覆盖部署方开关、Agent enabled、绑定和 enabled tools。
- [ ] `DATA_SCOPE_UNION_ACK_SHA256` 与维护锁下重跑报告一致。
- [ ] Provider egress 审计无未确认 quarantine 风险。
- [ ] 后端/Web lint、typecheck、覆盖率、build、确定性 E2E 和真实 Provider E2E 全绿。
- [ ] AI Trace 可核对成功、拒绝和失败终态且 DTO 脱敏。
- [ ] 所有专项 spec 回写实际测试证据和 ship date。
- [ ] 唯一基线状态由“实施中”翻转为“已发布”。
