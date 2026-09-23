# ADR-0003: 可信租户上下文与隔离边界

- **Status**: Accepted
- **Date**: 2026-08-31
- **Deciders**: hohu core team
- **Tags**: tenant / authentication / authorization / database / audit / architecture

## Context（背景）

引入多租户隔离前，HoHu 以单租户方式运行。`app.core.tenant.resolve_tenant_id()` 固定返回
`0`，System 的 User、Role、Department、Menu、Config、Dict、Job 和主要关联表
尚无租户列；Marketplace 低代码数据、File 以及 AI PreparedAction、结果投影和
Trace 已经局部携带 `tenant_id`。这种“部分表有字段、部分调用硬编码 0、部分对象
通过父表隐式继承”的状态可以支撑单租户，但不能直接通过允许任意 tenant 值升级为
多租户：任何遗漏的查询、外键、缓存键、后台任务或历史结果读取面都可能形成水平
越权。

HoHu 同时是开源项目。默认本地部署不应因多租户能力增加额外登录步骤或平台运维
负担；二次开发者也不能依赖“记得在每条 SQL 后补 tenant 条件”维持安全。项目需要
一个含义唯一、可渐进迁移、能由数据库和测试共同验证的租户边界。

## Decision（决策）

**所有租户业务能力统一使用服务端建立的不可变 `TenantContext`；租户数据显式持有
非空 `tenant_id`，应用层强制 scope，数据库用同租户约束兜底。平台全局资源与默认
租户严格分义，跨租户管理使用独立平台控制面。**

### 1. `0` 是默认租户，不代表全局

- `tenant_id=0` 作为现有本地单租户数据的合法默认租户保留，并在 `sys_tenant` 中有
  一条真实记录。
- 平台全局资源不增加 `tenant_id`，不得用 `0` 或 `NULL` 伪装“全局”。
- 租户资源的 `tenant_id` 一律 `NOT NULL`；迁移期 server default `0` 只用于回填，
  全部 writer 接入可信上下文后移除默认值，防止漏传时静默写入默认租户。

### 2. 登录定位不等于授权事实

- 默认 `single` 模式只使用服务端 canonical Default Tenant 0，部署不能改写 ID/code，现有
  登录 UI 无新增步骤。
- hosted 模式可由已校验 Host/subdomain 或登录请求中的 `tenantCode` 定位候选租户；
  它只是未可信 locator，只有租户启用、用户属于该租户且凭据验证成功后才能建立
  `TenantContext`。
- hosted 模式受代码级发布门禁和环境配置共同控制；部署时必须满足租户激活与隔离检查，不能仅修改环境变量绕过发布门禁。
- User 首期只属于一个租户；用户名唯一约束改为 `(tenant_id, user_name)`。多租户
  membership/tenant switch 不在本 ADR 范围，未来需要时另写 ADR。
- access/refresh token 同时冻结 `sub`、`tid` 与 tenant security version `tver`；每次认证按
  user/tenant 查询当前状态并复验 `Tenant.row_version`，tenant disable→enable 不会复活旧 token。
  请求 header、query、body、AI Tool args 或 multipart 中的
  `tenant_id` 都不能覆盖 token 与数据库建立的上下文。

### 3. Service 无租户默认值、无可变租户状态

- `TenantContext` 只能由认证依赖、已验证的后台任务 envelope 或独立平台控制面构造。
- 租户 Service 方法使用 keyword-only `tenant: TenantContext`，不得使用
  `tenant_id: int = 0`，也不得把当前租户保存在模块级 singleton 的 `self.tenant_id`
  中；后者在并发请求下会串租户。
- tenant-owned 查询从统一的无状态 `tenant_select()/tenant_filter()` 或领域 repository
  起步。直接 ID、列表、count、exists、JOIN、eager load、UPDATE、DELETE 和 raw SQL
  都必须包含同一 tenant predicate。
- 创建时由服务端写入 `context.tenant_id`；客户端 tenant 字段 fail closed。更新和删除
  先以 `(tenant_id, id)` 定位，跨租户目标与不存在使用相同 404 语义。

### 4. 关联和唯一性由数据库阻止跨租户

- 租户聚合根及其 tenant-owned 子表都直接持有 `tenant_id`，不只依赖父表隐式继承。
- tenant-owned 唯一键改为包含 `tenant_id`；同一租户内保持唯一，不同租户可以复用
  用户名、角色编码、部门名规则允许的业务键、配置键和字典类型。
- User-Role、User-Department、Role-Menu、Role-Department、Role-Agent 等关联表增加
  `tenant_id`，通过 `(tenant_id, foreign_id)` composite FK/unique constraint 保证两端
  属于同一租户。树形 `parent_id` 也使用同租户约束。
- 应用层 scope 是主防线；PostgreSQL composite constraint 是数据完整性防线。RLS 在
  连接级 tenant 绑定、迁移和运维路径成熟后作为第三道防线引入，不替代 Service Policy。

### 5. 系统超级管理员与租户管理员分离

- 用户名不授予权限。启用的 `R_SUPER` 角色授予本租户管理权限；仅默认租户中启用的系统超级管理员可通过普通登录会话管理全局 Agent、租户生命周期和租户模型授权。
- AiAgent、AiProvider、AiModel 等平台运行配置首期保持 platform-global，通过显式授权的控制面修改（Agent 和租户管理使用系统角色；Provider/模型目录等平台维护 API 使用独立平台身份）；租户是否可使用模型由显式 tenant-model policy 决定。
- 跨租户管理使用 `/platform/**` 或离线管理命令，按具体入口要求系统角色或独立平台
  维护身份，显式绑定目标租户，并记录 reason/ticket 和 append-only 审计。普通业务 API 不接受
  `tenantId` 来切换作用域。

### 6. 结果投影、审计、异步和缓存继承同一租户

- AI Conversation、Message、PreparedAction、OperationLog、query-cache、download token
  和 projection lineage 使用同一非空 tenant；读取时先比较 tenant，再校验 owner、Agent、
  Tool 和 data scope。跨租户结果按读取面返回 tombstone、最小状态或同面 404。
- 普通模型结果和用户 UI 不输出原始 `tenant_id`；租户 ID 只进入服务端授权 lineage、
  审计 envelope 和平台运维视图。
- System operation/login log、AI Trace 和安全事件必须记录可判定的 audit scope。已认证
  操作写非空 tenant；认证失败若无法定位租户，显式记录 unresolved scope，不能伪装为
  tenant 0。租户审计员只能读取本租户，retention purge 只能由独立平台任务执行。
- 后台任务、导入导出、Redis cache/quota/lock/idempotency key、对象存储路径和事件
  envelope 都冻结 tenant。Worker 不得在缺少 tenant 时回退为 0。

## Alternatives Considered（备选方案）

### 备选 A: 继续到处传 `tenant_id: int = 0`

- ✅ 改动最小。
- ❌ 默认值会掩盖漏传；模块级 Service 保存 tenant 会产生并发串租户；二开代码很难
  通过 review 发现所有遗漏。

### 备选 B: 只给聚合根加 tenant，子表通过 FK 隐式继承

- ✅ 列和索引较少。
- ❌ 每个子表查询都必须记住 JOIN 父表，raw SQL、缓存和审计容易漏；数据库无法直接
  阻止把不同租户的关联端点连接起来。

### 备选 C: 只依赖 PostgreSQL RLS

- ✅ SQL 遗漏时数据库仍可阻断。
- ❌ async 连接池需要每事务可靠 SET/RESET，迁移、Worker 和平台操作都需要额外策略；
  错误配置可能全拒绝或全放行，也无法替代领域 data scope 和跨对象完整集合校验。

### 备选 D: 把 `tenant_id=0` 同时解释为平台全局

- ✅ 无需区分默认租户与全局记录。
- ❌ 本地租户 0 会看到平台记录，unique、cache 和审计语义无法判定，后续迁移只能靠
  猜测数据来源。

## Consequences（后果）

### 正面

- 单租户默认体验不变，多租户启用前可以分阶段回填和验证。
- tenant 的来源、资源分类、错误语义和跨租户管理路径唯一，二次开发者不需要猜测
  `0/NULL` 或某个 Service 默认值的含义。
- 应用、数据库、AI 投影、审计和异步链形成一致的隔离边界，单点遗漏更容易被静态
  门禁和双租户回归发现。

### 负面 / 已知 trade-off

- System 核心表、关联表、唯一约束和大量 Service 都需要迁移，不能一次性打开多租户。
- hosted 登录需要 tenant locator；同一用户参与多个租户和在线切换暂不支持。
- platform-global AI 配置需要新 tenant-model policy；租户 BYOK/自定义 Agent 另行设计。
- composite FK 和 tenant-leading index 会增加 schema 与测试数量；RLS 仍需后续专项实施。

## 当前边界与参考

1. **默认租户菜单同步显式限定作用域** — `scripts/sync_menus.py` 的去重、父节点解析和旧路由改名只查询默认租户。**反例**: 其他租户的同名菜单导致默认租户漏补菜单或引用跨租户父节点。**回归**: `tests/scripts/test_sync_menus_tenant_scope.py`。

Hosted 登录受部署模式、全局登录开关、数据库租户状态及安全版本共同控制，不再受单个 canary ID 限制。详见 [核心多租户管理](../MULTI-TENANCY.md)。Marketplace/Lowcode 不提供 hosted 多租户能力；多租户 membership、租户在线切换、BYOK 和 RLS 需要独立设计。

- [安全规范](../SECURITY.md)
- [AI 安全指南](../AI-SECURITY.md)
- [AI 部署指南](../AI-DEPLOYMENT.md)
- [数据库迁移指南](../DATABASE-MIGRATIONS.md)
