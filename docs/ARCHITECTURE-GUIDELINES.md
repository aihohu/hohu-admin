# 架构规范（ARCHITECTURE-GUIDELINES）

> **主视角**：项目架构师
> **受众**：全员（开发 / 产品 / 测试 / 运维）
> **目的**：固化跨子项目的架构边界、分层、契约、演进策略，让每个新功能都能在既定骨架上生长，而不是临时发挥。
>
> **注意**：本文件是**架构规范**（governance），不是「应用市场架构设计」。后者见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)（插件化生态愿景）。

---

## 1. Monorepo 拓扑与依赖方向

### 1.1 子项目矩阵

| 子项目 | 语言 / 框架 | 角色 |
|---|---|---|
| `hohu-admin` | Python / FastAPI / SQLAlchemy 2.0 async | 后端 + 应用商店 + 低代码引擎 |
| `hohu-admin-web` | Vue 3 / NaiveUI / UnoCSS / Pinia | 管理后台前端 |
| `hohu-admin-app` | uni-app / Vue 3 / wot-design-uni | 跨平台移动端（H5/微信小程序/App） |
| `hohu-admin-docs` | VitePress | 文档站点 |
| `hohu-admin-desktop` | Electron / Forge / Vite / TypeScript | 桌面应用 |
| `hohu-cli` | Python / Typer | 脚手架与多项目启动器 |

### 1.2 依赖方向（**禁止逆向**）

```
hohu-cli ──管理──> hohu-admin / hohu-admin-web / hohu-admin-app
hohu-admin-web ──HTTP──> hohu-admin (后端 API)
hohu-admin-app ──HTTP──> hohu-admin (后端 API)
hohu-admin-desktop ──内嵌──> hohu-admin-web (前端产物)
hohu-admin-docs ──引用代码示例──> 各子项目（只读）

禁止:
- hohu-admin ──依赖──> hohu-admin-web 的代码（只能通过 OpenAPI 契约）
- 任何子项目 ──依赖──> hohu-cli 的代码
- hohu-admin-docs ──运行时依赖──> 任何子项目
```

### 1.3 子项目独立仓库

每个子项目是**独立的 git 仓库**（在 `hohu/` 下作为子目录存在，但 `.git` 独立）。理由：
- 各子项目有自己的发布节奏（前端 weekly、后端 hotfix）
- Python / TypeScript / Dart 生态差异大，统一 monorepo 收益有限
- 对外部贡献者更友好（只 clone 关心的部分）

CLI（`hohu-cli`）通过 `hohu admin create / init / dev` 跨子项目协作，组件配置统一在 [`hohu-cli/hohu/config/components.py`](../hohu-cli/hohu/config/components.py)。

---

## 2. 后端分层架构（API → Service → Model）

### 2.1 分层与职责

| 层 | 路径 | 职责 | 铁律 |
|---|---|---|---|
| **API** | `app/modules/<m>/api/` | HTTP 入参/出参、调用 Service、提交事务 | 必须 `await db.commit()` |
| **Service** | `app/modules/<m>/service/` | 业务逻辑、领域异常、DB 查询 | **绝不自行 commit** |
| **Model** | `app/modules/<m>/models/` | SQLAlchemy 2.0 `Mapped[T]` 实体 | PK 必须 `default=next_id` |
| **Schema** | `app/modules/<m>/schemas/` | Pydantic v2 DTO | `alias_generator=to_camel` + BigInteger `@field_serializer` |

### 2.2 模块级单例

```python
# 模块底部
user_service = UserService()
```

禁止在每个请求里 `UserService()` —— 共享缓存 / Redis 连接池 / 配置开销会爆炸。

### 2.3 `get_current_user` 在哪

**在 `app/modules/auth/service.py`，不在 `app/core/auth.py`**。这是历史决定（`core/` 是无状态工具，`auth/service.py` 才有 DB 依赖）。新代码引用时 import 路径别搞错。

### 2.4 异常层级

```
BusinessException (base, has error_code for i18n)
├── NotFoundException(resource_type)
├── DuplicateException(field, value)
├── AuthenticationException (401)
├── AuthorizationException (403)
└── BusinessRuleException(message)
    └── InvalidParameterException(message)
```

**铁律**：业务代码**禁用** `HTTPException`。必须用上面层级里的类。新异常只在「需要独特逻辑」时新建，否则复用 `NotFoundException("资源名")` 这类通用类。

**error_code 必填**（UPPER_SNAKE_CASE，如 `MARKETPLACE_APP_NOT_FOUND`），前端用 `$t('errorCode.XXX')` 映射。

详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md) §异常处理。

---

## 3. 前端分层架构（View → API → Store）

### 3.1 目录约定

```
hohu-admin-web/src/
├── views/<module>/index.vue        # 页面（@elegant-router 自动生成路由）
├── service/api/<module>.ts         # API 请求封装（带类型）
├── typings/api/<module>.d.ts       # 类型定义（declare namespace Api.Module）
└── store/modules/<module>.ts       # Pinia store
```

### 3.2 路由生成

`@elegant-router` 监听 `src/views/` 目录变化自动生成路由（dev server 运行时）。**新增页面不需要手动 `pnpm gen-route`**。

### 3.3 类型契约

后端 OpenAPI（`/docs`）是单一事实源。前端类型放 `typings/api/`，命名空间：

```typescript
declare namespace Api {
  namespace Marketplace {
    interface App { ... }
    namespace Review { ... }
  }
}
```

---

## 4. 跨项目契约（不可破）

### 4.1 响应格式

```json
{ "code": 200, "msg": "success", "data": <T> }
```

- 成功：`code: 200`
- 业务错误：`code: 4xx/5xx` + `msg` + 可选 `errorCode`
- 分页：`data: { records: T[], total: number, current: number, size: number }`

### 4.2 认证

- `Authorization: Bearer <jwt>`（HS256，7 天有效期）
- super_admin bypass：`user_name == "admin"` 或 role 含 `R_SUPER`
- 权限码：`<module>:<resource>:<action>`（如 `sys:user:list`）

### 4.3 ID 与序列化

- 主键：**Snowflake ID**（`default=next_id`），64 位整数
- JSON 序列化：**字符串**（防 JS BigInt 精度丢失）
- 实现：Schema 加 `@field_serializer("id", lambda v: str(v))`

### 4.4 命名转换

- 后端：`snake_case`（Python / SQL）
- 前端：`camelCase`（JS / TS）
- 自动转换链路：
  - 后端 → 前端：Pydantic `alias_generator=to_camel`
  - 前端 → 后端：Pydantic `populate_by_name=True` + 前端 axios 拦截器

### 4.5 时间格式

- DB 列：`TIMESTAMP WITH TIME ZONE`（timestamptz）
- 应用内：全 UTC
- API 响应：ISO 8601 + `Z`（如 `2026-05-19T08:00:00Z`）
- API 入参：接受任意时区，Pydantic 自动解析
- 前端：dayjs 按用户本地时区渲染

详见 [`docs/plans/datetime-timestamptz-migration.md`](./plans/datetime-timestamptz-migration.md)。

---

## 5. 领域建模（DDD 轻量版）

### 5.1 通用语言词汇表

新功能 spec 必须先对齐术语，避免后续混淆。标杆见 [`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md) §0 术语表。

| 术语 | 含义 |
|---|---|
| Tenant | 租户（多租户隔离的最小单位；Phase 1 全部 `tenant_id=0`） |
| App | 应用市场的顶层分发单元（slug + version 唯一） |
| Component | App 内部技术组成（backend / frontend / lowcode） |
| Manifest | App 的 `app.json` 描述文件 |
| Scope | 数据权限作用域（all / dept / self） |

新术语进入词汇表前需 spec 评审。

### 5.2 聚合根边界

每个 module 有清晰的聚合根：

| 模块 | 聚合根 |
|---|---|
| `system` | User / Role / Menu / DictType / DictData |
| `auth` | Token（无表）/ Session |
| `marketplace` | App / AppVersion / TenantApp / Review |
| `lowcode` | （借用 marketplace 的 App，无独立聚合根） |

**禁止**跨模块直接查表。例如 marketplace 要查 user，必须 `from app.modules.system.service import user_service`，不能 `from app.modules.system.models import User; SELECT User`。

### 5.3 限界上下文

```
auth ──提供 currentUser──> 所有模块
system ──提供 user/role/menu──> 所有模块
marketplace ──提供 app/version/install──> lowcode / web
lowcode ──提供 data_schema 运行时──> marketplace
```

模块依赖**单向**，禁止环。

---

## 6. 演进与兼容

### 6.1 数据库 schema 演进

| 场景 | 用什么 |
|---|---|
| 全新表 | `MigrationRunner.create_table`（CREATE TABLE IF NOT EXISTS） |
| 已有表加字段 / 改类型 | `MigrationRunner.apply_upgrade`（introspect + compare + ALTER） |
| 系统级 schema 变更（sys_user 加列） | Alembic migration（`alembic revision --autogenerate`） |
| 应用数据表（app_data_*）演化 | `apply_upgrade`（**禁用** `CREATE TABLE IF NOT EXISTS` 当演化用） |

**关键决策**：重装走 `apply_upgrade` 而非 `create_table`，详见 [`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md) §决策 #74。

### 6.2 API 版本策略

- **Phase 1-2**：无版本前缀，breaking change 通过 deprecation 周期管理
- **Phase 3+**：引入 `/v1/` `/v2/` URL 前缀（届时写 ADR）

### 6.3 Breaking change 流程

1. spec 标记 `⚠️ Breaking` 块，写迁移路径
2. 后端实现新接口 + 保留旧接口（deprecated 标注）
3. 前端切到新接口
4. 旧接口保留 ≥ 1 个 minor 版本，期间日志 warn
5. 删除旧接口前发 release notes 显著位置公告

---

## 7. ADR（架构决策记录）

跨项目 / 长期影响的架构选型走 ADR，详见 [`adr/README.md`](./adr/README.md)。

| 触发条件 | 写哪里 |
|---|---|
| 选 Snowflake ID 而非 UUID | ADR |
| 重装走 apply_upgrade 而非 create_table | spec 决策 #N |
| `created_at` 改为 timestamptz | ADR |
| 给某字段加索引 | 都不用写 |
| 应用市场采用 manifest 驱动 | ADR |

---

## 8. 技术债管理

### 8.1 标记

```python
# TODO(debt): 这里直接 f-string 拼 SQL，应该走参数化。
#   触发条件：marketplace Phase 2 接入第三方应用时
#   责任人：@zhangsan
```

`TODO(debt)` 是「**已知且暂时接受**」的技术债。普通 `TODO` 是「待办」，两者不要混。

### 8.2 偿还时机

- **机会性偿还**：每次接触该模块时，顺手修一个 `TODO(debt)`
- **集中偿还**：每个 Phase 结束前，留 20% 时间清旧债
- **不允许**「冻结式技术债」—— 超过 6 个月未动且未规划偿还的 `TODO(debt)` 必须升级为 issue

### 8.3 不允许的债

- 跳过 migration 直接 DDL
- Service 层 commit
- 跨模块直接查表
- raw SQL 用 f-string 拼（必须参数化）

---

## 9. 可扩展性预留

### 9.1 多租户 → 云市场分拆

Phase 2 的 `catalog` 与 `execution` 拆分预留，详见 [`MARKETPLACE-CLOUD-SPLIT.md`](./MARKETPLACE-CLOUD-SPLIT.md)。同一份代码按 `HOHU_MODE=cloud|local|hybrid` 启用不同 router 与 alembic 迁移。

### 9.2 Phase 1-3 预留接口

| Phase | 预留 |
|---|---|
| Phase 1 | manifest 驱动建表 / uninstall DROP / retained_table_names |
| Phase 2 | event bus + outbox（`mk_outbox` 表已在 spec 中定义） |
| Phase 3 | App SDK CLI / 容器化 backend 组件 |

新增功能时，若触及这些预留，必须先读对应 spec 章节对齐方向。

---

## 10. 反模式（Don't）

| 反模式 | 正解 |
|---|---|
| Service 自行 `await db.commit()` | 让 API 层 commit |
| 业务代码用 `HTTPException` | 用领域异常层级 |
| 跨模块直接 `SELECT User` | import service |
| 用 `CREATE TABLE IF NOT EXISTS` 处理演化 | 走 `apply_upgrade` |
| Snowflake ID 在 JSON 里是 number | 序列化为 string |
| 时间用 naive datetime | 全 UTC + timestamptz |
| raw SQL 用 f-string | 参数化（`text("... :name")` + `params`） |
| 给所有字段加索引 | 只给查询频繁 / 唯一约束字段加 |
| Service 不写单例直接 `new` | 模块底部加 `xxx_service = XxxService()` |
| 测试不隔离污染 dev DB | 用 `db_session` fixture 事务回滚 |
| 跨项目共享可变状态 | 走 API 契约 + 事件总线（Phase 2+） |

---

## 11. 参考

- 标杆 spec：[`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)
- 开发规范：[`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md)
- 测试规范：[`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md)
- ADR 索引：[`adr/README.md`](./adr/README.md)
- 子项目 CLAUDE.md：[`hohu-admin/CLAUDE.md`](../CLAUDE.md) / [`hohu-admin-web/CLAUDE.md`](../../hohu-admin-web/CLAUDE.md) 等
