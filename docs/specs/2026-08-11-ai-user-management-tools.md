# AI 用户管理缺失工具补齐

> 状态：✅ Plan 已完成（2026-08-11）  
> 日期：2026-08-11  
> 关联：[`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md)、[`2026-08-01-user-import-export-design.md`](./2026-08-01-user-import-export-design.md)

## 1. 背景与范围

当前 `user_mgmt` 已注册 `user.count/stats/distinct/list/lookup/update/batch_delete/import_preview/import_execute/export`，但 Gateway 内置 Agent 清单承诺的 `user.create` 与 `user.reset_password` 未实现。旧的导入导出 spec 还把 `user.create` 错标为已完成，造成设计与运行时 Registry 漂移。

本次只补齐这两个已承诺工具。`user.update_dept` / `user.update_roles` 仍按原 Task 25a+ 作为独立后续能力，不混入本次纠偏。

## 2. 决策记录

1. **只补 `user.create` 与 `user.reset_password`** — 它们已经出现在 Gateway §10.1 的 `user_mgmt` MVP 清单，而部门和角色调整在现有实现中明确标记为 Task 25a+。**反例**: 因“用户管理工具”字面含义一次性加入所有设想工具，会绕过各自的权限边界、data scope 与交互设计。**回归**: Registry 精确新增两个名称；静态内置工具清单从 16 增至 18；旧 spec 完整度表纠正。

2. **密码由后端私有配置生成，不进入工具签名和返回值** — 两个工具统一读取 `sys_config.auth:default_password`，在业务层哈希后写库；`AiToolMeta.sensitive_input` 声明密码，但函数签名、dry-run、`ToolResult.data`、`UIResult`、审计摘要均不包含密码值。管理员按现有 V1 策略从私有配置获知默认密码并线下通知用户。**反例**: 让 LLM 接收新密码，或把临时密码放入 result/UI，会让明文进入模型上下文、消息持久化或浏览器状态。**回归**: 签名反射与序列化测试断言没有 `password/new_password/hashed_password`；静态 sensitive-input gate 通过。

3. **新建用户使用既有普通用户角色 `USER_ROLE_CODE=R_USER`，AI 不接收角色参数** — `R_USER` 是现有数据库、认证契约与测试长期使用的普通用户角色编码；工具只查找并绑定该启用角色，fresh install 也按同一编码 seed。角色属于权限边界，不能由自然语言直接提升。**反例**: 沿用未被业务使用的旧常量值 `user` 会在已部署数据库误报默认角色不存在；暴露 `roles=["R_SUPER"]` 给模型则会把“创建账号”变成越权提权入口。**回归**: 常量与初始化种子测试锁定 `R_USER`；创建结果只绑定 `USER_ROLE_CODE`；默认角色缺失或禁用时返回稳定业务错误。

4. **AI 新建必须指定一个主部门并同时校验 data scope** — `primary_dept_id` 是创建工具必填参数；执行和 dry-run 都调用 `ensure_targets_in_scope(ctx, dept_ids=[...])`，随后复用 `dept_service.update_user_depts` 建立主部门关系。**反例**: 允许无部门创建，非全量 data-scope 操作者可能创建出自己不可见、无法继续治理的账号。**回归**: 越界部门在 dry-run/执行两条路径均被拒绝；成功用户只有一个主部门。

5. **两个工具都强制 HITL 且支持 dry-run** — 创建账号和使旧密码立即失效均为高风险写操作，确认抽屉在执行前显示目标账号、主部门或密码重置影响，但不显示敏感值。**反例**: 让 balanced agent 自动执行单行创建/重置，会把模型误判直接变成账号或凭据变更。**回归**: `risk="high"`、`hitl_always=True`、`dry_run_supported=True`，并存在同模块命名约定 dry-run hook。

6. **重置密码禁止操作当前登录账号，所有超级管理员目标仅允许超管操作** — 当前账号应走需要旧密码的个人改密流程；用户名 `admin` 或绑定启用 `R_SUPER` 的账号均属于高权限目标，普通委派管理员不得借 AI 重置。**反例**: 当前用户误重置自己会让当前会话与凭据状态割裂；普通管理员重置任一超级管理员可造成接管。**回归**: self-target 返回稳定错误；非超管 targeting `admin` 或其他 `R_SUPER` 均被拒；data-scope 校验仍先于业务写入。

7. **Tool adapter 只编排，事务仍由 Gateway 管理** — 工具复用 `user_service.create_user/reset_password` 与 `dept_service.update_user_depts`，只允许 `flush` 获取 ID，不在 service 或 tool 内 `commit`。**反例**: 工具自行 commit 会破坏 Gateway 的审计、失败回滚和 durable finalizer 原子性。**回归**: 单测用 session 验证结果可回滚；代码静态检查无 `commit()`。

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

## 3. 工具契约

### `user.create`

- 权限：`system:user:add`
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
