# AI 用户管理缺失工具补齐

> 状态：🚧 Plan 1 已完成（2026-08-11）；Plan 2 已完成 P2-A、P2-B1、P2-B2 与 P2-B3，其余工作包待实现
> 日期：2026-08-11｜更新：2026-08-18
> 关联：[`2026-08-14-ai-management-mvp-closure.md`](./2026-08-14-ai-management-mvp-closure.md)、[`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md)、[`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md)

## 1. 背景与范围

当前 `user_mgmt` 已注册 `user.count/stats/distinct/list/lookup/update/batch_delete/import_preview/import_execute/export`，但 Gateway 内置 Agent 清单承诺的 `user.create` 与 `user.reset_password` 未实现。旧的导入导出 spec 还把 `user.create` 错标为已完成，造成设计与运行时 Registry 漂移。

2026-08-11 的 Plan 1 只补齐这两个已承诺工具。2026-08-14 的 MVP 收口决策已经把原 Task 25a+ 的 `user.update_dept` / `user.update_roles` 提升为 Plan 2 必做能力；它们继续保持独立工具和独立风险边界，不混入通用 `user.update`。

## 2. 决策记录

1. **只补 `user.create` 与 `user.reset_password`** — 它们已经出现在 Gateway §10.1 的 `user_mgmt` MVP 清单，而部门和角色调整在现有实现中明确标记为 Task 25a+。**反例**: 因“用户管理工具”字面含义一次性加入所有设想工具，会绕过各自的权限边界、data scope 与交互设计。**回归**: Registry 精确新增两个名称；静态内置工具清单从 16 增至 18；旧 spec 完整度表纠正。

2. **密码由后端私有配置生成，不进入工具签名和返回值** — 两个工具统一读取 `sys_config.auth:default_password`，在业务层哈希后写库；`AiToolMeta.sensitive_input` 声明密码，但函数签名、dry-run、`ToolResult.data`、`UIResult`、审计摘要均不包含密码值。管理员按现有 V1 策略从私有配置获知默认密码并线下通知用户。**反例**: 让 LLM 接收新密码，或把临时密码放入 result/UI，会让明文进入模型上下文、消息持久化或浏览器状态。**回归**: 签名反射与序列化测试断言没有 `password/new_password/hashed_password`；静态 sensitive-input gate 通过。

3. **新建用户使用既有普通用户角色 `USER_ROLE_CODE=R_USER`，AI 不接收角色参数** — `R_USER` 是现有数据库、认证契约与测试长期使用的普通用户角色编码；工具只查找并绑定该启用角色，fresh install 也按同一编码 seed。角色属于权限边界，不能由自然语言直接提升。**反例**: 沿用未被业务使用的旧常量值 `user` 会在已部署数据库误报默认角色不存在；暴露 `roles=["R_SUPER"]` 给模型则会把“创建账号”变成越权提权入口。**回归**: 常量与初始化种子测试锁定 `R_USER`；创建结果只绑定 `USER_ROLE_CODE`；默认角色缺失或禁用时返回稳定业务错误。

4. **AI 新建必须指定一个主部门并同时校验 data scope** — `primary_dept_id` 是创建工具必填参数；执行和 dry-run 都调用 `ensure_targets_in_scope(ctx, dept_ids=[...])`，执行再由共享 `UserDepartmentAssignmentService` 完成完整集合、主部门、锁后重算和授权 dominance。**反例**: 允许无部门创建，或继续用无授权关联 helper，非全量 data-scope 操作者可能创建出自己不可见、无法继续治理的账号。**回归**: 越界部门在 dry-run/执行两条路径均被拒绝；成功用户只有一个主部门。

5. **两个工具都强制 HITL 且支持 dry-run** — 创建账号和使旧密码立即失效均为高风险写操作，确认抽屉在执行前显示目标账号、主部门或密码重置影响，但不显示敏感值。**反例**: 让 balanced agent 自动执行单行创建/重置，会把模型误判直接变成账号或凭据变更。**回归**: `risk="high"`、`hitl_always=True`、`dry_run_supported=True`，并存在同模块命名约定 dry-run hook。

6. **重置密码禁止操作当前登录账号，所有超级管理员目标仅允许超管操作** — 当前账号应走需要旧密码的个人改密流程；用户名 `admin` 或绑定启用 `R_SUPER` 的账号均属于高权限目标，普通委派管理员不得借 AI 重置。**反例**: 当前用户误重置自己会让当前会话与凭据状态割裂；普通管理员重置任一超级管理员可造成接管。**回归**: self-target 返回稳定错误；非超管 targeting `admin` 或其他 `R_SUPER` 均被拒；data-scope 校验仍先于业务写入。

7. **Tool adapter 只编排，事务仍由 Gateway 管理** — 工具复用用户 Service 与共享角色/部门 Assignment Policy，只允许 `flush` 获取 ID，不在 service 或 tool 内 `commit`。**反例**: 工具自行 commit 会破坏 Gateway 的审计、失败回滚和 durable finalizer 原子性。**回归**: 单测用 session 验证结果可回滚；代码静态检查无 `commit()`。

8. **确认抽屉与结果卡使用语义字段做前端 i18n，不解析后端中文** — direct HITL 继续保存安全 canonical presentation，Web 按 tool code 与 `user_name/primary_dept_id/user_id/affectedCount` 映射中英文工具名、摘要和字段标签；结果卡 title/field label 使用既有 i18n key。**反例**: 正则匹配 dry-run 中文或把用户输入翻译掉，会在文案调整后失效并改变批准对象。**回归**: mapper Vitest 覆盖 create/reset 两个工具、unknown fallback 与无密码值；Vue typecheck 固定所有 key 存在。

9. **fresh install 默认密码按环境 fail closed 并 seed 既有普通角色** — 旧 seed `hohu123456` 不含大写字母，与 `validate_password` 冲突；dev/test 使用通过强度校验的 `Hohu123456` 便利值，prod seed 为空并要求部署方显式配置私有值。同时 seed `USER_ROLE_CODE=R_USER` 的普通角色，不能另造 `user` 角色。已部署环境不静默创建/修改角色，缺失时由工具返回稳定错误，交给管理员显式治理。**反例**: 生产仍接受公开 seed、工具绕过 schema 接受弱配置、运行时偷偷创建角色，或初始化生成第二套普通角色编码，会让凭据与授权边界失守。**回归**: init seed 测试验证环境分支、密码强度、角色名称/编码/状态/data scope；工具测试验证缺失/禁用默认角色时 fail closed。

10. **自然语言部门名称先解析、后按 ID 写入** — `user_mgmt` 增加只读 `user.dept_lookup(dept_name)`，在调用者可见部门范围内按启用状态和完整名称查候选；唯一命中时 Agent 把返回 ID 传给 `user.create`，零命中时提示检查名称，多命中时展示父部门并要求用户消歧。`user.create` 继续只接收稳定 ID。**反例**: 让模型凭记忆猜 ID、让写工具按非唯一名称直接更新，或要求普通用户手工查 Snowflake ID，分别会造成误写、歧义和糟糕交互。**回归**: 工具可见性、唯一命中、data scope、同名多候选、零命中与“先 lookup 后 create”提示词测试。

11. **确认展示由 dry-run 安全补充名称，但必须绑定原始冻结 ID** — direct HITL 的 `confirmation_fields` 同时携带同名冻结 `value` 与可读 `display_value`；Gateway 校验两者绑定后，`user.create` 才把 `primary_dept_id` 展示为 `部门名称（ID）`。批准后仍执行服务端冻结的 `primary_dept_id`，名称只用于可读展示。**反例**: 前端解析 dry-run 中文摘要提取名称会受文案变化影响；允许未绑定展示值会出现展示 A、执行 B；把名称替换进冻结 args 会破坏 ID 精确授权。**回归**: dry-run 字段、Gateway raw/display 校验、确认抽屉展示值与 frozen args 分离测试。

12. **所有启用 `R_SUPER` 目标都按超级管理员账号保护** — 密码重置不能只按保底用户名 `admin` 判断；目标绑定启用的 `R_SUPER` 时，执行者也必须是当前启用的超级管理员。**反例**: 委派管理员拥有 reset-password 与全量 data scope 后重置另一个 `R_SUPER`，再用默认密码登录完成提权。**回归**: 非超管重置 `admin` 和其他 `R_SUPER` 两条路径均返回 `AI_SUPER_ADMIN_REQUIRED`。

13. **生产环境禁止使用公开初始化密码** — `Hohu123456` 仅作为 dev/test fresh-install 便利值；prod seed 写空值，且共享默认密码 helper 对从旧环境带入的公开 sentinel 再次 fail closed。**反例**: 仅在 remark 写“上线前修改”但运行时仍接受，会让新建、导入和重置账号落到公开凭据。**回归**: prod seed 为空；prod 数据库保存公开 sentinel 时返回 `AI_IMPORT_DEFAULT_PASSWORD_INVALID`，AI 映射为 `AI_USER_DEFAULT_PASSWORD_INVALID`。

14. **HITL 可读展示必须与冻结参数机器绑定** — dry-run 覆盖字段同时携带原始 `value` 和 `display_value`；Gateway 仅在原始值等于同 label frozen arg 时接受，并拒绝未知或重复 label。**反例**: 展示“总部（ID A）”但批准后执行 ID B，会破坏确认操作的授权语义。**回归**: 正常名称展示仍执行原 ID；不一致 raw value 在创建 pending action 前被拒绝。

15. **内置 prompt 只自动升级已知历史默认值** — seed 脚本维护历史默认 prompt 集合，空值和已知旧默认值自动升级，无法识别的部署方自定义值保留；`--force` 仅用于管理员明确覆盖。**反例**: 只填空会让存量环境永远拿不到新工具编排；无条件覆盖会删除部署方业务规则。**回归**: 旧 user_mgmt 默认 prompt 自动升级，自定义 prompt 不变，部署文档包含安全 seed 步骤。

16. **已解析的部门展示值不得因后续预检失败而丢失** — `user.create` dry-run 一旦完成部门 scope、状态和名称解析，后续即使因用户名已存在返回 `ok=False/count=0`，仍必须携带绑定原始 `primary_dept_id` 的 `confirmation_fields.display_value`，确保 HITL 显示 `部门名称（ID）`。**反例**: 重复用户名分支只返回 reason → Gateway 回退展示 Snowflake ID，用户无法确认实际部门。**回归**: 重复用户名 dry-run 测试断言部门 display value 保留，并经 `_build_direct_confirmation_fields` 输出名称与 ID。

17. **新工具所有用户可见表面统一 i18n** — 工具卡描述和 errorCode 从 locale 解析；确认抽屉对已知工具不再渲染后端中文 dry-run 文案；data_list 列名使用 i18n key 并在组件端解析。**反例**: 只国际化 presentation summary，会让英文界面仍出现中文影响摘要、表头或原始错误码。**回归**: mapper Vitest 覆盖 create/reset dry-run、工具描述、全部 `AI_USER_*` locale 解析与 unknown fallback；DataListView 对未知 label 保留原文。

18. **部门与角色调整提升为独立 MVP 工具** — `user.update_dept` 与 `user.update_roles` 分别处理组织数据范围和最终有效授权集合，两者都强制 HITL、冻结完整旧/新 ID 集合、目标用户前后物化 scope 和操作者 `GrantAuthority` 摘要，并在批准执行时按统一锁协议重读重算；不能并入只允许资料字段的 `user.update`。**反例**: 复用一个宽泛 `user.update` payload 会让模型把资料修改、部门迁移和权限提升混成同一审批语义；只比较新角色的枚举型 data_scope 会漏掉旧角色移除、CUSTOM 集合和 Agent/menu 的组合影响。**回归**: `tests/modules/ai/test_user_assignment_tools.py` 覆盖完整集合、越界部门、最终授权 dominance、自改、R_SUPER/admin 保护、审批后权限/目标漂移和事务回滚。

19. **角色授权使用独立 `system:user:role-auth`，不借用角色列表权限** — 用户 update/create/import 各自的写入口权限只决定“能否写该类用户操作”，`system:user:role-auth` 再决定该入口能否接收显式角色；它同时授权查询最小可委派角色候选，但不授予 Role 管理页面。upgrade 向历史 add/edit/import 任一 writer 幂等补权，不横向扩张原入口。**反例**: 用 `system:role:list` 作为用户角色写权限会把页面可见性误当委派能力；给 add-only 角色补 edit 会扩大存量权限。**回归**: add-only/edit-only/import-only 兼容矩阵分别断言原入口保持不变，显式角色写入必须叠加 role-auth。

20. **名称只用于 lookup 和确认展示，写工具只接受冻结 ID** — `user.role_lookup` 只返回当前 `GrantAuthority` 下可委派角色的最小摘要，零/多命中必须澄清；不能依赖 Phase 3 的 `role.lookup`。**反例**: 在批准时再次按名称查找可能命中被改名或新建的另一角色。**回归**: lookup 同名、零命中、不可委派和批准后 ID 对象漂移测试全部 fail closed。

21. **页面资料、角色与部门使用独立、拒绝额外字段的请求契约** — `PUT /system/user/{id}` 只接收资料字段，角色和部门分别使用完整集合的独立 writer；旧 `roles/deptIds` 不能静默忽略。**反例**: 继续用一个宽 schema 再由 Service 忽略角色或部门字段，会让旧 Web 显示保存成功但授权实际未更新。**回归**: schema 测试锁定旧字段 422、canonical Snowflake 字符串、严格 boolean、集合去重，以及页面 writer 独立提交事务。

22. **页面创建、AI 创建和导入新建共用完整角色集合 Policy** — 所有路径在关联写入前按 role → dept → user 锁定并重读，比较目标旧/新 permission、menu、active Role-Agent 与物化部门/用户集合；固定 `R_USER` 只免除 role-auth，不免除 dominance。**反例**: AI/导入继续直接写 `sys_user_role`，或只检查角色 ID 是否属于操作者，会形成页面与批处理两套授权规则。**回归**: 合法子集、旧/新越权、CUSTOM、Agent、自改、R_SUPER/admin、固定角色越界及事务顺序测试。

23. **导入角色权限由文件表头存在性决定** — `role_input` 列一旦存在，即使整列为空也要求 `system:user:import + system:user:role-auth`；缺列时导入新用户只能走受 dominance 约束的固定 `R_USER`。**反例**: 按非空单元格判断会允许低权用户保留角色列并在预览/执行间改变内容。**回归**: CSV/XLSX 表头检查、空角色列权限拒绝、缺列默认角色与无业务写入测试。

24. **页面部门完整替换先冻结依赖、锁后重算授权影响** — `PUT /system/user/{id}/departments` 与后续 `user.update_dept` 共用独立 Service；旧/新部门和目标用户都必须在操作者 scope 内，目标完整角色集合在变更前后的物化授权都必须受 `GrantAuthority` dominance。Service 预读角色、自定义部门、用户部门和子树结构影响，按 role → dept → user 加锁后重载；主部门标记或新结构依赖漂移 fail closed。**反例**: 只验证新部门可见会借完整替换删除不可管理的旧关联，或通过可见父部门扩出不可见子树。**回归**: `tests/modules/system/test_user_department_assignment_service.py`、`tests/modules/system/test_user_role_contracts.py`、`tests/modules/system/test_user_api_atomicity.py`。

25. **主部门策略直读锁行且候选作用域不继承旧主体** — `user_require_primary_dept` 在共享部门 Policy 内使用同一事务的 uncached `SELECT FOR UPDATE`；候选部门授权物化必须先移除数据库旧 scope 查询中的目标用户，再按候选部门与 SELF 规则决定是否加入。**反例**: Redis 失效失败让 false 缓存继续放行空部门，或 CUSTOM 旧部门查询残留目标用户而掩盖迁移后的授权变化。**回归**: `test_replace_departments_bypasses_stale_primary_policy_cache`、`test_hypothetical_custom_scope_drops_subject_from_removed_department`，并锁定隐藏旧关联删除与 admin/R_SUPER 目标保护。

26. **在线用户部门关联只保留一个写入边界** — 页面 create、AI create、import create/overwrite 与部门成员页都调用 `UserDepartmentAssignmentService`；导入先在整批锁内验证组合后的角色/部门事实，再使用仅面向锁内已验证集合的 apply helper。**反例**: 保留 `DeptService.update_user_depts` 或 import raw association SQL 会形成不执行旧/新 scope 与 dominance 的旁路。**回归**: create/AI/import/department-membership 回归覆盖全部入口，运行时代码检索不存在共享 Service 之外的 `user_depts` 写入。

27. **Web 不以宽 payload 模拟独立授权 writer** — create 根据 role-auth/dept-list 决定是否提交显式集合；edit 将 profile、roles、departments 分别提交，且只写实际变化的关联。部门成员页从最小分页 API 获取完整候选，并禁止在该入口移除主部门成员。**反例**: 每次资料保存都重写角色/部门会因隐藏旧集合产生误删或无关授权失败。**回归**: `user-operate-drawer.spec.ts` 与 `dept-users-modal.spec.ts`。

28. **导入的同一现有目标在一个批次中只能出现一次** — 解析结果按稳定 `target_user_id` 查重，重复行全部返回 `AI_IMPORT_DUPLICATE_TARGET`；涉及 `DEPT_AND_SUB` 时在锁前展开完整后代部门，锁内再执行最终角色/部门组合校验。**反例**: 两行分别改变同一用户的角色和部门，会让执行终态成为任一单行都未验证的组合。**回归**: duplicate target 与 descendant lock 定向测试。

29. **分页全集与多 writer UI 显式处理不完整状态** — 部门成员分页只有全部成功才启用 PUT；用户编辑中角色/部门已提交而后续 writer 失败时，关闭旧表单、提示部分成功并刷新服务端事实。**反例**: 分页失败后空集合仍可提交，或部分 commit 后让用户继续基于旧快照编辑。**回归**: 部门弹窗首/后页失败和用户编辑后续 writer 失败 Vitest。

## 3. 工具契约

### `user.create`

- 权限：`system:user:add + system:dept:list`
- 参数：`user_name`、`primary_dept_id`、可选 `nickname/user_email/user_phone/user_gender/status`
- 后端策略：密码=`auth:default_password`，角色=`R_USER`
- 结果：`{created: 1, userId, userName, roleCode, primaryDeptId, passwordPolicy}`；其中 `passwordPolicy` 只返回固定枚举 `system_default`，不返回值
- UI：`detail_card`，仅含非敏感账号摘要

### `user.dept_lookup`

- 权限：`system:user:add`
- 参数：`dept_name`（用户口语中的完整部门名称）
- 行为：仅查询启用且位于调用者 data scope 的同名部门，不写数据
- 结果：`{query, matchCount, matches: [{id, name, parentId, parentName}]}`
- 编排：唯一命中后调用 `user.create(primary_dept_id=matches[0].id)`；零命中或多命中不得猜测

### `user.reset_password`

- 权限：`system:user:reset-password`
- 参数：`user_id`
- 后端策略：新密码=`auth:default_password`
- 结果：`{updated: 1, userId, userName, passwordPolicy}`
- UI：`rows_affected`，审计只记录目标 ID 与策略名

### `user.dept_lookup`（Plan 2 权限纠偏）

- 权限：`system:dept:list`，不再借用 `system:user:add`
- 参数：名称/路径 query，`limit <= 20`
- 返回：只含当前部门 scoped selector 可见的 `{deptId, deptName, path}`
- 语义：零命中或多命中必须澄清，不拼回 scope 外祖先

### `user.role_lookup`（Plan 2）

- 权限：`system:user:role-auth`，不自动要求或授予 `system:role:list`
- 参数：名称/code query，`limit <= 20`
- 返回：当前 `GrantAuthority` 下可委派角色的 `{roleId, roleCode, roleName, dataScope}`
- 语义：零/多命中必须澄清；名称只供展示，后续写工具只接收稳定 ID

### `user.update_dept`（Plan 2）

- 权限：同时满足 `system:user:edit` 与 `system:dept:list`
- 参数：`user_id`、完整 `dept_assignments=[{dept_id, is_primary}]`
- 风险：`high`、`hitl_always=True`、`dry_run_supported=True`
- 数据权限：目标用户与 `旧部门集合 ∪ 新部门集合` 全部位于当前操作者可写 scope；不能借替换删除越界旧关联
- 授权影响：按目标用户完整启用角色分别物化变更前/后的部门和用户集合，两者都必须是操作者 `GrantAuthority` 的子集
- 业务规则：配置要求主部门时非空集合恰好一个主部门，否则最多一个；ID 去重且部门存在、启用、可分配
- 审批快照：冻结用户、状态、完整旧/新部门、主部门、角色定义、前后物化 scope hash、配置和操作者授权摘要，任一漂移返回 `AI_PREPARED_ACTION_SNAPSHOT_STALE`

### `user.update_roles`（Plan 2）

- 权限：同时满足 `system:user:edit` 与 `system:user:role-auth`
- 参数：`user_id`、完整 `role_ids`
- 风险：`high`、`hitl_always=True`、`dry_run_supported=True`
- 数据权限：目标用户必须位于当前操作者 scope；新集合非空、去重，角色存在且启用
- 提权防护：禁止自改；admin 或变更前后拥有启用 `R_SUPER` 的用户仅允许超级管理员操作；非超管不得移除自己无权管理的旧角色
- dominance：用应用完整新角色集合后的 permission/menu、Agent 和实际物化数据集合与操作者 `GrantAuthority` 做集合包含比较，不能只比单个角色或枚举优先级
- 审批快照：冻结完整旧/新角色、角色定义、目标用户前后有效授权摘要和操作者授权摘要，批准时锁后重新校验
- 复用边界：安全校验下沉共享 Service，用户管理页面与 AI 工具不得形成两套授权规则

### 传统页面与导入契约（Plan 2 同步交付）

- `PUT /system/user/{user_id}` schema 移除角色/部门字段；提交旧字段直接拒绝，不能静默忽略。
- `PUT /system/user/{user_id}/roles` body 固定 `{roleIds}`，要求 `system:user:edit + system:user:role-auth` 并复用完整角色替换 Policy。
- `PUT /system/user/{user_id}/departments` body 固定 `{deptAssignments}`，要求 `system:user:edit + system:dept:list` 并复用完整部门替换 Policy。
- `GET /system/user/assignable-roles` 只要求 `system:user:role-auth`，返回最多 20 个最小候选。
- create payload 只要出现 `roleIds`（包括空数组）就要求 `system:user:add + system:user:role-auth`；import 模板只要出现角色列（包括整列空值）就要求 `system:user:import + system:user:role-auth`，任一行越权整批零业务写入。
- 未显式提供角色时仅允许后端分配唯一、启用且不超过操作者 `GrantAuthority` 的固定 `R_USER`；请求 schema 不接受任意角色，这是唯一不要求 role-auth 的窄例外。

## 4. Plan 状态

- [x] Task 1 ✅ 已完成（2026-08-11）：纠正旧 spec 的 `user.create` 假完成记录；强制内置 inventory 从 16 更新为 18。
- [x] Task 2 ✅ 已完成（2026-08-11）：新增 12 个后端定向用例，覆盖安全签名、默认密码/角色/部门、data scope、self/admin 保护、dry-run 与无敏感输出。
- [x] Task 3 ✅ 已完成（2026-08-11）：实现 `user.create` / `user.reset_password`、两个 dry-run hook、默认策略、Agent prompt 与 fresh-install seed。
- [x] Task 4 ✅ 已完成（2026-08-11）：中英文工具名、确认摘要、参数标签、错误码与结果 label 落地；DetailCard 支持 i18n field label key。
- [x] Task 5 ✅ 已完成（2026-08-11）：后端 `ruff check .` + `ruff format --check .` + 1801 pytest 全过；18 个内置工具通过 12 项静态 gate；前端全量格式/typecheck/lint（0 error，31 条既有 warning）+ 14 文件 64 Vitest 全过。
- [x] Task 6 ✅ 已完成（2026-08-11）：纠正旧 `USER_ROLE_CODE=user` 与现网 `R_USER` 的契约漂移；初始化统一 seed `普通用户/R_USER/启用/仅本人`，新增种子回归后端全量 1802 pytest 通过。
- [x] Task 7 ✅ 已完成（2026-08-11）：补齐 `user.dept_lookup`、data scope、同名消歧、Agent 名称解析编排与前端展示，使“新建用户圣诞，部门是总部”无需用户输入部门 ID；19 个工具静态门禁、后端全量 1808 pytest、前端 64 Vitest/typecheck/lint 均通过。
- [x] Task 8 ✅ 已完成（2026-08-12）：确认抽屉的主部门显示为 `部门名称（ID）`，执行参数和审批快照继续使用原始部门 ID；后端全量 1809 pytest、19 工具静态门禁及前端全量检查通过。
- [x] Task 9 ✅ 已完成（2026-08-12）：审查纠偏超级管理员目标保护、prod 默认密码 fail-closed、HITL raw/display 绑定、存量 prompt 安全升级及工具卡/dry-run/data-list 全表面 i18n，并补充前后端反例测试。
- [x] Task 10 ✅ 已完成（2026-08-17）：新增 `system:user:role-auth` 的 fresh/sync/upgrade 权限数据与 add/edit/import 兼容矩阵；实现多角色并集 resolver、不可变 `GrantAuthority`/集合 dominance、role → dept → user 统一锁服务，以及只读 canonical scope-diff 与 ACK 门禁。传统 filter、导入预检、AI `DataScopeContext`/lineage 已共用新 resolver；授权核心、会话锁与 scope 审计定向测试 27 项、后端全量 2010 项和 73.24% 覆盖率门禁通过。此任务未接入实际用户/部门 writer，也未修改 Web。
- [x] Task 11 ✅ 已完成（2026-08-18）：拆分页面用户资料/部门/角色 API 和 schema，收口 create/import/fixed R_USER 例外，页面与 AI 共用替换 Policy。
  - [x] Task 11a ✅ P2-B1 已完成（2026-08-17；审查修复 2026-08-17）：资料/角色请求契约拆分，`roleIds` 固定为 canonical Snowflake `string[]` 且显式 null 失败，资料更新拒绝 password；新增角色完整替换与最小候选 API。页面 create、AI create 和 import 新建使用同一角色 Policy，显式 `roleIds`/角色列表头叠加 role-auth，缺省角色固定为受 dominance 约束的 `R_USER`。import preview/execute 共用 dominance，execute 锁内重验入口权限、冻结并锁后重查 role/dept/user 解析，任一授权越界整批零业务写入。定向回归 126 项、后端全量 2028 项和 73.57% 覆盖率门禁通过；数据库 schema、seed 和 Web 均无变化。
  - [x] Task 11b ✅ 已完成（2026-08-18）：交付独立部门 writer，并把 import overwrite 与其余存量角色/部门 writer 全部接入共享 Policy；完成 Web 契约切换。
    - [x] Task 11b-1 ✅ P2-B2 已完成（2026-08-17；审查修复 2026-08-17）：页面部门完整替换 contract/API/Service、双权限、主部门规则、完整 scope、授权影响 dominance、全局锁和锁后复验完成；主部门策略改为 uncached 锁行读取，候选作用域精确重建目标主体，并补齐隐藏旧关联与 admin/R_SUPER 回归。相关 48 项、system 539 项及后端全量 2053 项通过，覆盖率 73.81%，无 migration/seed/Web 改动。
    - [x] Task 11b-2 ✅ P2-B3 已完成（2026-08-18；审查修复 2026-08-18）：页面 create、AI create、import create/overwrite 与部门成员页统一使用共享角色/部门 Policy；删除旧 `DeptService` 关联 writer，导入拒绝重复目标并锁定 `DEPT_AND_SUB` 完整后代，部门成员完整集合在全局锁内批量物化且比较完整授权快照，隐藏旧成员、主部门移除和任一越权均整批失败。Web 完成独立 writer、部分提交恢复、最小可委派角色和 fail-closed 分页成员契约。无 migration/seed；最终验证证据见主基线。
- [ ] Task 12：纠正 data-scoped `user.dept_lookup`，实现 `user.update_dept` 的完整旧/新集合、前后授权影响、dry-run、i18n 与批准时复验。
- [ ] Task 13：同步实现 `user.role_lookup` 与 `user.update_roles` 的最终有效授权 dominance、dry-run、i18n 与批准时复验。
- [ ] Task 14：后端定向/全量门禁与真实浏览器多角色 E2E 通过后，将 Plan 2 翻转为完成。
