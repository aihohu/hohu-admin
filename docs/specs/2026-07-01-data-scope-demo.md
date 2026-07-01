# 数据权限演示页（data-scope-demo）

> 状态：✅ Plan 已完成（2026-07-01）
>
> 通过新建一张业务表演示 5 种 data_scope 的实际过滤效果。用真登录机制（不同
> 账号登录看不同数据），不引入"切换身份"端点。

## 背景

后端 RBAC 数据权限（5 种 scope + 优先级算法 + 过滤器）测试覆盖完善，但
**没有任何业务表的端点真的应用 data_scope 过滤**——`get_user_data_scope_filters`
只用在 `/system/user/list` 一个端点。新贡献者/客户/管理员无法直观感受"配了
不同 scope 看到的数据差异"。

本次新增一个演示页：新建一张业务表 `sys_data_scope_demo`，标准的 CRUD 端点
真应用 `get_data_scope_filters`，配 seed 脚本预设 5 种 scope 各一个用户，
演示者用不同账号登录 admin-web 看效果。

---

## 决策记录

### 1. **独立建模新表演示（而非用 sys_user/sys_dept）** —
让 data_scope 真作用于一份"业务数据"。sys_user 走 `get_user_data_scope_filters`
（多对多部门关联子查询），sys_dept/sys_role 没有 dept_id 字段（无法被过滤）。
独立表 `sys_data_scope_demo` 字段简单可控，能完整演示 5 种 scope。**反例**：
用 sys_user 表演示——但要造跨部门用户分布，污染生产账号；用 sys_dept——dept
表自己过滤自己，scope 无意义。**回归**：测试 `test_data_scope_demo_service.py`
8 个 case 覆盖 5 种 scope + CUSTOM 排除禁用 + admin 短路 + create 注入。

### 2. **`create_by` 用 BigInteger 存 user_id** —
`app/utils/data_scope.py` 的 `user_field` 默认是 `"create_by"`，过滤逻辑为
`user_col == user.user_id`（int）。sys_dept/sys_role 等表用 String(32) 存
user_name 字符串，与 data_scope 契约不一致。本表刻意用 BigInteger 存 ID 让
SELF scope 可比。**反例**：用 String 存 user_name，SELF scope 比较 `int == str`
永远不匹配，所有 SELF 用户看不到任何数据。**回归**：`test_sees_only_own_created`
验证 SELF 用户能看到自己 user_id 创建的数据。

### 3. **create 端点强制从 current_user 注入 dept_id/create_by** —
schema `DataScopeDemoCreate` 不接受 `dept_id` / `create_by` 字段，service
层从 `current_user` 主部门（`user_depts.is_primary='Y'`）取 dept_id，从
`current_user.user_id` 取 create_by。**反例**：前端能传 `dept_id` 字段，
恶意用户可伪造 dept_id 绕过自己 scope 限制往别的部门写数据。**回归**：
`test_uses_primary_dept_and_user_id` 验证 create 后 demo.dept_id 是
current_user 主部门、demo.create_by 是 current_user.user_id。

### 4. **不做"切换身份/试配/预览"端点** —
原 plan agent 提议 `/preview`（超管预览别人）+ `/my-view`（自查）+ override
参数。用户明确要求"用不同账号登录"的真实场景，所以全部砍掉，仅用现有登录机制。
**反例**：引入 preview 端点要绕过 `is_super_admin` 短路、需要 override 语义、
新增 `scope_source: "user_roles" | "manual_override"` 字段，复杂度大幅增加
且偏离"真实业务流"的教学目的。**回归**：无新端点，仅标准 CRUD；演示效果靠
seed 5 个用户 + admin-web 现有登录。

### 5. **seed ID 用 8 亿段（800000001-800000205）** —
避开测试 fixture 用过的所有 ID 段：`test_role_service_data_scope.py` 用
900000001-900000020，`test_data_scope_dept.py` 用 900000123/900001234 等，
本 demo 测试 fixture 用 1001-1006（dept）+ 5001-5005（role）+ 9001（admin）+
8000-8606（demo_id）。**反例**：seed 用 900000001，与旧测试 dept_id 冲突，
SAVEPOINT 回滚前 SELECT 看到 seed 写入的真数据，触发 `sys_dept_pkey` 唯一约束。
**回归**：`pytest` 全量 330 通过；seed 脚本 idempotent（按 user_name/role_code/
dept_id 检查存在），重跑安全。

### 6. **ALL/super_admin 测试用 `issubset` 而非精确等于** —
seed 数据已 commit 到库（30 条 `演示数据-*`），测试 fixture 再创建几条后，
ALL scope 用户和 admin 看到的是 seed 数据 + fixture 数据的合集。**反例**：
`assert ids == {8000,8001,8002,8003,8004}` 永远失败（实际 36 条）。
**回归**：`test_super_admin_sees_all` 和 `test_sees_everything` 改用
`assert my_ids.issubset(ids)`，只验证"我创建的都可见"，不验证精确总数。

### 7. **菜单 route_name 用 kebab（`system_data-scope-demo`）** —
@elegant-router 根据前端目录 `data-scope-demo/index.vue` 生成 route name
`system_data-scope-demo`（保留连字符）。后端 `sync_menus.py` 的 route_name
必须用相同 kebab，否则后端 menu 表找不到对应前端路由，前端侧边栏空白。
**反例**：后端 route_name 写 snake `system_data_scope_demo`，前端路由名是
kebab，前端 `asyncRoutes.find(r => r.name === backendRouteName)` 匹配失败。
**回归**：清理 DB 旧 snake 记录后重跑 sync_menus，前端 `/system/data-scope-demo`
路由正常渲染。

### 8. **i18n 文本里不能含 `@`** —
vue-i18n 把消息文本里的 `@` 当作 linked message 语法（`@:key` 引用其他 key）
解析。`demoAccountHint` 原文 `演示账号（密码统一 demo@12345）：...` 触发
`Invalid linked format` 编译错误。**反例**：i18n 文本写 `'密码：demo@12345'`，
页面加载时 vue-i18n 编译失败，整个 NAlert 渲染崩溃。**回归**：把密码
`demo@12345` 移到模板里直接渲染（不走 i18n 编译器），i18n key 只说"见下方
标签"。NTag 里的 `demo@12345` 是模板插值文本，不经过 vue-i18n 解析，所以
那个 `@` 不会触发错误。

### 9. **菜单 icon 用 `carbon:security`（不用 `carbon:shield-account`）** —
iconify 的 carbon 图标集没有 `shield-account` 系列命名。前端按 icon 名查
carbon 集找不到对应 SVG，渲染为空白。**反例**：icon 写 `carbon:shield-account`，
侧边栏菜单图标位置空白。**回归**：用 `carbon:security`（carbon 集确认存在）
直接 SQL UPDATE menu 表 icon 字段 + 改 sync_menus.py 源。

---

## 已接入模块清单

| 文件 | 作用 |
|---|---|
| `app/modules/system/models/data_scope_demo.py` | 新表 `sys_data_scope_demo` ORM |
| `app/modules/system/schemas/data_scope_demo.py` | Pydantic schemas（Create/Update/Query/Out）|
| `app/modules/system/service/data_scope_demo_service.py` | Service 单例，`get_list` 应用 `get_data_scope_filters` |
| `app/modules/system/api/data_scope_demo.py` | 4 端点：list/add/{id}/{id}，按钮权限 `system:data-scope-demo:*` |
| `alembic/versions/bf244f9a8b76_add_sys_data_scope_demo_table.py` | 迁移 |
| `scripts/seed_demo_data_scope.py` | idempotent seed：6 部门 + 5 角色 + 5 用户 + 30 数据 |
| `scripts/sync_menus.py` | 追加菜单 + 4 按钮权限码 |
| `tests/modules/system/test_data_scope_demo_service.py` | 8 个测试覆盖 5 种 scope |
| 前端 `src/views/system/data-scope-demo/` | 演示页面（index.vue + 2 子组件）|
| 前端 `src/service/api/system.ts` | 4 个 fetch 函数 |
| 前端 `src/typings/api/system-manage.d.ts` | `DataScopeDemo` namespace |
| 前端 `src/locales/langs/{zh-cn,en-us}.ts` + `app.d.ts` | i18n schema |

## 演示账号（密码统一 `demo@12345`）

| 账号 | 角色 data_scope | 主部门 | 预期可见 |
|---|---|---|---|
| `admin` | R_SUPER | — | 全部 30 条（is_super_admin 短路）|
| `demo_all` | ALL=1 | TEAM_A1 | 全部 30 条 |
| `demo_dept_sub` | DEPT_AND_SUB=4 | BRANCH_A | BRANCH_A 及子部门数据 |
| `demo_dept` | DEPT=3 | BRANCH_A | 仅 BRANCH_A 部门数据（不含子）|
| `demo_custom` | CUSTOM=2 | BRANCH_B | role_depts 配置的 TEAM_A1+TEAM_B1 数据 |
| `demo_self` | SELF=5 | TEAM_A1 | 仅 demo_self 自己 create_by 的数据 |

## 验证步骤

```bash
cd hohu-admin
uv run alembic upgrade head                          # 建表
uv run python scripts/seed_demo_data_scope.py        # 灌数据
uv run python scripts/sync_menus.py                  # 同步菜单
uv run pytest tests/modules/system/test_data_scope_demo_service.py -v  # 8 测试

cd ../hohu-admin-web
pnpm dev                                              # 启动前端
# 浏览器 http://localhost:9527 用各 demo_* 账号分别登录验证
```

## 参考借鉴

- 数据权限核心实现：[`docs/data-scope-guide.md`](../data-scope-guide.md)
- 数据策略未来设计：[`docs/specs/2026-04-29-data-policy-design.md`](./2026-04-29-data-policy-design.md)
- 按钮权限指南：[`docs/button-permission-guide.md`](../button-permission-guide.md)
- 标杆 spec：[`docs/APP-MARKETPLACE.md`](../APP-MARKETPLACE.md)
