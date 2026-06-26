# 应用市场架构演进路线（Phase 2：云市场拆分）

> **状态**：设计文档，待实施
> **创建**：2026-06-25
> **背景**：当前 Phase 1 是单体架构（catalog + execution 同库同进程），需演进为 VS Code 式「云市场 + 本地执行」模型

---

## 1. 目标模型

参考 VS Code Marketplace / Chrome Web Store：

- **云市场**（`marketplace.hohu.dev`）：目录展示、开发者上传、人工审核、zip 包分发
- **本地 HoHu**（用户/公司自部署）：从云拉应用 → 安装 → 运行业务数据
- **核心约束**：云市场**不接触用户业务数据**；本地 HoHu **不存储云市场目录**

## 2. 当前架构 vs 目标架构

### 当前（Phase 1，单体）

```
[HoHu 进程]  ← 单一 FastAPI app
   ├─ catalog 表（mk_app, mk_app_version, ...）
   ├─ execution 表（mk_tenant_app, app_data_*）
   ├─ developer upload
   ├─ admin review
   └─ install/enable/data CRUD
            
[单一 DB]  ← 所有表混在一起
```

### 目标（Phase 2，拆分）

```
[云市场进程]                  [本地 HoHu 进程 A]      [本地 HoHu 进程 B]
HOHU_MODE=cloud              HOHU_MODE=local         HOHU_MODE=local
   ├─ catalog routers           ├─ install               ├─ install
   ├─ developer upload          ├─ enable/disable        ├─ enable/disable
   ├─ admin review              ├─ app_data CRUD         ├─ app_data CRUD
   └─ zip download              └─ cloud_sync ↑            └─ cloud_sync ↑
        ↑                            │                        │
        └────────────────────────────┴────────────────────────┘
                  (HTTP: catalog browse + zip download)

[云市场 DB]                  [本地 DB A]              [本地 DB B]
catalog 表                  execution 表            execution 表
```

## 3. 表归属

### 仅云市场 DB（CLOUD-ONLY）

| 表 | 用途 |
|---|---|
| `mk_app` | 应用目录 |
| `mk_app_version` | 版本目录 |
| `mk_app_review` | 审核队列 |
| `mk_app_permission` | 应用声明的权限 |
| `mk_rating` | 公开评分（Phase 2 新增聚合）|
| `mk_developer` | 开发者账号（Phase 2 新增）|

### 仅本地 DB（LOCAL-ONLY）

| 表 | 用途 |
|---|---|
| `mk_tenant_app` | 本机安装记录 |
| `app_data_*` | 动态建的业务数据表（lowcode 应用产生）|
| `mk_local_app` | 本地开发者上传的应用（VSIX 等价物，Phase 2 新增）|
| `mk_local_app_version` | 本地应用版本 |
| `mk_cloud_app_cache` | 云市场目录缓存（可选，加速离线浏览）|

### 共享代码定义（不共享 DB）

同一份 Python class 定义在 repo 里，但**部署时按角色迁移**：

```python
# 代码库里
app/modules/marketplace/models/cloud/app.py         # class App
app/modules/marketplace/models/cloud/version.py     # class AppVersion
app/modules/marketplace/models/local/install.py     # class TenantApp
app/modules/marketplace/models/local/local_app.py   # class LocalApp

# 部署时
alembic/cloud/env.py    # 只 import cloud/* models
alembic/local/env.py    # 只 import local/* models
```

## 4. Router 归属

### Cloud-only routers

| 路由 | 用途 |
|---|---|
| `GET /marketplace/apps` | 浏览目录（公开）|
| `GET /marketplace/apps/{slug}` | 应用详情 |
| `POST /marketplace/developer/upload` | 开发者上传新版本 |
| `GET /marketplace/admin/reviews` | 审核队列列表 |
| `POST /marketplace/admin/reviews/{id}/approve` | 通过 |
| `POST /marketplace/admin/reviews/{id}/reject` | 拒绝 |
| `GET /marketplace/download/{slug}/{version}` | 下载 zip 包（API token 鉴权）|
| `POST /marketplace/rating` | 评分（注册用户）|

### Local-only routers

| 路由 | 用途 |
|---|---|
| `POST /marketplace/install` | 从云拉应用并安装到本地 |
| `POST /marketplace/enable/{slug}` | 启用已安装应用 |
| `POST /marketplace/disable/{slug}` | 禁用 |
| `POST /marketplace/uninstall/{slug}` | 卸载（含 DROP TABLE）|
| `GET /marketplace/installed` | 本机安装列表 |
| `GET /api/v1/app-data/{slug}/{model}` | 动态数据 CRUD |
| `GET /marketplace/contributes` | 聚合菜单/页面（启动时加载）|
| `POST /marketplace/local/upload` | 本地开发者通道（不经审核）|

### 本地的云代理（可选）

| 路由 | 用途 |
|---|---|
| `GET /cloud-proxy/apps` | 代理到云市场（绕 CORS 或加缓存）|

## 5. 部署配置：`HOHU_MODE`

```bash
# .env
HOHU_MODE=cloud    # 仅 cloud routers + cloud 表（部署 marketplace.hohu.dev）
HOHU_MODE=local    # 仅 local routers + local 表（普通用户/公司自部署）
HOHU_MODE=hybrid   # 两者都启用（Phase 1 兼容、开发自部署、单机内部用）
```

```python
# app/main.py
if settings.HOHU_MODE in ("cloud", "hybrid"):
    app.include_router(cloud_browse_router)
    app.include_router(developer_router)
    app.include_router(admin_review_router)
    app.include_router(download_router)

if settings.HOHU_MODE in ("local", "hybrid"):
    app.include_router(install_router)
    app.include_router(enable_router)  # enable/disable/uninstall
    app.include_router(app_data_router)
    app.include_router(contributes_router)
    app.include_router(local_upload_router)
```

`hybrid` 模式 = 当前的 Phase 1 行为，向后兼容。

## 6. 关键数据流

### 浏览（用户在本地 HoHu 前端看云市场应用）

```
[Browser]
   ↓ GET https://marketplace.hohu.dev/marketplace/apps
[云市场]
   ↓ SELECT FROM mk_app WHERE status='published'
[Browser 收到 catalog JSON]
```

本地前端**直连云市场 API**（云配 CORS）。本地 HoHu 后端不参与浏览。

### 安装

```
[Browser]
   ↓ POST https://local.hohu.user/marketplace/install
[本地后端]
   ├─ 1. GET 云 /marketplace/apps/{slug}/manifest
   ├─ 2. GET 云 /marketplace/download/{slug}/{version}  → zip bytes
   ├─ 3. 校验签名（Phase 2，使用云公钥）
   ├─ 4. INSERT mk_tenant_app (status=installed)
   ├─ 5. MigrationRunner.create_table() → 建 app_data_<slug> 表
   └─ 6. contributes_service.invalidate()
```

### 上传（云市场开发者）

```
[Developer Browser]
   ↓ POST 云 /marketplace/developer/upload (file + manifest)
[云市场后端]
   ├─ 1. validate_manifest()
   ├─ 2. compute_sha256, save zip
   ├─ 3. INSERT mk_app / mk_app_version (review_status=pending)
   ├─ 4. INSERT mk_app_review (status=pending)
   └─ 5. notify reviewers (邮件/webhook)
```

### 本地开发（不走云市场，类似 VS Code 的 VSIX）

```
[Local Dev]
   ↓ POST 本地 /marketplace/local/upload (file + manifest)
[本地后端]
   ├─ 1. validate_manifest()
   ├─ 2. save zip 到本地 uploads/
   ├─ 3. INSERT mk_local_app / mk_local_app_version (status=enabled, 不需审核)
   ├─ 4. INSERT mk_tenant_app (status=installed)
   └─ 5. MigrationRunner.create_table()
```

## 7. Phase 1 期间的临时约定：代码标记

物理拆分推迟到真要部署云市场时。在此之前，**用 docstring 标记**每个文件归属：

```python
# app/modules/marketplace/models/app.py
"""[CLOUD-ONLY] 应用目录表

部署在云市场 DB。本地 HoHu 不创建此表。
Phase 2 拆分时迁移到 app/modules/marketplace/models/cloud/app.py
"""
class App(Base):
    ...
```

```python
# app/modules/marketplace/models/install.py
"""[LOCAL-ONLY] 本地安装记录

部署在本地 DB。云市场不知道用户装了什么。
Phase 2 拆分时迁移到 app/modules/marketplace/models/local/install.py
"""
class TenantApp(Base):
    ...
```

未来物理拆分时：

```bash
grep -rn "\[CLOUD-ONLY\]" app/  # 列出所有云市场文件
grep -rn "\[LOCAL-ONLY\]" app/  # 列出所有本地文件
```

## 8. 物理拆分的具体步骤（Phase 2 启动时）

### Step 1：标记（**现在做**）

在每个 model/router/service 文件头加 `[CLOUD-ONLY]` / `[LOCAL-ONLY]` / `[SHARED]` docstring。10 分钟工作量。

### Step 2：物理重组（部署云市场前）

```bash
# 移动文件（IDE 重构自动改 import）
mkdir -p app/modules/marketplace/{models,api,service}/{cloud,local}

# models
mv app/modules/marketplace/models/app.py        app/modules/marketplace/models/cloud/
mv app/modules/marketplace/models/version.py    app/modules/marketplace/models/cloud/  # 如果独立文件
mv app/modules/marketplace/models/review.py     app/modules/marketplace/models/cloud/
mv app/modules/marketplace/models/permission.py app/modules/marketplace/models/cloud/
mv app/modules/marketplace/models/rating.py     app/modules/marketplace/models/cloud/
mv app/modules/marketplace/models/install.py    app/modules/marketplace/models/local/

# api
mv app/modules/marketplace/api/marketplace.py   app/modules/marketplace/api/cloud/  # browse 部分
mv app/modules/marketplace/api/developer.py     app/modules/marketplace/api/cloud/
mv app/modules/marketplace/api/admin.py         app/modules/marketplace/api/cloud/
mv app/modules/marketplace/api/app_data.py      app/modules/marketplace/api/local/
mv app/modules/marketplace/api/contributes.py   app/modules/marketplace/api/local/
```

### Step 3：Alembic 分轨

```python
# alembic/cloud/env.py
target_metadata.registry = [
    App, AppVersion, AppReview, AppPermission, Rating, Developer
]

# alembic/local/env.py  
target_metadata.registry = [
    TenantApp, LocalApp, LocalAppVersion
]
```

历史迁移脚本按修改的表分到对应目录。新建迁移时按 mode 生成。

### Step 4：HOHU_MODE 配置

- 添加 `HOHU_MODE` 到 `app/core/config.py`
- `app/main.py` 按 mode 注册 router（见第 5 节）

### Step 5：cloud_sync service

新增 `app/modules/marketplace/service/cloud_sync.py`：

```python
class CloudSyncService:
    def __init__(self, cloud_url: str, api_token: str | None = None):
        self.cloud_url = cloud_url
    
    async def fetch_catalog(self, *, category=None, page=1) -> dict: ...
    async def fetch_manifest(self, slug: str, version: str | None = None) -> dict: ...
    async def download_zip(self, slug: str, version: str) -> bytes: ...
    async def verify_signature(self, zip_bytes: bytes, signature: str) -> bool: ...
```

`install_service` 改造：从本地查 `mk_app_version` 改为调用 `cloud_sync.fetch_manifest()`。

### Step 6：部署云市场实例

```bash
# 在一台公网服务器
export HOHU_MODE=cloud
export DATABASE_URL=postgresql://.../hohu_marketplace
alembic upgrade head  # 只跑 cloud 迁移
fastapi run

# 配置 DNS
marketplace.hohu.dev → 这台服务器
```

### Step 7：本地 HoHu 接入

```bash
# 在用户/公司自部署的 HoHu
export HOHU_MODE=local
export HOHU_CLOUD_URL=https://marketplace.hohu.dev
export DATABASE_URL=postgresql://.../hohu_local
alembic upgrade head  # 只跑 local 迁移
fastapi run
```

## 9. 暂不做的事（等用户量起来再说）

| 功能 | 推迟理由 |
|---|---|
| 应用包签名（防供应链）| 当前用户量小，攻击面有限；先靠审核把关 |
| 开发者组织账号 | 现在用户量小，admin/developer 二级够用 |
| 计费（付费应用）| 开源项目优先建生态，不是商业化 |
| SBOM / CVE 扫描 | 等审核团队成立再加 |
| 跨租户统计（热门应用）| 没有真实多租户场景前没意义 |
| 镜像市场（企业私有 marketplace）| Phase 3+ |
| 应用版本回滚 | 等真出过线上事故再加 |
| 增量更新（差量 zip）| 流量小，没必要 |

## 10. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| catalog 与 execution 是否同库 | **否** | 数据隔离；可独立扩展；安全边界清晰 |
| 共用代码库还是拆两份 repo | **共用** | 维护成本低；按 mode 启不同模块即可 |
| 物理拆分时机 | **推迟** | 现在不知道云市场真实需求；等部署一台再拆最准 |
| 本地是否需要审核 | **否** | 本地 dev upload 直接信任；云上才需要审核 |
| 浏览时本地是否代理 | **否**（默认）| 前端直连云市场最简单；可选 cloud-proxy 用于离线 |
| 是否引入 organization 概念 | **推迟** | 当前用户量小，单人开发者为主 |
| Phase 1 是否做 hybrid 模式 | **是** | 向后兼容，老部署不破坏 |

## 11. 风险与已知问题

| 风险 | 缓解 |
|---|---|
| Phase 1 写的代码可能 import 跨边界 | 加 `[CLOUD-ONLY]` 标记；Code review 时注意 |
| 多个 model 文件共享 `Base` 元类 | 拆分时小心 import 顺序 |
| alembic 历史迁移已 mix | 拆分时按"修改了哪些表"切分历史；新部署直接跑对应 mode 的迁移 |
| 本地缓存云市场数据可能不一致 | 缓存 TTL 短（5 分钟）；点"安装"前强制刷新 manifest |

---

**维护者注释**：本文档随 Phase 2 实施进度更新。每完成一步，把对应章节标记 ✅ 并附 PR 链接。
