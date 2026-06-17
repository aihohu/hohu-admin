# hohu-admin 架构规划：插件化生态 + MCP + 应用商店

## 愿景

打造一个开源管理后台**平台 + 生态**：
- 核心是一个通用的 FastAPI 管理后台框架（hohu-admin-core）
- 用户通过**应用商店**按需安装业务模块（CRM、ERP、OA、HR 等）
- 鼓励社区开发者构建和发布第三方模块，形成类似 VS Code 的插件生态
- 集成 MCP（Model Context Protocol），让 AI 助手能够操作后台

---

## 仓库策略

**后端单仓库 + 前端独立仓库**

- `hohu-admin`：后端（FastAPI），包含核心框架 + 所有官方模块 + 应用商店后端
- `hohu-web`：前端（Vue 3），独立仓库，通过 OpenAPI spec 同步类型

理由：
- Python 和 TypeScript 生态差异大，合并不能真正共享类型
- 后端所有模块需要共享 auth/permission/基础设施
- 前后端独立部署，各有各的发布节奏
- 对开源贡献者更友好，各取所需

---

## 架构全景图

```
┌──────────────────────────────────────────────────────────────┐
│                        生态系统                               │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ 模块注册中心  │  │ 开发者文档站  │  │  CLI 工具    │       │
│  │ registry.json│  │ API 参考     │  │ hohu create  │       │
│  │ 模块元数据   │  │ 开发指南     │  │ hohu dev     │       │
│  │ 下载统计/评分 │  │ 最佳实践     │  │ hohu publish │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│                  hohu-admin-core（核心框架）                    │
│                                                              │
│  模块加载器          认证授权            应用商店 API          │
│  ├── 自动发现        ├── JWT             ├── 模块列表          │
│  ├── 依赖检查        ├── RBAC            ├── 一键安装          │
│  ├── 热加载(远期)    └── 权限管理         └── 版本更新          │
│  └── 版本管理                                                 │
│                                                              │
│  Module API          共享基础设施          数据库管理          │
│  ├── Router          ├── 分页             ├── Base           │
│  ├── Model           ├── 异常体系         ├── Session        │
│  ├── Schema          ├── ID 生成器        ├── 迁移           │
│  ├── Menu            ├── 数据脱敏         └── 种子数据        │
│  └── Permission      └── 响应格式                             │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│                         模块层                                │
│                                                              │
│  system(内置)  crm(官方)  erp(官方)  mcp(官方)  oa(社区)  ...  │
└──────────────────────────────────────────────────────────────┘
```

---

## Module API 设计（核心协议）

### ModuleDefinition 声明

```python
# 每个模块在 __init__.py 中导出 module 对象
from hohu_admin.module_api import ModuleDefinition, MenuDefinition

module = ModuleDefinition(
    name="crm",                              # 模块唯一标识
    display_name="CRM客户管理",               # 显示名称
    version="0.1.0",                         # 版本号
    category="business",                     # 分类：business | tool | integration
    icon="mdi:account-group",                # 图标
    description="客户管理、商机跟踪、合同管理", # 描述
    author="hohu",                           # 作者
    dependencies=["system"],                 # 依赖的其他模块

    # 路由注册
    routers=[
        (customer_router, "/crm/customer", "客户管理"),
        (deal_router, "/crm/deal", "商机管理"),
    ],

    # 数据模型模块（用于 Alembic 自动发现）
    models_module="app.modules.crm.models",

    # 菜单声明（安装时自动创建到 sys_menu）
    menus=[
        MenuDefinition(name="CRM", path="/crm", icon="account-group", children=[
            MenuDefinition(name="客户管理", path="/crm/customer",
                          component="crm/customer/index",
                          permission="crm:customer:list"),
            MenuDefinition(name="商机管理", path="/crm/deal",
                          component="crm/deal/index",
                          permission="crm:deal:list"),
        ])
    ],

    # 权限声明（安装时自动创建到 sys_menu type=F）
    permissions=[
        "crm:customer:list", "crm:customer:create", "crm:customer:edit", "crm:customer:delete",
        "crm:deal:list", "crm:deal:create", "crm:deal:edit", "crm:deal:delete",
    ],
)
```

### 模块能力边界

```
模块能做的：
  ✅ 注册 API 路由
  ✅ 定义数据模型（ORM Model）
  ✅ 声明菜单和权限
  ✅ 定义 Pydantic Schema
  ✅ 使用核心的认证/授权（get_current_user, require_permissions）
  ✅ 使用核心的分页工具（paginate, build_filters）
  ✅ 使用核心的异常体系
  ✅ 使用核心的 ID 生成器
  ✅ 声明模块间依赖
  ✅ 提供安装/卸载钩子
  ✅ 定义配置项（模块自己的 settings）
  ✅ 注册定时任务（远期）
  ✅ 注册事件监听（远期）

模块不能做的：
  ❌ 修改核心代码
  ❌ 直接操作其他模块的数据库表
  ❌ 覆盖核心路由
  ❌ 修改其他模块的配置
```

### 模块生命周期钩子

```python
@module.on_install
async def on_install(db: AsyncSession):
    """首次安装时执行：创建种子数据"""
    ...

@module.on_uninstall
async def on_uninstall(db: AsyncSession):
    """卸载时清理数据"""
    ...

@module.on_upgrade
async def on_upgrade(db: AsyncSession, old_version: str, new_version: str):
    """版本升级时执行数据迁移"""
    ...
```

---

## 目录结构

### 后端仓库（hohu-admin）

```
hohu-admin/
  app/
    core/
      module_registry.py          # [新增] ModuleDefinition + 模块加载器
      module_loader.py            # [新增] 动态发现和加载模块
      config.py                   # [修改] 添加 INSTALLED_MODULES
      auth.py                     # 权限检查
      base_response.py            # 响应格式
      exceptions.py               # 异常体系
      security.py                 # 密码 + JWT
      id_generator.py             # ID 生成
      redis.py                    # Redis 连接
    db/
      base.py                     # DeclarativeBase + 关联表
      session.py                  # 数据库会话
    modules/
      system/                     # 系统管理（内置，始终启用）
        __init__.py               # ModuleDefinition 声明
        api/, models/, schemas/, service/
      mcp/                        # MCP Server（可选模块）
        __init__.py               # ModuleDefinition 声明
        server.py                 # MCP Server 入口
        tools/
          system_tools.py         # 系统管理工具
          data_analysis_tools.py  # 数据分析工具
      crm/                        # CRM（官方可选模块，远期）
        __init__.py
        api/, models/, schemas/, service/
    middleware/
      rate_limit_middleware.py
    constants/
    utils/
      pagination.py
      mask_util.py
    store/                        # [新增] 应用商店
      api.py                      # 商店 API 端点
      service.py                  # 安装/卸载/更新逻辑
      registry_client.py          # 模块注册中心客户端
      models.py                   # InstalledModule 模型
    main.py                       # [修改] 动态加载模块
  cli/                            # [远期新增] 开发者 CLI 工具
    __init__.py
    create.py                     # 脚手架
    dev.py                        # 本地开发
    publish.py                    # 发布
  alembic/
    env.py                        # [修改] 动态导入活跃模块 models
```

---

## 应用商店设计

### 安装流程

```
用户在管理后台点击"安装 CRM"
     │
     ▼
1. 后端从模块注册中心获取 CRM 的元数据和下载地址
2. 下载模块代码（pip install 或 git clone）
3. 安装 Python 依赖
4. 执行数据库迁移（建表）
5. 执行 on_install 钩子（创建菜单、权限、种子数据）
6. 记录到 sys_installed_module 表
7. 标记"需要重启"，提示用户
     │
     ▼
用户重启服务，CRM 模块被加载，前端看到新菜单
```

### 应用商店 API

```
GET  /store/modules                  # 获取可用模块列表（从 registry 拉取）
GET  /store/modules/{name}           # 获取模块详情
GET  /store/installed                # 获取已安装模块列表
POST /store/install/{name}           # 安装模块（仅超级管理员）
POST /store/uninstall/{name}         # 卸载模块
GET  /store/check-updates           # 检查模块更新
POST /store/update/{name}           # 更新模块
POST /store/refresh-registry        # 刷新注册中心缓存
```

### 数据库表

```python
class InstalledModule(Base):
    __tablename__ = "sys_installed_module"
    module_id: int          # PK (Snowflake ID)
    module_name: str        # "crm"
    display_name: str       # "CRM"
    version: str            # "0.1.0"
    category: str           # "business"
    status: str             # "installed" / "disabled" / "pending_restart"
    source: str             # "pypi" / "git" / "local" / "registry"
    source_url: str         # 下载地址
    config: dict            # JSON，模块自定义配置
    author: str
    description: str
    icon: str
    install_time: str
    update_time: str
```

### 模块注册中心

初期用 GitHub 仓库实现：

```
hohu-admin-registry/
  registry.json            # 所有可用模块的元数据
  modules/
    crm/
      hohu_module.yaml     # 模块元数据
      screenshots/         # 截图
```

模块作者提 PR 新增模块，官方审核后合并。

### 前端应用商店页面

```
┌──────────────────────────────────────────────────────────┐
│  应用商店                              [刷新] [已安装]     │
├──────────────────────────────────────────────────────────┤
│  分类: [全部] [业务] [工具] [集成]                         │
│  搜索: [________________]                                 │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
│  │  📦          │  │  📊          │  │  🤖          │      │
│  │  CRM         │  │  ERP         │  │  MCP 助手    │      │
│  │  客户关系管理 │  │  进销存管理   │  │  AI 智能助手  │      │
│  │             │  │             │  │  ✅ 已安装    │      │
│  │  [安装]     │  │  [安装]     │  │  [设置]     │      │
│  └─────────────┘  └─────────────┘  └─────────────┘      │
└──────────────────────────────────────────────────────────┘
```

---

## MCP Server 集成

### 作为第一个官方可选模块

```python
# app/modules/mcp/__init__.py
module = ModuleDefinition(
    name="mcp",
    display_name="MCP智能助手",
    category="integration",
    dependencies=["system"],
)
```

### MCP 工具定义

```python
# 直接复用现有 Service 层，零重复代码
from app.modules.system.service.user_service import user_service
from app.modules.system.service.role_service import role_service

@mcp_tool("user_list", description="获取用户分页列表")
async def user_list(page: int = 1, size: int = 10, keyword: str = None):
    async with get_db_context() as db:
        query = UserQuery(current=page, size=size, user_name=keyword)
        result = await user_service.get_user_list(db, query)
        return result

@mcp_tool("user_create", description="创建新用户")
async def user_create(user_name: str, password: str, roles: list[str]):
    async with get_db_context() as db:
        user_in = UserCreate(user_name=user_name, password=password, roles=roles)
        user = await user_service.create_user(db, user_in)
        await db.commit()
        return {"user_id": str(user.user_id), "user_name": user.user_name}
```

### 运行方式

- **stdio 模式**：Claude Desktop / Cursor 直接启动
- **SSE 模式**：作为 HTTP 服务，前端或其他客户端连接
- 配置方式：在 .env 中设置 `MCP_TRANSPORT=stdio` 或 `MCP_TRANSPORT=sse`

---

## CLI 开发者工具（远期）

```bash
# 安装
pip install hohu-admin-cli

# 创建新模块（脚手架）
hohu create-module crm
# 交互式问答生成完整项目结构

# 本地开发
cd hohu-admin-crm
hohu dev          # 挂载到核心，热重载，打开 Swagger

# 验证
hohu validate     # 检查模块格式、API 兼容性

# 测试
hohu test         # 运行模块测试

# 发布
hohu publish      # 发布到 PyPI + 提交 PR 到 registry
```

---

## 安全考虑

1. **审核机制**：官方模块经过代码审核才能进入 registry
2. **权限控制**：只有超级管理员才能安装/卸载模块
3. **沙箱安装**：pip install 在隔离环境中执行
4. **签名验证**：模块包需要数字签名，防止篡改（远期）
5. **审计日志**：记录所有模块安装/卸载/更新操作
6. **依赖安全扫描**：安装前扫描模块依赖的已知漏洞（远期）

---

## 落地路线图

### Phase 0：Module API 设计
- 定义 ModuleDefinition 协议
- 确定模块能力边界
- 设计模块生命周期钩子
- 产出：API 设计文档

### Phase 1：核心框架重构
- 现有代码拆分为 core（框架）+ system（内置模块）
- 实现 ModuleDefinition 和模块加载器
- 添加 INSTALLED_MODULES 配置
- 重构 alembic/env.py 支持动态模型导入
- 添加 TimestampMixin 共享基类
- 产出：可加载/卸载模块的核心框架

### Phase 2：应用商店后端
- 模块注册中心（GitHub 仓库起步）
- 安装/卸载/更新 API
- InstalledModule 数据模型
- 模块安装流程实现
- 产出：可通过 API 安装模块的后端

### Phase 3：CLI 开发工具
- 脚手架（hohu create-module）
- 本地开发（hohu dev）
- 验证和发布（hohu validate, hohu publish）
- 产出：完整的开发者工具链

### Phase 4：应用商店前端
- Vue 3 应用商店页面
- 模块卡片、搜索、分类
- 安装/卸载/更新 UI
- 模块设置页面
- 产出：管理后台中的应用商店

### Phase 5：MCP 集成
- 作为第一个"官方可选模块"
- 暴露系统管理工具（用户、角色、菜单、字典 CRUD）
- 支持 stdio 和 SSE 传输
- 后续可扩展 CRM/ERP 工具
- 产出：可用的 MCP Server 模块

---

## 前端模块分发策略（双轨制）

### 核心思路

后端 Python 包**自带编译好的前端产物**（dist/），同时发布独立的**npm 源码包**供开发者二次开发。

```
┌───────────────────────────────────────────────────────────┐
│                    模块包结构                               │
│                                                           │
│  hohu-admin-crm/                  hohu-admin-crm-ui/      │
│  (Python 包，PyPI 发布)           (Vue 源码包，npm 发布)   │
│  ├── pyproject.toml               ├── package.json         │
│  ├── hohu_admin_crm/              ├── src/                 │
│  │   ├── __init__.py (ModuleDefinition)│   ├── views/      │
│  │   ├── api/                     │   ├── api/            │
│  │   ├── models/                  │   ├── stores/         │
│  │   ├── schemas/                 │   └── index.ts        │
│  │   └── service/                 ├── dist/  ← 编译产物    │
│  └── dist/  ← 编译好的前端 JS/CSS  └── vite.config.ts     │
│                                                           │
│  pip install 时包含 dist/         npm install 获得源码      │
└───────────────────────────────────────────────────────────┘
```

### 普通用户：一键安装（自动获得前端）

```bash
# 在管理后台点击"安装 CRM" 或命令行：
pip install hohu-admin-crm
```

安装后：
1. 后端自动挂载 `/static/modules/crm/` 提供前端 JS/CSS
2. 菜单数据写入 `sys_menu`（包含 component 路径）
3. 前端 app 从菜单 API 获取路由配置，动态加载模块组件

### 开发者/二次开发：npm 源码包

```bash
# 在前端项目中安装源码
npm install hohu-admin-crm-ui
# 或 pnpm add hohu-admin-crm-ui
```

直接 import 源码组件，自由修改和扩展：

```typescript
// 自定义 CRM 页面
import { CustomerList } from 'hohu-admin-crm-ui'
import { useCustomerStore } from 'hohu-admin-crm-ui/stores'
```

### 后端静态文件挂载

```python
# app/core/module_loader.py
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import importlib

def mount_module_static(app: FastAPI, module_name: str):
    """挂载模块的前端静态文件"""
    module_pkg = importlib.import_module(f"hohu_admin_{module_name}")
    dist_path = Path(module_pkg.__file__).parent / "dist"
    if dist_path.exists():
        app.mount(
            f"/static/modules/{module_name}",
            StaticFiles(directory=str(dist_path)),
            name=f"module_static_{module_name}"
        )
```

### 前端动态路由加载

```typescript
// hohu-web/src/core/moduleManager.ts

interface ModuleMenu {
  name: string
  path: string
  component: string           // "module:crm/customer/index" 或 "system/user/index"
  permission?: string
  children?: ModuleMenu[]
}

async function loadModuleRoutes() {
  // 从后端获取已安装模块的菜单配置
  const { data: menus } = await api.get('/system/menu/routes')

  menus.forEach(menu => {
    if (menu.component?.startsWith('module:')) {
      // 模块组件 → 从后端静态文件动态加载
      const [, mod, comp] = menu.component.match(/^module:(.+?)\/(.+)$/) || []
      router.addRoute('Layout', {
        path: menu.path,
        component: () => import(
          /* @vite-ignore */
          `/static/modules/${mod}/${comp}.js`
        ),
      })
    } else {
      // 内置组件 → 正常 import
      router.addRoute('Layout', {
        path: menu.path,
        component: () => import(`@/views/${menu.component}.vue`),
      })
    }
  })
}
```

### 部署架构（模块前端由后端托管）

用户在应用商店一键安装模块时，前端文件**不需要拷贝到任何地方**——后端直接从 pip 安装包中读取并服务。

#### 安装后的文件位置

```
pip install hohu-admin-crm 后：

/python/site-packages/
  hohu_admin_crm/
    __init__.py              # ModuleDefinition 声明
    api/
    models/
    schemas/
    service/
    dist/                    ← 前端编译产物就在这里
      customer/
        index.js
        index.css
      deal/
        index.js
      index.js               # 入口
```

#### 服务启动时自动挂载

```python
# app/core/module_loader.py — 服务启动时遍历已安装模块
def load_all_modules(app: FastAPI, installed_modules: list[str]):
    for module_name in installed_modules:
        # 1. 导入模块，注册 API 路由
        mod = importlib.import_module(f"hohu_admin_{module_name}")
        module_def = mod.module
        for router, prefix, tag in module_def.routers:
            app.include_router(router, prefix=prefix, tags=[tag])

        # 2. 挂载前端静态文件（直接从 site-packages 读取）
        dist_path = Path(mod.__file__).parent / "dist"
        if dist_path.exists():
            app.mount(
                f"/static/modules/{module_name}",
                StaticFiles(directory=str(dist_path)),
                name=f"module_{module_name}"
            )
```

#### 前端加载链路

```
1. 用户登录 → 前端调用 GET /auth/getUserRoutes
2. 后端返回菜单数据，包含 component: "module:crm/customer/index"
3. 前端路由守卫检测到 "module:" 前缀
4. 动态 import("/static/modules/crm/customer/index.js")
5. 请求到达 FastAPI → StaticFiles 中间件返回 JS 文件
6. 浏览器执行 JS，渲染模块页面
```

#### nginx 生产部署配置

```nginx
server {
    listen 80;
    server_name admin.example.com;

    # 主前端（Vue 3 编译产物）
    location / {
        root /var/www/hohu-web;
        try_files $uri $uri/ /index.html;
    }

    # 模块前端 → 转发给 FastAPI（由 StaticFiles 中间件处理）
    location /static/modules/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_cache_valid 200 7d;          # 静态文件缓存 7 天
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # API 请求
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
    }

    # OpenAPI 文档（开发环境）
    location /docs {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

#### 性能优化（可选，规模大了之后）

初期由 FastAPI 的 StaticFiles 直接服务模块前端文件，简单可靠。如果后续需要优化：

1. **nginx 缓存**：加 `proxy_cache`，静态文件只从后端取一次
2. **CDN**：将 `/static/modules/` 指向 CDN，模块文件全球加速
3. **安装时拷贝**：在安装脚本中将 dist/ 拷贝到 nginx 目录，完全不走后端

这些优化只改 nginx 配置，不需要改代码逻辑。

### 模块作者的开发流程

```
1. 初始化模块项目
   hohu create-module crm

2. 并行开发后端和前端
   hohu-admin-crm/          hohu-admin-crm-ui/
   ├── hohu_admin_crm/      ├── src/
   │   ├── api/             │   ├── views/
   │   ├── models/          │   ├── api/
   │   └── service/         │   └── stores/
   └── pyproject.toml        └── package.json

3. 本地联调
   # 终端 1：后端
   cd hohu-admin-crm && hohu dev
   # 终端 2：前端（Vite 代理到后端）
   cd hohu-admin-crm-ui && pnpm dev

4. 构建发布
   # 前端编译
   cd hohu-admin-crm-ui && pnpm build
   # 拷贝 dist/ 到后端包
   cp -r dist/ ../hohu-admin-crm/hohu_admin_crm/dist/
   # 发布 Python 包（含前端产物）
   cd ../hohu-admin-crm && hohu publish
   # 发布 npm 源码包
   cd ../hohu-admin-crm-ui && npm publish
```

### 前端模块约定

```
hohu-admin-crm-ui/
  src/
    views/                    # 页面组件
      customer/
        index.vue             # 列表页
        detail.vue            # 详情页
        form.vue              # 表单
      deal/
        index.vue
    api/                      # API 调用
      customer.ts
      deal.ts
    stores/                   # Pinia stores
      customer.ts
    components/               # 可复用组件
      CustomerSelect.vue
    types/                    # TypeScript 类型
      index.ts
    index.ts                  # 导出入口
  dist/                       # 编译产物（发布到 Python 包）
    customer/
      index.js
      index.css
    deal/
      index.js
    index.js                  # 入口
```

---

## 关键设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 仓库策略 | 后端单仓库 + 前端独立 | 跨语言 Monorepo 收益有限 |
| 前端模块分发 | 双轨制：PyPI 包含 dist/ + npm 源码包 | 普通用户一键安装，开发者自由定制 |
| 模块分发初期 | 配置化 INSTALLED_MODULES | 简单，一天实现 |
| 模块分发远期 | pip 包 + entry_points | 真正按需安装，支持第三方 |
| 应用商店注册中心 | GitHub 仓库起步 | 零基础设施成本 |
| 模块热加载 | Phase 1 提示重启，远期热加载 | Python 热加载复杂度高 |
| MCP 集成方式 | 后端模块，复用 Service 层 | 零重复代码，最简洁 |
