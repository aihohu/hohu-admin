# Hohu 插件市场架构设计

> 状态：架构讨论阶段 | 创建日期：2026-05-29

## 1. 核心抽象：什么是"插件"

统一抽象为 Plugin = Manifest + 一组可部署的组件。

```
Plugin = Manifest + 一组可部署的组件

组件类型：
├── backend    → FastAPI Router + Service + Model + Migration
├── frontend   → Vue 页面/组件 + 路由注册
├── lowcode    → JSON Schema（表单/列表/流程定义）
└── theme      → 样式/布局定制
```

一个插件可以包含一个或多个组件。例如：
- CRM 插件 = 1 个 backend + 1 个 frontend
- 数据大屏插件 = 1 个 lowcode
- 通知插件 = 1 个 backend（纯后端）

## 2. 插件生命周期

```
开发 → 打包 → 上传/发布 → 审核 → 上架 → 安装 → 配置 → 启用/禁用 → 升级 → 卸载
```

### 2.1 生命周期钩子

借鉴 Odoo（pre_init_hook/post_init_hook/uninstall_hook）和 WordPress（activation/deactivation/uninstall hooks）的成熟模式，每个阶段提供回调点：

| 钩子 | 触发时机 | 用途 | 适用类型 |
|------|----------|------|----------|
| `pre_install` | 安装前（建表前） | 检查前置条件、预留资源 | all |
| `post_install` | 安装后（建表后） | 初始化种子数据、注册定时任务 | all |
| `on_enable` | 启用时 | 启动后台服务、注册事件监听 | backend |
| `on_disable` | 禁用时 | 停止后台服务、清理临时资源 | backend |
| `pre_upgrade` | 升级前 | 数据备份、兼容性检查 | all |
| `post_upgrade` | 升级后（Schema 迁移后） | 数据转换、缓存刷新 | all |
| `pre_uninstall` | 卸载前（删表前） | 数据导出、通知依赖插件 | all |
| `post_uninstall` | 卸载后 | 清理配置、释放权限 | all |

**低代码插件**（Phase 1）：钩子在 manifest 中声明为 JSON 配置，由主系统内置执行器处理，不需要自定义代码：

```jsonc
{
  "hooks": {
    "post_install": { "seed": true },          // 自动填充 data_schema 中的 default 值
    "pre_uninstall": { "export": true }         // 自动导出数据为 JSON 备份
  }
}
```

**后端/全栈插件**（Phase 3）：钩子指向插件容器内的 HTTP 端点：

```jsonc
{
  "hooks": {
    "post_install": { "endpoint": "POST /internal/hooks/post-install" },
    "on_enable": { "endpoint": "POST /internal/hooks/on-enable" },
    "pre_uninstall": { "endpoint": "POST /internal/hooks/pre-uninstall", "timeout": 30 }
  }
}
```

### 2.2 关键决策点

- **谁可以发布？** 完全开放市场，任何第三方开发者均可提交，需走审核流程
- **安装粒度** — 当前系统级（管理员装一次全局可用），数据模型预留 `tenant_id` 支持未来多租户
- **升级策略** — 插件有独立的数据库迁移，通过 `pre_upgrade`/`post_upgrade` 钩子协调
- **依赖解析** — 版本范围采用 npm-like semver range（`^1.0.0`、`>=1.0.0 <2.0.0`），安装时做拓扑排序 + 循环依赖检测，循环依赖拒绝安装
- **依赖卸载** — 卸载插件时检查是否有其他已安装插件依赖它，若有则阻止卸载并提示"以下插件依赖此插件：{列表}"

## 3. 分发模式（双轨制）

| 角色 | 流程 | 看到的内容 |
|------|------|-----------|
| **开发者** | 本地开发 → 打包 → 提交到市场（CLI 或 API） | 开发者中心：我的插件、版本管理、下载统计 |
| **管理员/用户** | 浏览市场 → 一键安装 → 配置 → 启用 | 应用市场：分类浏览、搜索、评分、安装 |

## 4. 后端插件化架构

### 路径 A — 进程内动态加载（类 Django/Ninja 插件）

- 插件作为 Python package，运行时 `importlib` 加载
- 插件注册 FastAPI router 到主 app
- 优点：性能好，共享 DB 连接
- 缺点：插件崩溃影响全局，安全性难保证

### 路径 B — 独立进程 + 通信（类 WordPress 微服务模式）

- 每个插件是独立进程/容器
- 通过 HTTP/gRPC/MCP 与主系统通信
- 优点：隔离性好，可独立扩缩容
- 缺点：部署复杂，延迟增加

### 决策：路径 B（独立进程/容器隔离）

由于插件市场完全开放，必须支持不可信第三方插件，后端插件采用独立进程/容器 + API 网关代理模式。

- Phase 3 才引入后端插件（Phase 1 低代码不需要执行代码，天然安全）
- 插件通过 Docker 容器或受限子进程运行
- 主系统通过 API 网关（或 MCP）代理插件路由
- 插件不直接访问主数据库，通过标准 API 通信
- **网关上下文注入**：API 网关在验证 JWT 后，将用户身份（User-ID、Tenant-ID、Roles）以 `X-Hohu-*` 自定义 Header 透传给插件容器。插件不持有原始 JWT，仅通过 `X-Hohu-*` Header 获取当前操作者身份
- **容器依赖隔离**：插件容器的第三方 Python 依赖必须封装在自身的 Docker 镜像内，主系统进程绝不通过 pip 安装任何插件的依赖，从根本上避免依赖地狱

## 5. 前端插件化架构

### 5.1 架构总览

借鉴 VS Code 的 `contributes` 声明式注册 + IntelliJ 的 Extension Points 双向扩展模式：

```
主应用（Shell）
├── Plugin Registry（插件注册表，全局单例）
│   ├── 声明式贡献解析（读 manifest，不加载代码即可注册菜单/路由/配置）
│   └── 运行时注册表（存储所有扩展点及其实现）
│
├── Extension Points（扩展点，由主应用或插件声明）
│   ├── sidebar:menu        侧边栏菜单注入
│   ├── page:toolbar        页面工具栏按钮
│   ├── detail:tab          详情页 Tab 面板
│   ├── page:full           自定义全屏页面
│   ├── table:column        表格自定义列渲染器
│   ├── form:field          表单自定义字段组件
│   └── action:handler      动作处理器（按钮点击、事件响应）
│
├── 路由系统
│   ├── 系统路由（@elegant-router，构建时确定）
│   └── 插件路由（/plugin/:slug/:pageKey，运行时注册）
│
├── 懒加载（Lazy Activation）
│   ├── 低代码插件：首次访问时加载 Schema，由内置渲染引擎渲染
│   └── 前端插件（Phase 2）：按 activation_event 触发才加载代码
│
└── 事件总线（EventBus / Pinia Store）
    ├── 插件 → 主应用：通过标准扩展点 API 通信
    ├── 插件 → 插件：通过事件总线松耦合通信
    └── 主应用 → 插件：通过生命周期钩子通知
```

### 5.2 声明式贡献（Contributes）

借鉴 VS Code 模式：插件在 manifest 中**声明**要贡献的 UI 和功能，主应用解析 manifest 后即可注册菜单、路由等，**无需加载插件代码**。

**服务端聚合缓存**：管理员启用/禁用插件时，后端将所有活跃插件的 `contributes`（菜单、页面、按钮等）聚合为一份扁平 JSON 缓存。前端初始化时一次性加载该缓存完成路由注册和菜单渲染，将运行时解析复杂度降为 O(1)，避免每次路由切换遍历所有 manifest。

```jsonc
// 低代码插件的 contributes（由 models/pages/menu 隐式推导，无需额外声明）
// 前端插件（Phase 2）的 contributes 示例：
"contributes": {
  "menus": [
    { "id": "my-plugin-menu", "title": "报表中心", "icon": "BarChartOutline", "parent": null, "order": 200 }
  ],
  "pages": [
    { "route": "dashboard", "title": "数据大屏", "component": "Dashboard.vue" }
  ],
  "toolbarButtons": [
    { "id": "export-btn", "title": "导出", "icon": "DownloadOutline", "target": "user-list" }
  ],
  "config": {
    "type": "object",
    "properties": { "refreshInterval": { "type": "number", "title": "刷新间隔(秒)", "default": 30 } }
  }
}
```

### 5.3 懒加载激活（Lazy Activation）

借鉴 VS Code 的 `activationEvents`，插件在满足条件时才被激活（加载资源/执行代码）：

| 插件类型 | 激活时机 | 说明 |
|----------|----------|------|
| lowcode | 用户首次访问插件页面 | Schema 已缓存，渲染引擎内置，无需加载额外代码 |
| frontend | `onPage:slug/pageKey` 或 `onCommand:slug/action` | 访问插件页面或触发插件命令时才加载 Vue 组件 |
| backend | `onInstall` 或 `onEnable` | 安装/启用时启动容器 |

```jsonc
// Phase 2 前端插件的激活事件声明
"activation_events": [
  "onPage:zhangsan-report/dashboard",
  "onCommand:zhangsan-report.export"
]
```

### 5.4 @elegant-router 共存方案

**决策**：插件路由使用独立的 `pluginRouter`（Vue Router 实例），与 `@elegant-router` 生成的系统路由完全隔离。

```
Vue Router
├── 系统路由（@elegant-router 生成，构建时确定）
│   ├── /dashboard
│   ├── /system/user
│   └── ...
└── /plugin/:slug/:pageKey   ← 插件路由通配（运行时注册）
    ├── /plugin/zhangsan-crm/customer-list
    ├── /plugin/zhangsan-crm/customer-form
    └── ...
```

- 插件页面统一挂在 `/plugin/:slug/:pageKey` 路径下
- 低代码插件：路由指向通用的 `LowcodeRenderer.vue`，由 slug + pageKey 加载对应 Schema 渲染
- 前端插件（Phase 2）：路由指向远程加载的 Vue 组件
- 侧边栏菜单通过 Plugin Registry 动态注入，与系统菜单合并渲染

## 6. 低代码插件（Phase 1 详细设计）

低代码插件只提供 JSON 配置，渲染引擎在主应用中内置，不需要执行任意代码，天然安全。

### 6.1 Schema 标准：JSON Schema + UI Schema

- **data_schema**：标准 JSON Schema（IETF），描述数据结构和校验规则
- **ui_schema**：独立描述渲染方式，映射到 NaiveUI 组件

```jsonc
// data_schema (数据结构)
{
  "type": "object",
  "properties": {
    "name": { "type": "string", "title": "客户名称", "minLength": 2 },
    "level": { "type": "string", "title": "客户等级", "enum": ["A", "B", "C"] },
    "contact": { "type": "string", "title": "联系方式", "format": "phone" }
  },
  "required": ["name", "level"]
}

// ui_schema (渲染控制)
{
  "name": { "widget": "NInput", "span": 12 },
  "level": { "widget": "NSelect", "span": 12 },
  "contact": { "widget": "NInput", "props": { "mask": "phone" } },
  "ui:order": ["name", "level", "contact"],
  "ui:layout": "grid"
}
```

类型到组件映射：
```
string    → NInput / NSelect（有 enum 时）/ NDatePicker（有 date format 时）
number    → NInputNumber / NSlider
boolean   → NSwitch
array     → NDataTable（列表）/ NCheckboxGroup
object    → 嵌套表单 / NCollapse
```

### 6.2 数据存储：通用数据 API

主系统提供动态表能力，插件不需要自建数据表。

```
通用数据 API（按 model 操作）：
POST   /api/v1/plugin-data/{plugin_slug}/{model_key}          → 创建记录
GET    /api/v1/plugin-data/{plugin_slug}/{model_key}          → 分页查询
GET    /api/v1/plugin-data/{plugin_slug}/{model_key}/{id}     → 获取单条
PUT    /api/v1/plugin-data/{plugin_slug}/{model_key}/{id}     → 更新
DELETE /api/v1/plugin-data/{plugin_slug}/{model_key}/{id}     → 删除
GET    /api/v1/plugin-data/{plugin_slug}/{model_key}/export   → 导出数据（支持 json/csv，用于卸载前备份）
```

### 数据表设计

`models` 为可选字段：
- **有 models**：每个 model 独立建表 `plugin_data_{slug}_{model_key}`
- **无 models**：所有页面共享一张表 `plugin_data_{slug}`

每张表自动包含系统字段：`id`(Snowflake)、`created_at`、`updated_at`、`created_by`、`updated_by`。

JSON Schema 到 PostgreSQL 类型映射：
```
string              → VARCHAR / TEXT
integer             → INTEGER
number              → NUMERIC
boolean             → BOOLEAN
string(format:date) → DATE
string(format:datetime) → TIMESTAMPTZ
array / object      → JSONB
```

### 索引策略

插件在 model 中声明索引，安装时自动创建。

```jsonc
"models": [
  {
    "key": "customer",
    "data_schema": { ... },
    "indexes": [
      { "fields": ["name"], "unique": true },
      { "fields": ["level", "created_at"] }
    ]
  }
]
```

- 每个索引的 `fields` 为字段名数组
- `"unique": true` 表示唯一索引（默认 false）
- 系统自动为 `id`、`created_at`、`tenant_id`（预留）创建索引，无需声明
- JSONB 字段不支持索引声明；如需 JSONB 内查询，由 Phase 2 扩展支持

### JSONB 字段查询限制

`array` / `object` 类型存为 JSONB，**Phase 1 的限制**：

- `array` 类型支持 `contains` 查询：筛选器可匹配数组是否包含某个值（PostgreSQL `?` 操作符）。例如 `tags` 字段含 `["VIP", "重点"]`，筛选 `tags` contains `"VIP"` 可命中
- `object` 类型不支持按内部字段筛选，表单中以 JSON 编辑器组件呈现（只读或简单编辑）
- Phase 2 可通过 `ui_schema` 中的 `"filterable": true` + JSONB GIN 索引支持按 object 内部字段筛选

```jsonc
// array 字段筛选示例（Phase 1 支持）
"ui_schema": {
  "tags": { "widget": "NCheckboxGroup", "span": 24, "filterable": true, "filter_type": "contains" }
}
```

### 表关联（Phase 1 基础支持）

Phase 1 支持 `belongs_to` 和 `has_many` 两种关联，满足多表插件的基本需求（如订单列表显示客户名称）。

```jsonc
"models": [
  { "key": "customer", "data_schema": { ... } },
  {
    "key": "order",
    "data_schema": {
      "type": "object",
      "properties": {
        "customer_id": { "type": "string", "title": "客户", "x-ref": "customer", "x-ref-label": "name" },
        "amount": { "type": "number", "title": "金额" }
      }
    },
    "relations": [
      { "type": "belongs_to", "model": "customer", "foreign_key": "customer_id", "label_field": "name" }
    ]
  }
]
```

关联显示字段声明：
- 在 `relations` 中通过 `label_field` 显式指定关联模型的显示字段（如 `"label_field": "name"`）
- 若未声明 `label_field`，fallback 到关联模型的第一个 `string` 类型字段
- `x-ref` 中也可通过 `"x-ref-label": "name"` 声明，优先级高于 relations 中的 label_field

运行时行为：
- `belongs_to`：列表页自动 JOIN 并返回关联模型的显示字段
- `has_many`（反向）：由 `belongs_to` 自动推导，无需声明
- 数据 API 支持查询参数 `?expand=customer` 展开关联数据
- Phase 2 扩展：`many_to_many`、中间表、关联字段自定义显示

### Schema 变更（插件升级时）

```
对比新旧 data_schema：
├── 新增字段       → ALTER TABLE ADD COLUMN（带默认值或 NULL，安全操作）
├── 删除字段       → 保留列，标记 deprecated（不丢数据）
├── 安全类型变更   → 允许：VARCHAR 加宽、INTEGER → NUMERIC 等 widening 操作
├── 破坏性类型变更 → 拒绝：如 string → integer、缩小 VARCHAR 宽度等，要求开发者新建字段迁移
└── 生成 migration 记录，可回滚
```

**破坏性变更的处理**：升级时系统自动检测并标记为"不安全变更"，拒绝自动执行。开发者需在 manifest 中声明 `migrations` 脚本手动处理。

### 卸载与重装

- **软删除（默认）**：不重命名物理表。在 `tenant_plugin` 记录中标记卸载状态，并记录 `archived_table_name`（如 `plugin_data_crm_suite`）和 `has_data=true`。管理员可选硬删除（DROP TABLE，需二次确认）
- **重装同名插件**：查询 `tenant_plugin` 中该 `plugin_id` 的历史安装记录，若存在 `archived_table_name`，提示管理员选择"恢复历史数据"或"全新安装"。选择恢复则复用已有数据表，并在安装过程中执行必要的 Schema 迁移
- 相比重命名物理表（`ALTER TABLE`），此方案避免高并发下的锁表风险，且重装检测走数据库查询而非 `information_schema`，更可靠

### 6.3 逻辑能力：CRUD + 动作（中等）

内置标准 CRUD（列表 + 新增 + 编辑 + 删除 + 详情），额外支持按钮动作：

```
动作类型（Phase 1）：
├── api_call     → 调用受限 API（见下方权限边界）
├── navigate     → 跳转到指定页面（插件内或其他页面）
├── confirm      → 弹窗确认后执行（如：删除前确认）
├── notification → 显示操作结果（成功/失败提示）
└── form_submit  → 提交表单数据（标准 CRUD 已内置）
```

### api_call 权限边界

`api_call` 动作**不是任意 API 调用**，而是受限的白名单机制：

```
api_call 允许的调用范围：
├── 本插件的通用数据 API（/api/v1/plugin-data/{self_slug}/...）
├── 系统公开 API（在 manifest permissions 中声明并通过审核的）
└── 外部 HTTP API（仅 GET，且 URL 必须在 permissions 中预声明）

api_call 禁止：
├── 调用其他插件的数据 API
├── 调用系统管理类 API（用户、角色、权限等）
├── POST/PUT/DELETE 到外部 URL
└── 访问内网地址（防止 SSRF：127.0.0.1, 169.254.169.254, 10.x, 192.168.x 等）
```

```jsonc
// api_call 示例：调用外部天气 API
"actions": [
  {
    "key": "query_weather",
    "type": "api_call",
    "config": {
      "url": "https://api.weather.com/v1/current",
      "method": "GET",
      "params": { "city": "{{form.city}}" }  // 支持模板变量引用表单字段
    }
  }
]
// 必须在 permissions 中声明：{ "type": "external_api", "detail": "GET https://api.weather.com/v1/*" }
```

### api_call 模板变量安全规则

模板变量 `{{form.fieldName}}` 仅用于 URL query parameter 的值，有以下安全限制：

- 模板变量**只作为 URL query parameter 的值**，禁止拼入 URL path 部分
- 模板变量的值经过 URL encode 后发送，不参与 URL 路径构造
- **结构化参数传递**：运行时由服务端将 `params` 解析为严格的 Key-Value 字典，通过 HTTP 客户端的 `params` 参数传递（如 `httpx.get(url, params=params_dict)`），**严禁**将变量值拼接到 URL 字符串中，从根本上杜绝参数注入
- 字段值长度限制 500 字符（超长截断）
- 仅允许字母、数字、CJK 字符、常见标点，过滤控制字符和特殊符号（`< > " ' \x00-\x1f`）
- 外部 API 的 URL path 必须在 `permissions` 中精确声明（支持 `*` 通配符），运行时校验实际请求 URL 是否匹配声明的 pattern

### 权限执行模型

Manifest 中 `permissions` 声明是**强制白名单**，不是记录式声明：

- 安装时：管理员审批权限列表，可逐项拒绝
- 运行时：系统在 API 网关层强制校验，未声明的操作会被拦截
- 低代码插件：只有 `api_call`、`external_api`、`menu` 三种权限类型
- 前端/后端插件（Phase 2/3）：扩展 `api`、`db_table`、`filesystem` 等权限类型

Phase 2 再扩展：条件显隐、字段联动、跨表关联。

### 6.4 低代码插件完整结构

**数据模型引用规则**：`data_schema` 只在 `models`（或无 models 时的顶层）定义一次，`pages` 通过 `model` key 引用，不重复声明 data_schema。

- **有 `models` 数组**：每个 page 必须通过 `model` 字段引用对应的 model key
- **无 `models` 数组**：顶层 `data_schema` 作为默认 model，pages 中 `model` 可省略

**简单插件（单表）**：

```jsonc
{
  "name": "客户管理",
  "slug": "customer-mgmt",
  "version": "1.0.0",
  "type": "lowcode",
  "data_schema": { /* 字段定义，唯一声明处 */ },
  "pages": [
    {
      "key": "list", "title": "客户列表", "page_type": "table",
      // model 省略 → 自动引用顶层 data_schema
      "ui_schema": { /* 列表渲染 */ },
      "actions": [
        { "key": "add", "type": "navigate", "target": "form" }
      ]
    },
    {
      "key": "form", "title": "客户表单", "page_type": "form",
      "ui_schema": { /* 表单渲染 */ },
      "actions": [
        { "key": "submit", "type": "form_submit" },
        { "key": "cancel", "type": "navigate", "target": "list" }
      ]
    }
  ],
  "menu": { "title": "客户管理", "icon": "PeopleOutline", "parent": null }
}
// → 建一张表: plugin_data_customer_mgmt
```

**复杂插件（多表，带关联）**：

```jsonc
{
  "name": "CRM 套件",
  "slug": "crm-suite",
  "version": "1.0.0",
  "type": "lowcode",
  "models": [
    {
      "key": "customer",
      "data_schema": { /* 客户字段 */ },
      "indexes": [
        { "fields": ["name"], "unique": true }
      ]
    },
    {
      "key": "order",
      "data_schema": {
        "type": "object",
        "properties": {
          "customer_id": { "type": "string", "title": "客户", "x-ref": "customer", "x-ref-label": "name" },
          "amount": { "type": "number", "title": "金额" },
          "status": { "type": "string", "title": "状态", "enum": ["pending", "paid", "done"] }
        }
      },
      "relations": [
        { "type": "belongs_to", "model": "customer", "foreign_key": "customer_id", "label_field": "name" }
      ]
    },
    { "key": "product", "data_schema": { /* 商品字段 */ } }
  ],
  "pages": [
    { "key": "customer-list", "model": "customer", "title": "客户列表", "page_type": "table",
      "ui_schema": {...}, "actions": [...] },
    { "key": "customer-form", "model": "customer", "title": "客户表单", "page_type": "form",
      "ui_schema": {...}, "actions": [...] },
    { "key": "order-list", "model": "order", "title": "订单列表", "page_type": "table",
      "ui_schema": {...}, "actions": [...] },
    { "key": "product-list", "model": "product", "title": "商品列表", "page_type": "table",
      "ui_schema": {...}, "actions": [...] }
  ],
  "menu": { "title": "CRM", "icon": "BriefcaseOutline", "parent": null }
}
// → 建三张表: plugin_data_crm_suite_customer / _order / _product
```

## 7. Plugin Manifest 规范

所有插件类型共用一套 Manifest 规范，是 `plugin.json` 的标准格式。

### 7.1 决策

- **slug 唯一性**：采用 `author-slug` 格式（如 `zhangsan-customer-mgmt`），author 前缀在开发者注册时确定，不可更改。官方内置插件使用 `hohu-` 前缀。审核时确认不重复
- **版本策略**：语义化版本，每次发布创建新版本记录，支持多版本共存和回滚
- **可配置性**：`config_schema` 使用标准 JSON Schema 格式，与 `data_schema` 保持一致，由渲染引擎统一处理

### 7.2 Manifest 结构

```jsonc
{
  // ─── 基础元信息（所有类型必填） ───
  "name": "客户管理",
  "slug": "zhangsan-customer-mgmt",     // author-slug 格式，全局唯一
  "version": "1.0.0",                   // 语义化版本
  "description": "客户信息管理模块",
  "type": "lowcode",                    // lowcode | frontend | backend | fullstack | theme
  "author": "张三",
  "homepage": "https://github.com/...",
  "license": "MIT",

  // ─── 兼容性 ───
  "engines": {
    "hohu": ">=1.0.0 <2.0.0"
  },
  "dependencies": {                     // 依赖的其他插件，采用 npm-like semver range
    "notification-plugin": "^1.0.0"     // 支持 ^、>=、< 等语义化版本范围
  },

  // ─── 权限声明 ───
  "permissions": [
    { "type": "api", "detail": "GET /api/v1/users" },
    { "type": "menu", "detail": "inject:sidemenu" }
  ],

  // ─── 安装后可配置项（标准 JSON Schema，管理员可在 UI 修改） ───
  "config_schema": {
    "type": "object",
    "properties": {
      "api_key": { "type": "string", "title": "API密钥" },
      "sync_interval": { "type": "number", "title": "同步间隔(秒)", "default": 300 }
    },
    "required": ["api_key"]
  },

  // ─── 低代码组件（type=lowcode 时，直接放在顶层） ───
  "models": [ /* ... 见 6.4 节 */ ],
  "pages": [ /* ... 见 6.4 节 */ ],

  // ─── 其他类型的组件放在 components 内 ───
  "components": {
    // frontend: { "entries": [...], "widgets": [...] }
    // backend:  { "routes": [...], "models": [...], "migrations": [...] }
    // fullstack: { "frontend": {...}, "backend": {...} }
    // theme:    { "variables": {...}, "layouts": [...] }
  },

  // ─── 菜单注册 ───
  "menu": {
    "title": "客户管理",
    "icon": "PeopleOutline",
    "parent": null,                     // null = 顶级，或填父插件 slug
    "order": 100
  },

  // ─── 市场展示 ───
  "marketplace": {
    "category": "业务管理",
    "tags": ["CRM", "客户", "销售"],
    "screenshots": ["screenshot1.png"],
    "changelog": "## 1.0.0\n首次发布"
  }
}
```

### 7.3 打包格式

插件打包为 `.zip`（或 `.tar.gz`），结构：

```
zhangsan-customer-mgmt-1.0.0.zip
├── plugin.json            # Manifest（必需）
├── README.md              # 插件说明（可选）
├── CHANGELOG.md           # 变更日志（可选）
├── screenshots/           # 截图目录（可选）
│   └── screenshot1.png
└── assets/                # 静态资源（可选，前端/主题插件）
    └── ...
```

低代码插件：`plugin.json` 内包含全部 Schema 定义，无需额外代码文件。
前端/后端插件：`assets/` 目录包含实际的代码文件。

## 8. 市场前端 UI 设计

### 8.1 决策

- **导航位置**：应用市场作为侧边栏顶级菜单项，与"系统管理"平级
- **角色分离**：应用市场（管理员/用户）和 开发者中心（开发者）为两套独立 UI

### 8.2 应用市场（管理员/用户视角）

```
侧边栏
├── ...
├── 应用市场 ← 顶级菜单
│   ├── 浏览市场
│   │   ├── 搜索栏 + 分类筛选
│   │   ├── 推荐插件轮播
│   │   ├── 分类卡片（业务管理、数据分析、效率工具、...）
│   │   └── 热门/最新插件列表
│   │
│   ├── 插件详情页
│   │   ├── 基本信息（名称、作者、版本、评分、下载量）
│   │   ├── 截图预览
│   │   ├── 功能描述（README 渲染）
│   │   ├── 版本历史
│   │   └── 操作按钮（安装 / 已安装 → 设置 / 启用/禁用 / 卸载）
│   │
│   └── 已安装管理
│       ├── 已安装插件列表（名称、版本、状态、操作）
│       ├── 一键更新检查
│       └── 批量启用/禁用
│
├── 系统管理
│   ├── ...
```

每个插件安装后，其 `menu` 声明的菜单项会动态注入到侧边栏对应位置。

### 8.3 插件设置页

安装后，管理员可通过插件详情页进入设置，设置表单由 `config_schema` 动态渲染。

### 8.4 开发者中心（开发者视角）

```
侧边栏
├── ...
├── 开发者中心 ← 顶级菜单（仅开发者角色可见）
│   ├── 我的插件
│   │   ├── 插件列表（名称、状态、版本、下载量）
│   │   └── 操作（编辑、发布新版本、下架、查看统计）
│   │
│   ├── 发布插件
│   │   ├── 上传 zip 包
│   │   ├── Manifest 校验（自动检查格式、权限声明）
│   │   ├── 预览（低代码插件可实时预览渲染效果）
│   │   └── 提交审核
│   │
│   └── 数据统计
│       ├── 下载量趋势
│       ├── 安装量
│       └── 版本分布
```

### 8.5 权限控制

- **应用市场**：所有登录用户可浏览，仅管理员可安装/卸载/配置
- **开发者中心**：拥有 `plugin:develop` 权限的用户可见
- **审核权限**：拥有 `plugin:review` 权限的管理员可审核插件

## 9. Plugin API 与开发者工具

### 9.1 Plugin API 层（hohu namespace）

借鉴 VS Code 的 `vscode` 命名空间和 IntelliJ 的服务架构，插件通过受控 API 与主系统交互，**不能直接访问主系统内部模块**。

```
Plugin API（hohu namespace）
├── hohu.data              通用数据 CRUD（本插件数据）
│   ├── create(model, record)
│   ├── query(model, params)
│   ├── update(model, id, data)
│   └── delete(model, id)
│
├── hohu.config            插件配置读写
│   ├── get(key)
│   └── set(key, value)
│
├── hohu.i18n              国际化
│   └── t(key, params)
│
├── hohu.event              事件通信
│   ├── emit(event, data)        发布事件
│   └── on(event, handler)       订阅事件
│
├── hohu.notification       消息通知
│   ├── success(msg)
│   ├── error(msg)
│   └── warning(msg)
│
├── hohu.router             路由（前端插件 Phase 2）
│   ├── navigate(path)
│   └── getCurrentRoute()
│
└── hohu.storage            持久化存储
    ├── get(key)
    └── set(key, value)
```

**API 版本化策略**（借鉴 VS Code）：

- API 与 `engines.hohu` 版本绑定，语义化版本控制
- 新增字段/方法标记为 `@since x.y.z`
- 破坏性变更仅在 major 版本中发生，并保留至少一个 major 版本的兼容期
- Phase 1 仅暴露 `hohu.data`、`hohu.config`、`hohu.event`、`hohu.notification`、`hohu.storage`

### 9.2 后端插件 API（Phase 3，容器内 HTTP）

后端插件运行在独立容器中，通过 HTTP 调用主系统 API：

```
主系统 → 插件容器：
  POST /internal/hooks/{hook_name}     生命周期钩子回调

插件容器 → 主系统（通过 API Gateway）：
  GET  /api/v1/plugin-data/{self}/...   本插件数据 CRUD
  GET  /api/v1/plugin/config            读取配置
  POST /api/v1/plugin/event/emit        发送事件
```

所有请求携带插件身份 token（安装时签发），API Gateway 校验权限范围。

### 9.3 Plugin SDK（开发工具链）

借鉴 VS Code 的 `vsce` CLI 和 WordPress 的 `wp plugin scaffold`，提供完整开发工具链。

```bash
# 初始化插件脚手架
hohu plugin create my-plugin --type lowcode
hohu plugin create my-plugin --type frontend
hohu plugin create my-plugin --type backend

# 本地开发
hohu plugin dev                    # 启动本地预览（连接开发后端）
hohu plugin dev --hot              # 热重载模式（前端插件）

# 校验与测试
hohu plugin validate               # 校验 manifest 格式、权限声明
hohu plugin test                   # 运行插件测试套件
hohu plugin lint                   # 代码检查

# 打包与发布
hohu plugin pack                   # 打包为 zip（自动校验）
hohu plugin publish                # 发布到市场（需登录）
hohu plugin publish --dry-run      # 预览发布流程

# 版本管理
hohu plugin version patch          # bump patch 版本
hohu plugin version minor          # bump minor 版本
hohu plugin version major          # bump major 版本
```

**脚手架生成的目录结构**：

```
my-plugin/
├── plugin.json           # Manifest（预填充模板）
├── README.md             # 插件说明模板
├── CHANGELOG.md          # 变更日志模板
├── screenshots/          # 截图目录
├── tests/                # 测试目录
│   └── manifest.test.js  # Manifest 校验测试
└── assets/               # 静态资源（前端/后端插件）
```

## 10. 事件系统（Plugin Interop）

借鉴 WordPress 的 Actions/Filters 和 Vite/Rollup 的 Hook Pipeline 模式，提供插件间松耦合通信机制。

### 事件类型

| 类型 | 模式 | 返回值 | 典型场景 |
|------|------|--------|----------|
| **Action（动作）** | 发布/订阅，一对多 | 无 | 通知"订单已创建"，多个插件可各自响应 |
| **Filter（过滤器）** | 管道链式，顺序执行 | 修改后的数据 | 拦截并修改"列表查询结果"，添加计算字段 |
| **Command（命令）** | 首个响应者胜出 | 单个结果 | "导出数据"，只有第一个处理的插件执行 |

```
Action 示例：
  插件A emit("order:created", { orderId: "123" })
  → 插件B on("order:created", handler)  // 发送通知
  → 插件C on("order:created", handler)  // 更新库存

Filter 示例：
  插件A applyFilter("customer:list:columns", baseColumns)
  → 插件B filter(handler)  // 添加"累计消费"列
  → 插件C filter(handler)  // 添加"最后下单时间"列
  → 返回合并后的列定义

Command 示例：
  插件A executeCommand("data:export", { format: "excel" })
  → 插件B registerCommand("data:export", handler)  // 第一个注册者处理
```

### 事件命名空间

```jsonc
// 插件声明对外暴露的事件（在 manifest 中）
"events": {
  "emits": [
    "order:created",       // 本插件发出的事件
    "order:status_changed"
  ],
  "subscribes": [
    "user:login",          // 订阅其他插件/系统事件
    "notification:send"
  ],
  "filters": [
    "customer:list:columns"  // 提供的过滤器钩子
  ],
  "commands": [
    "data:export"            // 提供的命令
  ]
}
```

- 事件名使用 `{domain}:{action}` 格式，域名与插件 slug 对应
- 系统内置事件：`user:login`、`user:logout`、`plugin:installed`、`plugin:enabled`、`plugin:disabled`
- 权限控制：插件只能订阅 manifest 中声明的事件（审核时校验合理性）
- Phase 1 仅支持 Action 类型（低代码插件通过页面级事件声明触发），Phase 2 开放 Filter 和 Command

**低代码插件的事件触发（Phase 1）**：在 page 级别通过 `events` 字段声明事件触发时机，由主系统内置执行器处理：

```jsonc
"pages": [
  {
    "key": "form", "title": "客户表单", "page_type": "form",
    "ui_schema": { /* ... */ },
    "events": {
      "after_create": { "emit": "customer:created", "payload": { "id": "{{record.id}}", "name": "{{record.name}}" } },
      "after_update": { "emit": "customer:updated", "payload": { "id": "{{record.id}}" } },
      "after_delete": { "emit": "customer:deleted", "payload": { "id": "{{record.id}}" } }
    }
  }
]
```

- `events` 的 key 为触发时机：`after_create`、`after_update`、`after_delete`、`after_submit`（通用）
- `emit` 为事件名，格式为 `{domain}:{action}`，domain 与插件 slug 对应
- `payload` 支持模板变量引用当前记录字段（`{{record.fieldName}}`），规则同 api_call 模板变量安全规则
- 插件需在 manifest 的 `events.emits` 中声明会发出的事件

## 11. 审核流程

三层审核架构：规则检查 → AI 审核 → 人工审核。

### 11.1 更新审核策略

- **首次发布**：规则 + AI + 人工审核
- **patch 更新（x.y.Z）**：仅规则检查，免 AI 和人工
- **minor 更新（x.Y.z）**：规则 + AI 审核，低风险免人工
- **major 更新（X.y.z）**：规则 + AI + 人工审核

### 11.2 第 1 层：规则检查（即时）

```
格式校验：
├── plugin.json 存在且合法 JSON
├── 必填字段完整（name/slug/version/type）
├── slug 不与已有插件冲突
└── 版本号格式合法（semver）

安全扫描：
├── 低代码：JSON Schema 合法性
├── 前端：扫描 eval/Function/innerHTML
└── 后端：扫描 os.system/subprocess

基础检查：
├── 文件大小 <= 50MB（可配置）
└── engines.hohu 版本范围有效
```

### 11.3 第 2 层：AI 审核（异步，秒级）

实现为专用 `PluginReviewAgent`，复用 hohu AI 基础设施。

```
低代码插件（Phase 1）：
├── Schema 合理性（字段类型、校验规则完备性）
├── UX 检查（表单字段数量、页面结构）
├── 描述一致性（README vs Schema 是否匹配）
└── 自动生成插件功能摘要

前端/后端插件（Phase 2/3）：
├── 代码安全扫描（eval、命令注入、SSRF）
├── API 调用合规性
├── 依赖安全性（已知漏洞检测）
└── 性能风险标记

输出：
├── 风险等级：low / medium / high
├── 审核报告摘要
└── low → 自动通过；medium/high → 推入人工队列
```

### 11.4 第 3 层：人工审核

审核员看到 AI 生成的审核报告摘要，重点审查 AI 标记的可疑点。

```
审核要点：
├── 功能描述是否准确
├── 是否有恶意行为倾向
├── 是否侵犯商标/版权
└── 是否和已有插件高度重复

结果：通过 / 拒绝（附原因，开发者可修改后重新提交）
```

## 12. 数据模型

### 12.1 插件包（plugin）

```sql
plugin
├── id               BIGINT PK (Snowflake)
├── name             VARCHAR(100) NOT NULL
├── slug             VARCHAR(150) NOT NULL UNIQUE   -- author-slug 格式
├── type             VARCHAR(20)  NOT NULL          -- lowcode|frontend|backend|fullstack|theme
├── description      TEXT
├── icon             VARCHAR(500)                    -- 图标 URL
├── author_id        BIGINT FK → user.id             -- 关联系统用户（开发者）
├── author_name      VARCHAR(100)                    -- 冗余，方便展示
├── status           VARCHAR(20)  NOT NULL DEFAULT 'draft'  -- draft|reviewing|published|archived|rejected
├── current_version_id BIGINT FK → plugin_version.id -- 最新已发布版本 ID
├── homepage         VARCHAR(500)
├── license          VARCHAR(50)
├── download_count   INT DEFAULT 0
├── avg_rating       DECIMAL(2,1) DEFAULT 0.0
├── rating_count     INT DEFAULT 0
├── created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
└── updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()

注：manifest 只存在于 plugin_version 表中，plugin 表通过 current_version_id JOIN 获取最新 manifest，避免双写不同步问题。
```

### 12.2 插件版本（plugin_version）

```sql
plugin_version
├── id               BIGINT PK (Snowflake)
├── plugin_id        BIGINT FK → plugin.id NOT NULL
├── version          VARCHAR(20)  NOT NULL           -- semver
├── changelog        TEXT
├── manifest         JSONB NOT NULL                  -- 该版本的完整 manifest 快照（含 engines.hohu 兼容性声明）
├── file_url         VARCHAR(500) NOT NULL           -- 对象存储路径（S3/MinIO）
├── file_hash        VARCHAR(64)  NOT NULL           -- SHA-256
├── file_size        BIGINT                           -- 字节
├── review_status    VARCHAR(20)  NOT NULL DEFAULT 'pending'  -- pending|approved|rejected
├── review_id        BIGINT FK → plugin_review.id     -- 关联最新审核记录
├── created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
└── UNIQUE(plugin_id, version)

注：兼容性信息统一从 manifest 中的 engines.hohu 提取，不再在 plugin_version 表冗余 min_app_version/max_app_version 字段。
```

### 12.3 插件审核记录（plugin_review）

```sql
plugin_review
├── id               BIGINT PK (Snowflake)
├── plugin_id        BIGINT FK → plugin.id NOT NULL
├── version_id       BIGINT FK → plugin_version.id NOT NULL
├── rule_check_result  JSONB            -- 第 1 层：规则检查结果（通过/失败详情）
├── rule_check_at      TIMESTAMPTZ
├── ai_risk_level      VARCHAR(10)      -- 第 2 层：low|medium|high|pending|skipped
├── ai_report          JSONB            -- AI 审核报告（摘要、风险点列表）
├── ai_review_at       TIMESTAMPTZ
├── human_status       VARCHAR(20) DEFAULT 'pending'  -- 第 3 层：pending|approved|rejected|skipped
├── human_reviewer_id  BIGINT FK → user.id
├── human_comment      TEXT             -- 审核意见
├── human_reviewed_at  TIMESTAMPTZ
├── final_status       VARCHAR(20) NOT NULL DEFAULT 'pending'  -- pending|approved|rejected
├── created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
└── updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

### 12.4 租户安装记录（tenant_plugin）

```sql
tenant_plugin
├── id               BIGINT PK (Snowflake)
├── tenant_id        BIGINT                            -- 预留，Phase 1 默认 0
├── plugin_id        BIGINT FK → plugin.id NOT NULL
├── installed_version VARCHAR(20) NOT NULL             -- 当前安装的版本号
├── status           VARCHAR(20) NOT NULL DEFAULT 'disabled'  -- installed|enabled|disabled|uninstalled
├── config           JSONB                              -- 管理员填写的配置（按 config_schema）
├── approved_permissions JSONB                          -- 管理员审批通过的权限子集
├── archived_table_name VARCHAR(100)                    -- 卸载后记录数据表名（如 plugin_data_crm_suite）
├── has_data         BOOLEAN DEFAULT false              -- 卸载时是否有历史数据可恢复
├── installed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
├── updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
└── UNIQUE(tenant_id, plugin_id)
```

### 12.5 插件权限声明（plugin_permission）

```sql
plugin_permission
├── id                    BIGINT PK (Snowflake)
├── plugin_id             BIGINT FK → plugin.id NOT NULL
├── permission_type       VARCHAR(30) NOT NULL   -- api|external_api|menu|db_table|...
├── permission_detail     JSONB NOT NULL          -- 权限详情（API 路径、URL pattern 等）
├── permission_detail_hash VARCHAR(64) NOT NULL   -- permission_detail 的 SHA-256 哈希，用于 UNIQUE 约束
├── created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
└── UNIQUE(plugin_id, permission_type, permission_detail_hash)
```

### 12.6 插件评分与评论（plugin_rating）

```sql
plugin_rating
├── id               BIGINT PK (Snowflake)
├── plugin_id        BIGINT FK → plugin.id NOT NULL
├── user_id          BIGINT FK → user.id NOT NULL
├── rating           SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5)
├── comment          TEXT
├── created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
├── updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
└── UNIQUE(plugin_id, user_id)

注：评分前校验用户是否已安装该插件（JOIN tenant_plugin 验证），未安装用户不允许评分，防止刷评。
```

### 12.7 文件存储

- **存储后端**：对象存储（S3 或 MinIO），路径格式 `plugins/{slug}/{version}/{filename}`
- **下载 URL**：通过后端 API 签发生成临时 URL（有效期 1 小时），不直接暴露存储路径
- **完整性校验**：上传时计算 SHA-256 存入 `file_hash`，下载时校验

## 13. 已确定决策

1. **多租户** — 先单租户，数据模型预留 `tenant_id`，后续升级改动最小
2. **插件信任模型** — 完全开放市场，支持第三方不可信插件 → 后端必须容器隔离
3. **开发优先级** — Phase 1 低代码 → Phase 2 前端插件 → Phase 3 后端 + 全栈插件
4. **内置模块关系** — `app/modules/system` 等核心模块永远内置，与插件系统完全分离
5. **slug 命名空间** — `author-slug` 格式（如 `zhangsan-customer-mgmt`），官方使用 `hohu-` 前缀
6. **前端路由隔离** — 插件路由使用独立 `pluginRouter`，统一挂在 `/plugin/:slug/:pageKey` 下，与 `@elegant-router` 完全隔离
7. **权限白名单执行** — Manifest 中 permissions 是强制白名单，安装时管理员逐项审批，运行时网关层强制校验
8. **api_call 受限** — 低代码插件的 api_call 只能调用本插件数据 API 和预声明的外部 GET API，禁止 SSRF
9. **Schema 变更安全** — 只允许 widening 类型变更，破坏性变更需手动 migration 脚本
10. **文件存储** — 对象存储（S3/MinIO），SHA-256 校验，签名临时 URL 下载
11. **卸载重装** — 软删除（在 tenant_plugin 记录中标记卸载状态 + 记录归档表名），不重命名物理表；重装时查询历史安装记录提示恢复或全新安装
12. **声明式贡献** — 插件通过 manifest 声明 UI 贡献（菜单、页面、按钮），主应用解析后注册，无需加载代码
13. **懒加载激活** — 插件仅在用户首次访问时激活，避免启动时加载所有插件
14. **受控 API 层** — 插件通过 `hohu` namespace 访问主系统，不可直接访问内部模块
15. **事件系统** — Action/Filter/Command 三种模式支持插件间松耦合通信
16. **Plugin SDK** — 提供 CLI 脚手架（create/dev/validate/pack/publish），降低开发门槛
17. **依赖管理** — 版本范围采用 npm-like semver range，安装时拓扑排序 + 循环依赖检测，依赖插件被卸载时阻止
18. **数据模型唯一性** — manifest 只存于 plugin_version 表，plugin 表通过 current_version_id JOIN 获取；data_schema 只在 models（或顶层）定义一次
19. **关联显示字段显式声明** — relations 支持 `label_field` / `x-ref` 支持 `x-ref-label` 显式指定关联模型的显示字段，未声明时 fallback 到第一个 string 字段
20. **api_call 结构化参数** — 模板变量由服务端解析为 Key-Value 字典，通过 HTTP 客户端 params 参数传递，严禁字符串拼接构造 URL
21. **前端沙箱技术选型** — Phase 2 采用 Wujie 微前端 + Module Federation 组合：MF 处理依赖共享和代码分发，Wujie 提供 ShadowRoot 样式隔离 + iframe JS 隔离，拒绝纯 eval/fetch 加载
22. **网关上下文注入** — API 网关验证 JWT 后，将 User-ID、Tenant-ID、Roles 以 `X-Hohu-*` Header 透传给插件容器，插件不持有原始 JWT
23. **容器依赖隔离** — 插件容器的第三方 Python 依赖封装在自身 Docker 镜像内，主系统进程绝不通过 pip 安装插件依赖
24. **Manifest 聚合缓存** — 启用/禁用插件时后端聚合所有活跃插件的 contributes 为一份扁平 JSON 缓存，前端一次性加载完成路由注册

### 开发阶段规划

```
Phase 1：低代码插件（MVP）
  → 内置 JSON Schema 渲染引擎
  → 市场基础设施：上传、审核、安装、配置、启用/禁用
  → 不需要沙箱（JSON 不执行代码），安全性天然保证
  → 覆盖 70% 的 CRUD 场景

Phase 2：前端插件
  → Vue 组件远程加载 + 路由动态注入
  → 扩展点机制（菜单、工具栏、Tab、自定义页面）
  → Wujie 微前端 + Module Federation 隔离（MF 处理依赖共享和代码分发，Wujie 通过 ShadowRoot 隔离样式 + iframe 隔离 JS 全局上下文，拒绝纯 eval/fetch 加载）

Phase 3：后端 + 全栈插件
  → Docker 容器 / 受限子进程沙箱
  → API 网关代理插件路由，网关承担上下文增强职责（X-Hohu-* Header 透传用户/租户身份）
  → 前后端组合插件编排
  → 需评估是否为插件提供受限 PostgreSQL 账号直连专有表（用于复杂事务和 ORM 高级特性，代替纯 API 模式）
```

## 14. 参考系统借鉴

### Odoo

| 模式 | 借鉴内容 | 应用位置 |
|------|----------|----------|
| 模块继承（`_inherit`） | 不修改上游代码，通过声明式方式扩展已有模型和视图 | Phase 2 前端插件扩展点（向现有页面注入字段/Tab） |
| XPath 视图组合 | 多个插件独立扩展同一视图，按 priority 合并 | Phase 2 表格/表单扩展点（`table:column`、`form:field`） |
| 生命周期钩子 | `pre_init_hook`/`post_init_hook`/`uninstall_hook` | 第 2 节插件生命周期钩子 |
| 依赖拓扑排序 | `depends` 声明 + 拓扑排序加载 | Manifest `dependencies`，安装时按依赖顺序加载 |
| 每 module 独立安全声明 | `security/ir.model.access.csv` 定义 CRUD 权限 | Manifest `permissions`，按模型声明权限 |

### WordPress

| 模式 | 借鉴内容 | 应用位置 |
|------|----------|----------|
| Action/Filter 钩子 | `do_action`/`apply_filters` — 所有扩展性的基础 | 第 9.5 节事件系统（Action/Filter/Command） |
| 插件头部元数据 | 标准化 `Plugin Name`/`Version` 头部声明 | `plugin.json` Manifest 格式 |
| Activation/Deactivation 钩子 | 插件启用/禁用时的回调 | 第 2 节 `on_enable`/`on_disable` 钩子 |
| SVN 仓库 + 手动审核 | 发布前人工审核流程 | 第 10 节三层审核流程 |
| Capability 权限系统 | `current_user_can('capability')` 细粒度权限 | `permissions` 白名单 + `plugin:develop`/`plugin:review` 角色 |
| 插件扩展插件（WooCommerce 模式） | 通过 hooks 递归扩展 | Phase 2 插件间事件通信 |

### VS Code

| 模式 | 借鉴内容 | 应用位置 |
|------|----------|----------|
| `contributes` 声明式注册 | 不加载代码即可注册命令/视图/菜单 | 第 5.2 节声明式贡献 |
| Extension Host 进程隔离 | 扩展运行在独立进程，崩溃不影响主程序 | Phase 3 Docker 容器隔离 |
| `vscode` 受控 API 命名空间 | 扩展只能访问稳定 API，不能访问内部模块 | 第 9.1 节 `hohu` namespace |
| `activationEvents` 懒加载 | 首次触发条件时才加载扩展 | 第 5.3 节懒加载激活 |
| `engines.vscode` 版本约束 | 声明兼容的宿主版本范围 | Manifest `engines.hohu` |
| API 版本化 | 稳定 API + proposed API 两层 | 第 9.1 节 API 版本化策略 |
| `vsce` CLI 发布工具 | CLI 打包、校验、发布 | 第 9.3 节 Plugin SDK |

### IntelliJ IDEA

| 模式 | 借鉴内容 | 应用位置 |
|------|----------|----------|
| Extension Points 双向扩展 | 平台和插件都可以声明和消费扩展点 | 第 5.1 节 Extension Points |
| `plugin.xml` 声明式服务注册 | 通过 XML 声明服务、Action、扩展 | `plugin.json` Manifest 结构 |
| ClassLoader 隔离 | 每个插件独立类加载器，依赖版本不冲突 | Phase 3 容器隔离天然实现 |
| 动态加载/卸载 | 运行时安装/卸载插件不重启 | Phase 1 低代码天然支持，Phase 2/3 需处理资源释放 |
| Action Group 锚定 | 通过 `add-to-group` + `anchor` 控制注入位置 | Manifest `menu.order` + `menu.parent` |

### Vite / Webpack

| 模式 | 借鉴内容 | 应用位置 |
|------|----------|----------|
| Hook Pipeline（Rollup transform） | 链式处理，每个插件可修改前一个的输出 | 第 9.5 节 Filter 类型（管道链式） |
| First-wins 语义（Rollup resolveId） | 第一个返回结果的插件胜出 | 第 9.5 节 Command 类型（首个响应者胜出） |
| `enforce` 排序控制 | pre/post 控制插件执行顺序 | 事件订阅的 `priority` 参数 |
| 最小契约接口 | 插件 = name + hooks，无需继承 | `plugin.json` 作为唯一契约 |
| Plugin Context 共享状态 | 通过 `this` 传递工具方法，避免直接依赖 | `hohu` namespace API 注入 |
