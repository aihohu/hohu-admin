# 按钮级权限（Button Permission）使用指南

本文档说明 hohu-admin 中**按钮级权限**的完整链路、命名规范、前后端用法，以及新增权限码的流程。

> 阅读前可先看 [数据权限指南](./data-scope-guide.md) 区分两个维度：数据权限控制「能看到哪些行」，按钮权限控制「能调用哪些操作」。

---

## 一、核心概念

按钮级权限基于 RBAC 三层模型（User → Role → Menu）扩展，由 F 类型菜单（按钮）承载：

| 概念 | 说明 | 位置 |
|---|---|---|
| **权限码（permission code）** | 业务字符串标识，如 `system:role:add` | `sys_menu.permission` 字段 |
| **F 类型菜单** | `menu_type='F'` 的菜单行，承载权限码 | `sys_menu` 表 |
| **角色关联** | 角色通过 `sys_role_menu` 关联 F 菜单 = 拥有该权限 | `sys_role_menu` 表 |
| **超管旁路** | `user_name == "admin"` 或角色含 `R_SUPER` → 直接通过所有检查 | `app/core/auth.py:is_super_admin` |

### 完整链路

```
sync_menus.py 种子 / 菜单管理 UI 维护
  ↓
sys_menu (F 类型行，含 permission 字段)
  ↓
sys_role_menu 关联（管理员在「角色权限」页勾选）
  ↓
用户登录 → get_current_user() 通过 role.menus 加载所有关联菜单
  ↓
/auth/getUserInfo 遍历 role.menus 提取 permission → 返回 buttons: [...]
  ↓ (超管走捷径：buttons=["*"])
前端 userInfo.buttons → v-permission / hasAuth 控制按钮显隐
  ↓
后端 require_permissions("xxx") 独立校验（防绕过）
```

---

## 二、命名规范

格式：`{module}:{resource}:{action}` —— 全小写，冒号分隔。

| 模块 | 示例 |
|---|---|
| system | `system:role:list` / `system:user:reset-password` / `system:config:export` |
| ai | `ai:provider:test-model` / `ai:conversation:clear` |
| marketplace | `marketplace:app:install` / `marketplace:review:approve` |

**约定俗成的 action 后缀**：`list / add / edit / delete / batch-delete / export / import / run / clean / menu-auth`。

⚠️ **前后端权限码字符串必须完全一致**。任一端拼写错误都会让权限控制静默失效。

---

## 三、后端用法

### 3.1 接口接权限校验（必做）

每个写/删/改接口**必须**接 `require_permissions`。仅靠前端按钮显隐**不够**——用户可绕过 UI 直接调 API。

```python
from app.core.auth import require_permissions

# 单权限码
@router.post(
    "/add",
    dependencies=[Depends(require_permissions("system:role:add"))],
)
async def add_role(...):
    ...

# 仅超管
@router.delete(
    "/{role_id}",
    dependencies=[Depends(require_permissions(super_admin_only=True))],
)
async def delete_role(...):
    ...
```

**注意 `dependencies=[Depends(...)]` 的写法**：用 `dependencies` 参数声明副作用依赖，而不是 `_current_user: User = Depends(require_permissions(...))`。后者把校验结果当入参绑定，容易和真正的 `get_current_user` 依赖重复。

查询接口（list/detail）按业务需求决定是否接权限码（多数场景应接 `*:list`）。

### 3.2 关键文件

| 文件 | 作用 |
|---|---|
| `app/core/auth.py` | `require_permissions()` 装饰器、`is_super_admin()` |
| `app/modules/auth/api.py` | `/auth/getUserInfo` 下发 `buttons` |
| `app/modules/system/models/menu.py` | `Menu.permission` 字段 |
| `app/db/base.py` | `sys_role_menu` 关联表（含 `ondelete=CASCADE`） |
| `scripts/sync_menus.py` | 菜单 + F 按钮权限码的 seed |

---

## 四、前端用法

前端提供**三层 API**，按场景选择：

### 4.1 `v-permission` 指令（首选，全局可用）

```vue
<NButton v-permission="'system:role:add'">新增</NButton>
<NButton v-permission="['system:role:add', 'system:role:edit']">操作</NButton>
```

实现要点（`src/directives/permission.ts`）：
- 用 `watchEffect` 订阅 `userInfo.buttons`：登录后 buttons 异步加载能自动更新显隐
- 用 `display: none` 而非 `removeChild`：vNode 仍留在 slot 数组中，**不会触发 Vue 的 slot fallback**

### 4.2 `hasAuth` 函数（TSX render 用）

```tsx
import { useAuth } from '@/hooks/business/auth';
const { hasAuth } = useAuth();

// 在 columns render 里：
{hasAuth('system:role:edit') && <NButton>编辑</NButton>}
```

### 4.3 `TableHeaderOperation` 组件 props（列表页顶部）

```vue
<TableHeaderOperation
  add-auth="system:role:add"
  delete-auth="system:role:batch-delete"
  @add="handleAdd"
  @delete="handleBatchDelete"
  @refresh="getData"
/>
```

不传 `add-auth` / `delete-auth` 时按钮始终显示（向后兼容）。**比 slot + `v-if` 更可靠**（见下文踩坑）。

**完整 props**：

| Prop | 类型 | 默认 | 说明 |
|---|---|---|---|
| `showAdd` | boolean | `true` | 是否显示新增按钮。日志页等不能新增的场景设 `false` |
| `showDelete` | boolean | `true` | 是否显示批量删除按钮。无批量删除功能的页面设 `false` |
| `addAuth` | string | — | 新增按钮权限码。不传则不控制权限 |
| `deleteAuth` | string | — | 批量删除按钮权限码。不传则不控制权限 |
| `disabledDelete` | boolean | `false` | 批量删除按钮 disabled 状态（如未勾选行时） |

显隐逻辑：`显示 = showXxx && (!auth || hasAuth(auth))`。

**常见组合**：

```vue
<!-- 标准 CRUD 页：新增 + 批量删除都按权限 -->
<TableHeaderOperation add-auth="xxx:add" delete-auth="xxx:batch-delete" ... />

<!-- 日志页（不能新增，只有批量删除）-->
<TableHeaderOperation :show-add="false" delete-auth="xxx:delete" ... />

<!-- Provider 页（无批量删除功能，避免 dead UI）-->
<TableHeaderOperation add-auth="xxx:add" :show-delete="false" ... />
```

### 4.4 关键文件

| 文件 | 作用 |
|---|---|
| `src/directives/permission.ts` | `v-permission` 指令实现 |
| `src/directives/index.ts` | `setupDirectives(app)` 全局注册 |
| `src/hooks/business/auth.ts` | `useAuth()` / `hasAuth()` |
| `src/store/modules/auth/index.ts` | `userInfo.buttons` 存储 |
| `src/components/advanced/table-header-operation.vue` | 顶部操作栏组件 |

---

## 五、新增按钮权限码流程

以「给角色管理加一个 `system:role:menu-auth`（菜单权限）按钮」为例：

1. **后端 seed**（`scripts/sync_menus.py` 的 `MENU_DEFINITIONS`）：

   ```python
   {
       "key": "system_role_menu-auth",        # 唯一 key
       "parent_route": "system_role",          # 父菜单 route_name
       "menu_name": "菜单权限",                # UI 显示名
       "menu_type": "F",
       "permission": "system:role:menu-auth",  # 权限码
       "route_path": "",
       "status": "1",
   },
   ```

2. **执行同步**：`python scripts/sync_menus.py`（按 `permission` 去重，安全重复执行）

3. **后端接口接权限**：

   ```python
   @router.put(
       "/menu/{role_id}",
       dependencies=[Depends(require_permissions("system:role:menu-auth"))],
   )
   async def update_role_menu(...):
       ...
   ```

4. **前端控制显隐**：

   ```vue
   <!-- 简单按钮 -->
   <NButton v-permission="'system:role:menu-auth'">菜单权限</NButton>

   <!-- 或 TSX render -->
   {hasAuth('system:role:menu-auth') && <NButton>菜单权限</NButton>}

   <!-- 或 TableHeaderOperation 顶部按钮 -->
   <TableHeaderOperation add-auth="system:role:add" delete-auth="system:role:batch-delete" ... />
   ```

---

## 六、踩坑提醒

### 6.1 Slot 场景不要用 `v-if hasAuth`

❌ **错误**：

```vue
<TableHeaderOperation>
  <NButton v-if="hasAuth('system:role:add')">新增</NButton>
  <NPopconfirm v-if="hasAuth('system:role:batch-delete')">...</NPopconfirm>
</TableHeaderOperation>
```

**原因**：当 slot 子节点全部 `v-if=false`（只剩注释节点）时，Vue 会判定 slot 为空 → 触发 `TableHeaderOperation` 的默认 slot fallback → 渲染组件内部**没有权限控制的默认按钮**。

✅ **正确**：

```vue
<TableHeaderOperation
  add-auth="system:role:add"
  delete-auth="system:role:batch-delete"
/>
```

或在其他位置用 `v-permission`（`display:none` 不触发 fallback）。

### 6.2 TSX 中用 `hasAuth` 而非 `v-permission`

指令在 TSX 中需要 `withDirectives` wrapper，写法繁琐。直接用 `hasAuth('xxx')` 条件渲染更简洁。

### 6.3 编辑菜单时不能改 button code

`menu_service.update_menu` 按 `permission` 业务键增量更新（避免删除-重建破坏 `role_menus` 关联）。前端 `menu-operate-modal.vue` 在编辑模式下已禁用 `value.code` 输入框。如需"改名"权限码：删旧按钮 + 加新按钮 + 重新配角色。

### 6.4 前端控制只是 UX，后端必须独立校验

任何用户都能用 curl / Postman 绕过前端 UI 直接调 API。**所有写/删/改接口必须接 `require_permissions`**，前端权限控制只是体验优化。

### 6.5 不要传 `:show-add="false"` 之外还传 `add-auth`

`showAdd=false` 已经把按钮隐藏了，再传 `addAuth` 是冗余。两者关系：先按 `showXxx` 决定是否显示，再按 `auth` 决定权限。`showXxx=false` 直接跳过权限判断。

### 6.6 模块前缀不统一（历史遗留）

仓库内权限码前缀不统一：
- `system:*` — system 模块（role/menu/user/dept/dict/file/config/job）
- `monitor:*` — 日志类（operation-log/login-log）
- `ai:*` — AI 模块（chat/conversation/provider）
- `marketplace:*` — 应用市场

新增模块按业务域选前缀，**不要硬塞 `system:`**。前缀不一致不影响功能，但写代码时要注意对应的 hasAuth 字符串别拼错。

---

## 七、特殊模块设计模式

不同业务场景下，权限控制有几种典型模式：

### 7.1 标准 CRUD 模式（role/menu/user/dept/dict/config）

- 所有 CRUD 接口都接权限码
- 前端顶部 TableHeaderOperation 用 `add-auth` + `delete-auth`
- 行内 edit/delete 用 `hasAuth`（TSX）
- 权限码：`{module}:list/add/edit/delete/batch-delete`

### 7.2 业务通用接口不接权限（dict_data/type、provider/models、file/upload）

部分接口是「业务下拉/通用上传」，**任何登录用户都可能用到**，不接权限码。否则会卡业务。

| 模块 | 业务通用接口（不接） | 管理类接口（接权限） |
|---|---|---|
| dict_type | `/all`（下拉） | list/add/edit/delete/batch-delete |
| dict_data | `/type/{dict_type}`（按类型查） | list/add/edit/delete/batch-delete |
| ai/provider | `/models`（chat 页选模型） | list/add/edit/delete/test-model |
| file | `/upload`、`/batch-upload`、`/{id}` GET | list/delete/batch-delete |
| menu | `/tree-option`（角色权限页用） | list/add/edit/delete/batch-delete |
| dept | `/tree`、`/tree-option`（下拉） | list/add/edit/delete/batch-delete |
| config | `/public`（无需鉴权） | list/add/edit/delete/batch-delete/export/import |

业务侧调用通用接口时，**业务模块自己控权限**。例如 CRM「添加客户附件」按钮，前端用 `v-permission="'crm:customer:edit'"`，CRM 后端接口接 `require_permissions("crm:customer:edit")`，file/upload 本身不接。

### 7.3 用户私有数据模式（ai/conversation）

会话是用户私有数据，**service 层按 user_id 隔离**，不需要权限码：

```python
async def get_list(self, db, query, user_id: int):
    # 自动加 WHERE user_id = ?
    ...
```

用户只能 CRUD 自己的数据，无需权限码控制。注意：service 必须强制传 user_id，**不能漏**。

### 7.4 ownership + 管理员旁路模式（file）

文件是通用服务（业务方上传），但删除要防越权。设计：

| 操作 | 权限模型 |
|---|---|
| 上传 | 不接权限码（业务通用） |
| 单删 `DELETE /{id}` | 不接权限码 + service 检查 ownership（普通用户只能删自己上传的，超管旁路） |
| 批删 `POST /batch-delete` | 接 `system:file:delete` 权限码（管理员批量） |
| 列表 | 接 `system:file:list`（管理员视角） |

```python
# file_service.delete
async def delete(self, db, file_id, current_user=None, is_admin=False):
    file_record = await self.get_by_id(db, file_id)
    if not is_admin and current_user is not None:
        if file_record.create_by != current_user.user_name:
            raise AuthorizationException("权限不足", error_code="FILE_OWNERSHIP_REQUIRED")
    ...
```

API 层调用时传 `is_admin=is_super_admin(current_user)`。

### 7.5 监控/审计类（operation_log / login_log）

只读 + 清理 + 批量删除。无新增/编辑。

```vue
<TableHeaderOperation :show-add="false" delete-auth="monitor:operation-log:delete" ... />
<NPopconfirm v-permission="'monitor:operation-log:clean'" ...>清理</NPopconfirm>
```

行内只有「查看详情」按钮（不接权限，任何能进页面的用户都能看）。

### 7.6 混合模式（marketplace）

一个模块内同时存在多种模式，按子场景分别处理：

| 子场景 | 模式 | 权限码 |
|---|---|---|
| 浏览市场（list/search/detail/manifest） | 业务通用 | 不接 |
| 安装/卸载/启用/停用 | 标准 CRUD | `marketplace:install` |
| 应用审核（admin.py 的 list/detail/approve/reject） | 管理员视角 | `marketplace:review` |
| 评分（rating CRUD） | 用户私有（user_id） | 不接 |
| lowcode 应用数据（app_data.py） | tenant 隔离 | 不接 |
| 前端初始化（contributes.py） | 业务通用 | 不接 |

要点：
- **不要为了"统一"给所有接口都加权限码**。浏览类接口加了反而卡业务（普通用户逛不了市场）。
- **管理员操作（审核）单独权限码**，跟普通用户操作（安装）分开。
- **跨文件模块**注意路由 prefix（marketplace.py 在 `/marketplace`，admin.py 在 `/marketplace/admin`）。

---

## 八、全仓权限码矩阵

按模块整理当前已接入的权限码（截至 2026-07-01）：

| 模块 | 前缀 | 权限码 | sync_menus |
|---|---|---|---|
| role | `system:` | list/add/edit/delete/batch-delete/menu-auth | ✅ |
| menu | `system:` | list/add/edit/delete/batch-delete | ✅ |
| user | `system:` | list/add/edit/delete/batch-delete/reset-password | ✅ |
| dept | `system:` | list/add/edit/delete/batch-delete | ✅ |
| dict_type | `system:dict-type:` | list/add/edit/delete/batch-delete | ✅ |
| dict_data | `system:dict-data:` | list/add/edit/delete/batch-delete | ✅ |
| config | `system:` | list/add/edit/delete/batch-delete/export/import | ✅ |
| job | `system:` | list/add/edit/delete/batch-delete/run | ✅ |
| job_log | `system:job-log:` | list/clean/batch-delete | ✅ |
| file | `system:` | list/upload/delete（含 ownership 检查） | ✅ |
| operation_log | `monitor:operation-log:` | list/clean/delete | ✅ |
| login_log | `monitor:login-log:` | list/clean/delete | ✅ |
| ai/provider | `ai:provider:` | list/add/edit/delete/test-model | ✅ |
| ai/chat | — | 不接（业务通用） | — |
| ai/conversation | — | 不接（user_id 隔离） | — |
| marketplace（浏览） | — | apps list/search/detail/manifest 不接（业务通用） | — |
| marketplace:install | `marketplace:` | install（安装/卸载/启用/停用） | ✅ |
| marketplace:review | `marketplace:` | review（审核 list/detail/approve/reject，在 admin.py） | ✅ |
| marketplace app_data | — | 不接（lowcode 应用数据，tenant 隔离） | — |
| marketplace contributes | — | 不接（前端初始化加载） | — |

新增模块时，按本表格式补一行。

---

## 九、参考

- 调研报告与 bug 修复记录：见 git log（`menu_service` / `role_service` / `menu-auth-modal` / `v-permission` 关键词）
- 相关模块：`src/views/system/role/`（按钮权限样板页面）
- 关联规范：[数据权限指南](./data-scope-guide.md)（行级数据过滤）、[分页查询指南](./pagination-guide.md)
