# 后端架构与契约

hohu-admin 是 FastAPI 后端，使用 PostgreSQL、SQLAlchemy 2.0 async、Pydantic v2、Redis 和 Alembic。Web、App、Desktop、文档站和 hohu-cli 为独立项目；本文只描述本仓库的边界。

## 代码入口与分层

| 位置 | 职责 |
| --- | --- |
| [app/main.py](../app/main.py) | 路由注册、中间件、应用生命周期 |
| [app/modules](../app/modules) | auth、system、platform、ai、job 等模块 |
| [app/core](../app/core) | 配置、安全、租户上下文、领域异常 |
| [app/db](../app/db) | 数据库会话、基础模型与关联表 |
| [scripts](../scripts/README.md) | CLI 调用的部署初始化入口 |
| [tools](../tools) | 静态检查、运维与演示工具 |
| [tests](../tests) | 单元、集成及隔离发布验证 |

业务请求遵循 API → Service → Model。API 层负责请求、权限依赖和事务提交；Service 负责业务逻辑、查询和领域异常，不调用 `commit()`。模块级 Service 单例不保存请求用户、租户或数据库会话。

Schema 使用 Pydantic 校验，Model 使用 SQLAlchemy `Mapped[T]`。遵循既有模块布局；跨模块交互优先通过服务契约，不因目录风格不同而大规模搬迁代码。异常使用 [领域异常](../app/core/exceptions.py)，禁止在业务逻辑中散落原生 HTTPException。

## 对外契约

- 响应为 `{code, msg, data}`，成功码为 200；错误可提供稳定的 `errorCode` 供客户端翻译。
- 认证使用 JWT Bearer；HS256，access 默认 60 分钟、refresh 默认 7 天。平台维护身份有独立认证边界。
- Snowflake ID 在 JSON 中作为字符串传输，避免 JavaScript 精度丢失。
- Python 字段使用 `snake_case`，客户端字段通过 Schema 别名输出 `camelCase`；不要假设任意嵌套业务字典的键都应改写。
- API 时间按现有 Schema 的 UTC/ISO 8601 契约处理；修改数据库时间类型必须提供迁移及回归，不能靠显示偏好改变存储语义。
- 接口细节查看实际运行服务的 OpenAPI；文件存在不表示路由已注册。

## 租户与权限

认证入口生成可信 [TenantContext](../app/core/tenant.py)，业务层显式传递。请求体、任意 Header 或资源 ID 不能代替租户授权。

租户模型查询、计数、更新、删除、关联以及缓存键都必须带租户范围。使用 [tenant_scope](../app/core/tenant_scope.py) 辅助函数；再叠加功能权限、数据范围与 owner 校验。超级管理员权限也有租户边界，见 [多租户](MULTI-TENANCY.md) 和 [数据权限](data-scope-guide.md)。

默认租户 `tenant_id=0` 是真实租户，不表示平台全局。平台全局配置与租户数据分别建模，不得用 `0` 或 `NULL` 隐含全局作用域。系统超级管理员可操作的全局 AI 配置及审计要求见 [AI 安全](AI-SECURITY.md)；普通业务 API 不接受客户端租户参数来切换作用域。

Host、子域名和登录时的租户代码只用于定位候选租户；账号归属、租户状态和凭据验证成功后才能建立可信上下文。认证冻结用户与租户身份，并复验当前状态及安全版本；停用后重新启用不能恢复旧 token。请求头、查询参数、文件或工具参数不得覆盖认证得到的租户。

租户 Service 显式接收上下文，不使用默认 `tenant_id=0` 或模块单例的可变租户状态。直接 ID、列表、count、JOIN、关联加载、写入和删除使用同一 scope；跨租户目标按当前接口的不可见资源语义拒绝，避免暴露其他租户的对象。

租户资源持有非空 `tenant_id`，同租户关联及唯一约束由数据库约束兜底；迁移中的约束不能只靠应用过滤替代。后台任务、缓存、锁、幂等键、文件路径、消息及结果投影继承可信租户，缺少上下文时不得回退为默认租户。当前不提供跨租户 membership、在线租户切换、BYOK 或 PostgreSQL RLS 隔离。

## 演进

数据结构由 Alembic 维护，种子只补齐数据；发布后的迁移不可重写。跨客户端或 CLI 的契约变更需配套交付并说明升级顺序。当前应用市场未注册到主应用，不承诺云市场或任意 Python 插件热加载，见 [能力状态](APP-MARKETPLACE.md)。

架构背景和备选方案默认内部维护，公开手册说明当前契约、兼容性和限制，保存规则见 [文档维护](DOCUMENTATION.md)。新增模块见 [开发指南](MODULE-DEVELOPMENT-GUIDE.md)，部署与升级见 [迁移指南](DATABASE-MIGRATIONS.md)。
