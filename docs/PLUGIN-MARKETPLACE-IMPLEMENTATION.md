# Hohu 插件市场 Phase 1 实施计划

> 状态：待审查 | 创建日期：2026-06-02 | 对应设计文档：[PLUGIN-MARKETPLACE.md](./PLUGIN-MARKETPLACE.md)

## 概述

本文档为插件市场 Phase 1（低代码插件 MVP）的详细实施计划。目标：实现完整的端到端闭环——上传插件 → 安装 → 动态建表 → 通过低代码渲染引擎查看/管理数据 → 市场浏览与管理。

Phase 1 仅支持 `lowcode` 类型插件（纯 JSON Schema 配置，不执行任意代码），天然安全。

---

## 实施路线图

```
M1 (数据模型) → M2 (Schema) → M3 (服务层) → M4 (API层) → M5 (前端基建) → M6 (前端页面) → M7 (集成测试)
                                                    ↕
                                              M5 可与 M4 并行
```

---

## M1：模块脚手架 + 数据模型

### 1.1 创建模块目录

遵循现有模块结构（参考 `app/modules/system/`）：

```
app/modules/plugin/
├── __init__.py
├── api/__init__.py
├── models/__init__.py
├── schemas/__init__.py
└── service/__init__.py
```

### 1.2 创建 6 张数据表对应的 SQLAlchemy Model

每个 Model 遵循现有模式：
- `Base` 从 `app.db.base` 导入
- `next_id` 从 `app.core.id_generator` 导入
- 主键：`BigInteger + default=next_id`
- 审计字段：`create_by`、`create_time`、`update_by`、`update_time`（与现有 Model 一样逐个声明，无公共 Mixin）
- JSONB 字段：从 `sqlalchemy.dialects.postgresql` 导入

#### Plugin（插件包）

```python
# app/modules/plugin/models/plugin.py
__tablename__ = "plugin"
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `plugin_id` | BigInteger PK | Snowflake ID |
| `name` | VARCHAR(100) NOT NULL | 插件名称 |
| `slug` | VARCHAR(150) NOT NULL UNIQUE | author-slug 格式，全局唯一 |
| `type` | VARCHAR(20) NOT NULL | lowcode/frontend/backend/fullstack/theme |
| `description` | TEXT | 插件描述 |
| `icon` | VARCHAR(500) | 图标 URL |
| `author_id` | BigInteger FK → sys_user.user_id | 开发者 |
| `author_name` | VARCHAR(100) | 冗余展示用 |
| `status` | VARCHAR(20) NOT NULL DEFAULT 'draft' | draft/reviewing/published/archived/rejected |
| `current_version_id` | BigInteger FK → plugin_version.version_id (nullable) | 最新已发布版本 |
| `homepage` | VARCHAR(500) | 主页 |
| `license` | VARCHAR(50) | 开源协议 |
| `download_count` | INT DEFAULT 0 | 下载量 |
| `avg_rating` | DECIMAL(2,1) DEFAULT 0.0 | 平均评分 |
| `rating_count` | INT DEFAULT 0 | 评分人数 |
| 审计字段 ×4 | — | — |

#### PluginVersion（插件版本）

```python
# app/modules/plugin/models/plugin_version.py
__tablename__ = "plugin_version"
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `version_id` | BigInteger PK | Snowflake ID |
| `plugin_id` | BigInteger FK → plugin.plugin_id | 所属插件 |
| `version` | VARCHAR(20) NOT NULL | semver 版本号 |
| `changelog` | TEXT | 变更日志 |
| `manifest` | JSONB NOT NULL | 该版本的完整 manifest 快照 |
| `file_url` | VARCHAR(500) NOT NULL | 对象存储路径 |
| `file_hash` | VARCHAR(64) NOT NULL | SHA-256 |
| `file_size` | BigInteger | 文件大小（字节） |
| `review_status` | VARCHAR(20) NOT NULL DEFAULT 'pending' | pending/approved/rejected |
| `review_id` | BigInteger FK → plugin_review.review_id (nullable) | 关联审核记录 |
| `create_time` | DateTime | — |

**唯一约束**：`UNIQUE(plugin_id, version)`，需使用 `__table_args__` 定义。

#### PluginReview（审核记录）

```python
# app/modules/plugin/models/plugin_review.py
__tablename__ = "plugin_review"
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `review_id` | BigInteger PK | — |
| `plugin_id` | BigInteger FK | — |
| `version_id` | BigInteger FK | — |
| `rule_check_result` | JSONB | 第 1 层规则检查结果 |
| `rule_check_at` | DateTime | — |
| `ai_risk_level` | VARCHAR(10) | low/medium/high/pending/skipped |
| `ai_report` | JSONB | AI 审核报告 |
| `ai_review_at` | DateTime | — |
| `human_status` | VARCHAR(20) DEFAULT 'pending' | pending/approved/rejected/skipped |
| `human_reviewer_id` | BigInteger FK (nullable) | — |
| `human_comment` | TEXT | 审核意见 |
| `human_reviewed_at` | DateTime | — |
| `final_status` | VARCHAR(20) NOT NULL DEFAULT 'pending' | pending/approved/rejected |
| `create_time`、`update_time` | DateTime | — |

#### TenantPlugin（租户安装记录）

```python
# app/modules/plugin/models/tenant_plugin.py
__tablename__ = "tenant_plugin"
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | BigInteger PK | — |
| `tenant_id` | BigInteger DEFAULT 0 | 预留多租户 |
| `plugin_id` | BigInteger FK | — |
| `installed_version` | VARCHAR(20) NOT NULL | 安装的版本号 |
| `status` | VARCHAR(20) NOT NULL DEFAULT 'disabled' | installed/enabled/disabled/uninstalled |
| `config` | JSONB | 管理员填写的配置 |
| `approved_permissions` | JSONB | 审批通过的权限子集 |
| `archived_table_name` | VARCHAR(100) | 卸载后记录数据表名 |
| `has_data` | BOOLEAN DEFAULT false | 卸载时是否有历史数据 |
| `installed_at` | DateTime | — |
| `update_time` | DateTime | — |

**唯一约束**：`UNIQUE(tenant_id, plugin_id)`

#### PluginPermission（权限声明）

```python
# app/modules/plugin/models/plugin_permission.py
__tablename__ = "plugin_permission"
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | BigInteger PK | — |
| `plugin_id` | BigInteger FK | — |
| `permission_type` | VARCHAR(30) NOT NULL | api/external_api/menu/db_table/... |
| `permission_detail` | JSONB NOT NULL | 权限详情 |
| `permission_detail_hash` | VARCHAR(64) NOT NULL | SHA-256 哈希，用于唯一约束 |
| `create_time` | DateTime | — |

**唯一约束**：`UNIQUE(plugin_id, permission_type, permission_detail_hash)`

#### PluginRating（评分评论）

```python
# app/modules/plugin/models/plugin_rating.py
__tablename__ = "plugin_rating"
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | BigInteger PK | — |
| `plugin_id` | BigInteger FK | — |
| `user_id` | BigInteger FK | — |
| `rating` | SmallInt NOT NULL CHECK(1-5) | 评分 |
| `comment` | TEXT | 评论 |
| `create_time`、`update_time` | DateTime | — |

**唯一约束**：`UNIQUE(plugin_id, user_id)`

### 1.3 注册 Alembic 迁移

修改 `alembic/env.py`，添加所有 plugin model 的导入。执行：

```bash
cd hohu-admin
alembic revision --autogenerate -m "add plugin tables"
alembic upgrade head
```

---

## M2：Pydantic Schema 层

### 2.1 实体 Schema（每个 Model 对应一个文件）

每个 Schema 文件遵循现有模式（参考 `app/modules/system/schemas/role.py`）：
- `alias_generator=to_camel, populate_by_name=True`
- `@field_serializer` 处理 BigInteger → string 和 datetime 格式化
- 输出 Schema 加 `from_attributes=True`

| 文件 | 包含的 Schema |
|------|---------------|
| `schemas/plugin.py` | PluginCreate, PluginUpdate, PluginQuery, PluginOut, PluginSimpleOut |
| `schemas/plugin_version.py` | PluginVersionCreate, PluginVersionOut |
| `schemas/plugin_review.py` | PluginReviewOut, ReviewActionIn |
| `schemas/tenant_plugin.py` | InstallPluginIn, UpdatePluginConfigIn, TenantPluginOut, TenantPluginQuery |
| `schemas/plugin_rating.py` | RatingCreate, RatingOut |
| `schemas/plugin_permission.py` | PermissionOut |

### 2.2 Manifest 校验 Schema

创建 `schemas/manifest.py` — 用 Pydantic 校验 `plugin.json` 结构：

```python
class PluginManifest(BaseModel):
    name: str
    slug: str                                    # 格式：^[a-z0-9]+-[a-z0-9-]+$
    version: str                                 # semver 格式
    type: Literal["lowcode"]                     # Phase 1 仅支持 lowcode
    author: str
    engines: EnginesConfig                       # { hohu: ">=1.0.0 <2.0.0" }
    permissions: list[PermissionDecl] = []
    config_schema: dict | None = None            # JSON Schema 格式的配置定义
    data_schema: dict | None = None              # 单表插件
    models: list[ModelDef] | None = None         # 多表插件
    pages: list[PageDef]                         # 页面定义
    menu: MenuDef                                # 菜单声明
    hooks: dict | None = None                    # 生命周期钩子
    events: dict | None = None                   # 事件声明
    marketplace: MarketplaceDef | None = None    # 市场展示信息
```

校验规则：
- slug 格式必须为 `author-slug`
- version 必须符合 semver
- 每个 page 的 `model` key 必须在 `models` 数组中存在（或无 models 时自动引用顶层 data_schema）
- index 中声明的字段必须存在于 data_schema
- x-ref 目标必须指向已存在的 model key

---

## M3：服务层

### 3.1 Manifest 校验器

**文件**：`service/manifest_validator.py`

- 输入：解析后的 dict（plugin.json）
- 输出：`ValidationResult(passed: bool, errors: list, warnings: list)`
- 校验内容：Pydantic schema 校验 + 语义校验（page-model 交叉引用、索引字段存在性等）
- 调用时机：上传时、安装前

### 3.2 动态表服务

**文件**：`service/dynamic_table_service.py`

这是整个系统技术复杂度最高的部分。

#### 建表（`create_plugin_tables`）

```
输入：slug, manifest
├── 解析 models（或顶层 data_schema）
├── 为每个 model 构建建表 DDL
│   ├── 系统列：id(BigInteger PK Snowflake), created_at, updated_at, created_by, updated_by
│   ├── 业务列：根据 JSON Schema → PostgreSQL 类型映射
│   │   ├── string → VARCHAR / TEXT
│   │   ├── integer → INTEGER
│   │   ├── number → NUMERIC
│   │   ├── boolean → BOOLEAN
│   │   ├── string(format:date) → DATE
│   │   ├── string(format:datetime) → TIMESTAMPTZ
│   │   └── array / object → JSONB
│   └── 声明的索引：CREATE INDEX ...
└── 通过 db.execute(text("CREATE TABLE ...")) 执行
```

表名规则：
- 有 models：`plugin_data_{slug}_{model_key}`
- 无 models：`plugin_data_{slug}`

#### 动态 CRUD

```
query_records(slug, model_key, params) → 分页列表
├── 构造 SELECT ... FROM plugin_data_{slug}_{model_key}
├── 支持字段过滤（精确匹配、contains）
├── 支持 JSONB array contains 查询
├── 支持 expand 参数（belongs_to JOIN）
└── 分页：OFFSET/LIMIT

insert_record / update_record / delete_record
├── 动态构建 INSERT/UPDATE/DELETE SQL
└── 自动填充 created_by/updated_by
```

#### 卸载

- 软删除：在 tenant_plugin 记录 `archived_table_name` + `has_data=true`
- 硬删除：`DROP TABLE`（需超级管理员二次确认）

### 3.3 插件服务

**文件**：`service/plugin_service.py`

标准 CRUD，遵循 `RoleService` 模式：
- `get_plugin_list(db, query)` — 分页查询，按 name/type/status 筛选
- `get_plugin_detail(db, plugin_id)` — 含当前版本的 manifest
- `create_plugin(db, ...)` — 创建记录
- `update_plugin(db, plugin_id, ...)` — 更新基本信息
- `delete_plugin(db, plugin_id)` — 仅在无租户安装时可删
- 模块级单例：`plugin_service = PluginService()`

### 3.4 租户插件服务

**文件**：`service/tenant_plugin_service.py`

安装/卸载/启停的生命周期编排器。

#### 安装流程

```
install_plugin(db, tenant_id, plugin_id, version_id, approved_permissions)
├── 1. 校验插件存在且已发布
├── 2. 校验版本已审核通过
├── 3. 校验未重复安装（或处理重装逻辑）
├── 4. 解析 manifest
├── 5. 调用 dynamic_table_service.create_plugin_tables() 建表
├── 6. 创建 tenant_plugin 记录
└── 7. 递增 plugin.download_count
```

#### 卸载流程

```
uninstall_plugin(db, tenant_id, plugin_id, hard_delete=False)
├── 1. 检查是否有其他插件依赖
├── 2. 软删除：标记 status='uninstalled'，记录 archived_table_name
└── 3. 硬删除：DROP TABLE + 删除 tenant_plugin 记录
```

#### 其他方法

- `enable_plugin` / `disable_plugin` — 切换状态
- `update_config` — 更新 config JSONB
- `get_installed_plugins` — 分页查询已安装列表

### 3.5 Manifest 聚合服务

**文件**：`service/manifest_aggregation_service.py`

为前端提供菜单/路由的聚合缓存。

```python
get_active_contributes(db, tenant_id) -> dict
├── 查询所有 status='enabled' 的 tenant_plugin
├── 解析每个插件的 manifest
├── 构建 flat contributes:
│   ├── menus: [{name, path, component, meta}]
│   └── routes: [{name, path, component, meta}]
└── 返回 { menus: [...], routes: [...] }
```

插件路由格式：
- 目录路由（菜单）：`name=slug, path="/plugin/{slug}", component="layout.base"`
- 页面路由：`name="{slug}_{pageKey}", path="/plugin/{slug}/{pageKey}", component="view.plugin_page"`

### 3.6 审核服务 + 规则检查器

**文件**：`service/plugin_review_service.py` + `service/review_rule_checker.py`

规则检查器（Layer 1）校验内容：
- plugin.json 存在且为合法 JSON
- 必填字段完整（name/slug/version/type）
- slug 不与已有插件冲突
- semver 格式合法
- 文件大小 ≤ 50MB
- 低代码：JSON Schema 合法性

审核服务：
- 上传时自动触发规则检查
- 创建 plugin_review 记录
- MVP 阶段仅实现 Layer 1（规则检查），AI 审核（Layer 2）和人工审核（Layer 3）的完整流程留后续迭代

### 3.7 评分服务

**文件**：`service/plugin_rating_service.py`

- 创建/更新评分（每个用户每个插件一条）
- 校验用户已安装该插件才能评分
- 评分变更时更新 plugin 的 avg_rating 和 rating_count

---

## M4：API 层

### 4.1 创建 7 个 API Router

每个 Router 遵循现有模式（参考 `app/modules/system/api/role.py`）。

#### 插件管理 `/plugin/plugin`

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/list` | plugin:plugin:list | 分页列表 |
| GET | `/{plugin_id}` | plugin:plugin:query | 详情 |
| POST | `/add` | plugin:plugin:add | 创建（version 在上传接口创建） |
| PUT | `/{plugin_id}` | plugin:plugin:edit | 更新基本信息 |
| DELETE | `/{plugin_id}` | plugin:plugin:delete | 删除（无安装记录时） |

#### 版本管理 `/plugin/version`

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| POST | `/upload` | plugin:version:upload | 上传 zip 包，解析 manifest，创建 plugin + version + review |
| GET | `/list` | — | 指定插件的版本列表 |
| GET | `/{version_id}` | — | 版本详情 |

**上传流程**：
```
POST /plugin/version/upload (multipart/form-data, file=xxx.zip)
├── 1. 接收 .zip 文件
├── 2. 解压，解析 plugin.json
├── 3. manifest_validator 校验
├── 4. 计算 SHA-256
├── 5. 存储文件（复用现有文件存储模式）
├── 6. 创建 plugin + plugin_version 记录
└── 7. 触发规则检查，创建 plugin_review
```

#### 审核 `/plugin/review`

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/list` | plugin:review:list | 审核队列 |
| POST | `/{review_id}/approve` | plugin:review:approve | 通过 |
| POST | `/{review_id}/reject` | plugin:review:reject | 驳回（附原因） |

#### 租户插件 `/plugin/tenant`

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| POST | `/install` | plugin:tenant:install | 安装 |
| POST | `/uninstall` | plugin:tenant:uninstall | 卸载 |
| PUT | `/{id}/enable` | plugin:tenant:enable | 启用 |
| PUT | `/{id}/disable` | plugin:tenant:disable | 禁用 |
| PUT | `/{id}/config` | plugin:tenant:config | 更新配置 |
| GET | `/installed` | — | 已安装列表 |

#### 动态数据 `/plugin-data`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/{slug}/{model_key}` | 创建记录 |
| GET | `/{slug}/{model_key}` | 分页查询 |
| GET | `/{slug}/{model_key}/{record_id}` | 获取单条 |
| PUT | `/{slug}/{model_key}/{record_id}` | 更新 |
| DELETE | `/{slug}/{model_key}/{record_id}` | 删除 |
| GET | `/{slug}/{model_key}/export` | 导出（json/csv） |

访问前校验：插件已安装且已启用。

#### 市场 `/plugin/marketplace`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/browse` | 公开浏览（搜索、分类筛选） |
| GET | `/{plugin_id}` | 插件详情 |
| GET | `/contributes` | 当前租户的活跃插件菜单/路由聚合 |

#### 评分 `/plugin/rating`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/` | 创建评分（需已安装） |
| GET | `/list` | 指定插件的评分列表 |
| PUT | `/{rating_id}` | 更新自己的评分 |

### 4.2 注册路由

修改 `app/main.py`，添加所有 plugin router 的 include。

### 4.3 插件菜单注入 getUserRoutes

**关键改动**：修改 `app/modules/auth/api.py` 的 `get_user_routes` 端点。

在构建系统菜单树之后，调用 `manifest_aggregation_service.get_active_contributes(db, tenant_id=0)` 获取活跃插件，将插件的菜单和页面路由转换为 `UserRoute` 对象，追加到路由树中。

```python
# 伪代码
route_tree = build_menu_tree(menu_list, 0)

# 注入插件路由
plugin_routes = await manifest_aggregation_service.get_active_contributes(db, tenant_id=0)
for pr in plugin_routes:
    route_tree.append(pr)  # UserRoute 格式
```

插件路由结构示例：
```python
UserRoute(
    name="crm-suite",
    path="/plugin/crm-suite",
    component="layout.base",
    meta=RouteMeta(title="CRM", icon="BriefcaseOutline", order=200),
    children=[
        UserRoute(
            name="crm-suite_customer-list",
            path="/plugin/crm-suite/customer-list",
            component="view.plugin_page",
            meta=RouteMeta(title="客户列表")
        ),
        # ...更多页面
    ]
)
```

---

## M5：前端基础设施

### 5.1 类型定义

**文件**：`src/typings/api/plugin.d.ts`

```typescript
declare namespace Api {
  namespace Plugin {
    // 插件实体
    type Plugin = Common.CommonRecord<{
      pluginId: string;
      name: string;
      slug: string;
      type: string;
      description: string;
      icon: string | null;
      authorId: string;
      authorName: string;
      status: string;
      currentVersionId: string | null;
      homepage: string | null;
      license: string | null;
      downloadCount: number;
      avgRating: number;
      ratingCount: number;
    }>;

    type PluginList = Common.PaginatingQueryRecord<Plugin>;
    type PluginSearchParams = CommonType.RecordNullable<
      Pick<Plugin, 'name' | 'type' | 'status'> & Common.CommonSearchParams
    >;

    // 版本、安装记录、动态数据等类型...
    // Manifest 类型（供低代码渲染器使用）
    type Manifest = { /* ... */ };
    type ModelDef = { /* ... */ };
    type PageDef = { /* ... */ };
    type MenuDef = { /* ... */ };
  }
}
```

### 5.2 API 服务层

**文件**：`src/service/api/plugin.ts`

遵循现有命名模式 `fetch<Verb><Entity>`：

```typescript
export function fetchGetPluginList(params?) { /* GET /plugin/plugin/list */ }
export function fetchUploadPlugin(file: File) { /* POST /plugin/version/upload */ }
export function fetchInstallPlugin(data) { /* POST /plugin/tenant/install */ }
export function fetchUninstallPlugin(data) { /* POST /plugin/tenant/uninstall */ }
export function fetchGetInstalledPlugins(params?) { /* GET /plugin/tenant/installed */ }
export function fetchEnablePlugin(id: string) { /* PUT /plugin/tenant/{id}/enable */ }
export function fetchDisablePlugin(id: string) { /* PUT /plugin/tenant/{id}/disable */ }
export function fetchUpdatePluginConfig(id: string, config) { /* PUT /plugin/tenant/{id}/config */ }
export function fetchGetPluginData(slug, modelKey, params?) { /* GET /plugin-data/{slug}/{modelKey} */ }
export function fetchCreatePluginData(slug, modelKey, data) { /* POST */ }
export function fetchUpdatePluginData(slug, modelKey, id, data) { /* PUT */ }
export function fetchDeletePluginData(slug, modelKey, id) { /* DELETE */ }
export function fetchGetContributes() { /* GET /plugin/marketplace/contributes */ }
export function fetchBrowsePlugins(params?) { /* GET /plugin/marketplace/browse */ }
export function fetchGetPluginDetail(id) { /* GET /plugin/marketplace/{id} */ }
```

修改 `src/service/api/index.ts` 添加 `export * from './plugin'`。

### 5.3 插件页面组件注册

**关键改动**：修改 `src/router/routes/index.ts` 的 `getAuthVueRoutes` 函数。

后端注入的插件路由使用 `component="view.plugin_page"`，前端需要将这个 component 字符串映射到低代码渲染器组件：

```typescript
export function getAuthVueRoutes(routes: ElegantConstRoute[]) {
  const extendedViews = {
    ...views,
    'plugin_page': () => import('@/views/plugin/_plugin-page/index.vue'),
  };
  return transformElegantRoutesToVueRoutes(routes, layouts, extendedViews);
}
```

这样 `transform.ts` 在解析 `view.plugin_page` 时能找到对应的 Vue 组件，不会抛错。

---

## M6：前端页面

### 6.1 市场浏览页

遵循 `system/role` 的三文件模式：

```
src/views/plugin/marketplace/
├── index.vue                           # 插件卡片网格 + 搜索
└── modules/
    ├── plugin-search.vue               # 搜索表单（分类、类型、关键词）
    └── plugin-card.vue                 # 单个插件卡片组件
```

### 6.2 插件详情页

```
src/views/plugin/marketplace/detail/
└── [id].vue                            # 详情页（基本信息、截图、安装/管理按钮）
```

### 6.3 已安装管理页

```
src/views/plugin/installed/
├── index.vue                           # 已安装列表（状态、启用/禁用/配置/卸载操作）
└── modules/
    └── plugin-config-drawer.vue        # 配置抽屉（动态渲染 config_schema）
```

### 6.4 低代码渲染引擎

这是前端最核心的部分。

```
src/views/plugin/_plugin-page/
└── index.vue                           # 入口：根据 slug+pageKey 加载 manifest，派发给子渲染器

src/views/plugin/_lowcode-renderer/
├── table-renderer.vue                  # 动态表格页
├── form-renderer.vue                   # 动态表单页
├── detail-renderer.vue                 # 动态详情页
└── composables/
    ├── use-schema-parser.ts            # JSON Schema → NaiveUI 组件映射
    └── use-plugin-data.ts              # 动态数据 CRUD hooks
```

#### `_plugin-page/index.vue`（入口组件）

```
路由：/plugin/:slug/:pageKey
├── 根据 slug 从后端加载 manifest
├── 根据 pageKey 找到对应的 page 定义
├── 根据 page.page_type 派发渲染：
│   ├── "table" → <TableRenderer :page="page" :schema="schema" :slug="slug" />
│   ├── "form" → <FormRenderer :page="page" :schema="schema" :slug="slug" />
│   └── "detail" → <DetailRenderer :page="page" :schema="schema" />
```

#### `use-schema-parser.ts`（Schema 解析器）

核心映射逻辑：

**表格列解析**：`parseTableColumns(dataSchema, uiSchema)`
```
JSON Schema type → NaiveUI DataTable column
├── string     → 文本列（默认）
├── string + enum → NTag 列（状态标签）
├── number     → 数字列
├── boolean    → NSwitch 列（只读）
├── string(format:date) → 格式化日期列
├── array      → 展开为标签列表
└── x-ref 字段 → 显示关联模型的 label_field
```

**表单字段解析**：`parseFormFields(dataSchema, uiSchema)`
```
JSON Schema type → NaiveUI 表单控件
├── string                    → NInput
├── string + enum             → NSelect
├── string(format:date)       → NDatePicker
├── string(format:datetime)   → NDatePicker(type="datetime")
├── string(format:phone)      → NInput(mask)
├── number                    → NInputNumber
├── boolean                   → NSwitch
├── array                     → NCheckboxGroup
├── object                    → 嵌套表单 / JSON 编辑器
└── x-ref 字段                → NSelect（异步加载关联数据）
```

JSON Schema 校验规则 → NaiveUI 表单规则：
- `required` → `defaultRequiredRule`
- `minLength`/`maxLength` → 长度校验
- `minimum`/`maximum` → 范围校验
- `pattern` → 正则校验

#### `table-renderer.vue`（动态表格）

```
├── 使用 use-plugin-data 获取分页数据
├── 使用 use-schema-parser 构建 columns
├── 支持声明的 actions（navigate、confirm、notification、form_submit）
├── 支持 ui_schema 中的 filterable 筛选
└── 支持 expand 参数展开关联数据
```

#### `form-renderer.vue`（动态表单）

```
├── 使用 use-schema-parser 构建 formItems
├── 支持验证规则（从 JSON Schema 推导）
├── 提交 → POST/PUT 动态数据 API
└── 取消 → navigate 回列表页
```

### 6.5 路由生成

创建完所有视图文件后，执行 `pnpm gen-route` 注册系统路由（市场浏览页、已安装管理页等）。

插件动态路由（`/plugin/:slug/:pageKey`）由后端注入，不走 `@elegant-router`。

---

## M7：集成测试

### 7.1 审核流程串联

- 上传时自动触发规则检查
- 管理员审核列表页（复用 table 模式）

### 7.2 端到端测试流程

手动测试序列：

```
1. 构造一个示例 plugin.json + 打包为 zip
2. POST /plugin/version/upload 上传
   → 验证 plugin + plugin_version + plugin_review 记录已创建
3. POST /plugin/review/{id}/approve 通过审核
   → 验证 plugin.status 变为 published
4. POST /plugin/tenant/install 安装
   → 验证 tenant_plugin 记录已创建
   → 验证动态表 plugin_data_{slug} 已创建（\dt 检查）
5. PUT /plugin/tenant/{id}/enable 启用
6. GET /plugin/marketplace/contributes 获取聚合路由
   → 验证返回了插件的菜单和页面路由
7. GET /auth/getUserRoutes 获取用户路由
   → 验证插件菜单已出现在路由树中
8. 前端验证：侧边栏显示插件菜单，点击可进入低代码页面
9. POST /plugin-data/{slug}/{model_key} 创建记录
10. GET /plugin-data/{slug}/{model_key} 查询记录
    → 验证分页、过滤、关联展开
11. POST /plugin/tenant/uninstall 卸载
    → 验证 archived_table_name 已记录
```

---

## 文件清单

### 后端新增文件（约 22 个）

| 类别 | 文件路径 |
|------|----------|
| 模型 ×6 | `app/modules/plugin/models/plugin.py` |
| | `app/modules/plugin/models/plugin_version.py` |
| | `app/modules/plugin/models/plugin_review.py` |
| | `app/modules/plugin/models/tenant_plugin.py` |
| | `app/modules/plugin/models/plugin_permission.py` |
| | `app/modules/plugin/models/plugin_rating.py` |
| Schema ×7 | `app/modules/plugin/schemas/plugin.py` |
| | `app/modules/plugin/schemas/plugin_version.py` |
| | `app/modules/plugin/schemas/plugin_review.py` |
| | `app/modules/plugin/schemas/tenant_plugin.py` |
| | `app/modules/plugin/schemas/plugin_rating.py` |
| | `app/modules/plugin/schemas/plugin_permission.py` |
| | `app/modules/plugin/schemas/manifest.py` |
| 服务 ×7 | `app/modules/plugin/service/manifest_validator.py` |
| | `app/modules/plugin/service/dynamic_table_service.py` |
| | `app/modules/plugin/service/plugin_service.py` |
| | `app/modules/plugin/service/tenant_plugin_service.py` |
| | `app/modules/plugin/service/manifest_aggregation_service.py` |
| | `app/modules/plugin/service/plugin_review_service.py` |
| | `app/modules/plugin/service/review_rule_checker.py` |
| | `app/modules/plugin/service/plugin_rating_service.py` |
| API ×7 | `app/modules/plugin/api/plugin.py` |
| | `app/modules/plugin/api/plugin_version.py` |
| | `app/modules/plugin/api/plugin_review.py` |
| | `app/modules/plugin/api/tenant_plugin.py` |
| | `app/modules/plugin/api/plugin_data.py` |
| | `app/modules/plugin/api/marketplace.py` |
| | `app/modules/plugin/api/plugin_rating.py` |

### 后端修改文件

| 文件 | 改动 |
|------|------|
| `alembic/env.py` | 添加 plugin model 导入 |
| `app/main.py` | 注册 7 个 plugin router |
| `app/modules/auth/api.py` | getUserRoutes 中注入插件菜单 |

### 前端新增文件（约 14 个）

| 文件路径 | 说明 |
|----------|------|
| `src/typings/api/plugin.d.ts` | 类型定义 |
| `src/service/api/plugin.ts` | API 服务 |
| `src/views/plugin/marketplace/index.vue` | 市场浏览页 |
| `src/views/plugin/marketplace/modules/plugin-search.vue` | 搜索表单 |
| `src/views/plugin/marketplace/modules/plugin-card.vue` | 插件卡片 |
| `src/views/plugin/marketplace/detail/[id].vue` | 插件详情页 |
| `src/views/plugin/installed/index.vue` | 已安装管理页 |
| `src/views/plugin/installed/modules/plugin-config-drawer.vue` | 配置抽屉 |
| `src/views/plugin/_plugin-page/index.vue` | 低代码渲染入口 |
| `src/views/plugin/_lowcode-renderer/table-renderer.vue` | 表格渲染器 |
| `src/views/plugin/_lowcode-renderer/form-renderer.vue` | 表单渲染器 |
| `src/views/plugin/_lowcode-renderer/detail-renderer.vue` | 详情渲染器 |
| `src/views/plugin/_lowcode-renderer/composables/use-schema-parser.ts` | Schema 解析 |
| `src/views/plugin/_lowcode-renderer/composables/use-plugin-data.ts` | 数据 CRUD hooks |

### 前端修改文件

| 文件 | 改动 |
|------|------|
| `src/service/api/index.ts` | 添加 plugin 导出 |
| `src/router/routes/index.ts` | getAuthVueRoutes 中注册 plugin_page 组件映射 |

---

## 验证策略

每个 Milestone 完成后执行：
- 后端：`cd hohu-admin && ruff check . && ruff format .`
- 前端：`cd hohu-admin-web && pnpm lint && pnpm typecheck`

最终验证：按 M7 端到端测试流程逐项验证。

---

## 风险点

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 动态表 DDL 在高并发下的锁竞争 | 安装时建表可能阻塞 | 安装操作限制为管理员 + 低频操作 |
| JSON Schema → NaiveUI 映射不完整 | 复杂表单无法渲染 | Phase 1 覆盖常用类型，edge case 给出降级提示 |
| `@elegant-router` 对动态路由的支持 | 路由注入方式不兼容 | 通过 getUserRoutes 注入，绕过 elegant-router |
| 大量插件时 manifest 聚合缓存性能 | 首次加载慢 | 后端聚合缓存 + 前端一次加载 |
