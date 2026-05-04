# CLAUDE.zh.md

## 项目概述

FastAPI 管理后台后端，异步 SQLAlchemy 2.0 + PostgreSQL + Redis + Alembic。

**技术栈：** FastAPI / SQLAlchemy 2.0（异步）/ PostgreSQL / Redis / Alembic / Pydantic v2 / Snowflake ID / bcrypt / python-jose (JWT)

**环境要求：** Python >= 3.12, uv

## 命令

```bash
uv sync                                    # 安装依赖
fastapi dev app/main.py                    # 开发服务器 (端口 8000, 热重载)
alembic revision --autogenerate -m "描述"  # 创建迁移
alembic upgrade head                       # 执行迁移
ruff check . && ruff format .              # 检查 + 格式化
pytest                                     # 运行测试
python scripts/init_db.py                  # 初始化管理员用户和菜单
```

**写完代码后必须执行 `ruff check . && ruff format .`。**

## 项目结构

```
app/
├── main.py                    # 应用入口、生命周期、路由注册
├── core/
│   ├── auth.py                # require_permissions() 权限依赖
│   ├── base_response.py       # ResponseModel[T]、PageResult[T]
│   ├── config.py              # Pydantic Settings（从 .env 加载）
│   ├── exceptions.py          # 异常层次结构 + 全局处理器
│   ├── id_generator.py        # Snowflake ID (next_id())
│   ├── redis.py               # 异步 Redis 连接池
│   └── security.py            # bcrypt + JWT 创建/验证
├── db/
│   ├── base.py                # DeclarativeBase + 关联表
│   └── session.py             # 异步引擎、get_db() 依赖
├── modules/
│   ├── auth/
│   │   ├── api.py             # 认证接口
│   │   ├── service.py         # AuthService、get_current_user()、build_menu_tree()
│   │   └── schemas/           # LoginCredentials、Token 等
│   └── system/
│       ├── api/               # user.py、role.py、menu.py、dict_type.py、dict_data.py
│       ├── models/            # User、Role、Menu、DictType、DictData
│       ├── schemas/           # 各实体的 Pydantic schema
│       └── service/           # 各实体的 Service 单例
└── utils/
    ├── pagination.py          # paginate()、build_filters()、QueryParams
    └── mask_util.py           # MaskUtil（手机号、邮箱、身份证脱敏）
```

## 架构：API → Service → Model

### API 层
- 处理 HTTP、解析参数、调用 Service、**提交事务**（`await db.commit()`）
- 返回 `ResponseModel.success(data=...)` 或 `ResponseModel.error(msg=...)`

### Service 层
- 业务逻辑、抛出领域异常、执行数据库查询
- **永不提交** — 让 API 层处理
- 模块级单例：`user_service = UserService()`

### Model 层
- SQLAlchemy 2.0 `Mapped[T]`，主键用 `default=next_id`（Snowflake）
- 所有模型从 `app.db.base` 导入 `Base`（包含关联表）

### Schema 层
- Pydantic v2，`alias_generator=to_camel` 自动 snake_case → camelCase
- BigInteger ID 通过 `@field_serializer` 序列化为字符串（防止 JS BigInt 精度丢失）

## 响应格式

```json
{"code": 200, "msg": "success", "data": <T>}
{"code": 200, "msg": "success", "data": {"records": [...], "total": 100, "current": 1, "size": 10}}
{"code": 400, "msg": "错误信息", "data": null}
```

## 认证与授权

- **登录：** `POST /auth/login` → JWT（HS256，7天有效期）
- **令牌：** `Authorization: Bearer <token>`
- **`get_current_user()`：** 在 `app/modules/auth/service.py`（不是 `app/core/auth.py`）
- **RBAC：** 用户 → 角色 → 菜单（三层模型）
- **权限校验：** `require_permissions("sys:user:list")` 或 `require_permissions(super_admin_only=True)` 在 `app/core/auth.py`
- **超级管理员：** `user_name == "admin"` 或角色代码包含 `R_SUPER` → 绕过所有权限检查

## 异常层次结构

```
BusinessException（基类，含 error_code 用于前端 i18n 映射）
├── NotFoundException(resource_type) — 可复用，传入资源名称
├── DuplicateException(field, value) — 可复用
├── AuthenticationException (401)
├── AuthorizationException (403)
└── BusinessRuleException(message) — 通用业务规则异常
    └── InvalidParameterException(message)
```

**规则：**
- **禁止使用 `HTTPException`** — 使用 `app/core/exceptions.py` 中的异常类
- **复用通用异常**（`NotFoundException("资源名")`、`DuplicateException("字段", "值")`），不要为每个实体创建子类
- 只有确实需要特殊逻辑时才建子类（不仅仅是不同的 message）
- **必须设置 `error_code`** 用于前端 i18n 映射 — 使用 `UPPER_SNAKE_CASE`（如 `AI_PROVIDER_NOT_FOUND`），响应自动包含 `errorCode` 字段
- 前端通过 `$t('errorCode.XXX')` 映射；无映射时回退到后端 `msg`

## 分页工具

- `QueryParams(BaseModel)` — 基类，包含 `current`（默认 1）、`size`（默认 10）
- `paginate(db, model, query_params, filters, order_by, eager_loads)` — 通用分页查询
- `build_filters(model, field_mapping, **kwargs)` — 将参数映射为 SQLAlchemy 过滤条件
  - 字符串：精确匹配
  - 元组 `(字段, 操作)`：`"contains"`、`"=="`、`"in_"`、`">="`、`"<="`
  - 可调用对象：自定义过滤逻辑

## 添加新模块

1. 创建目录：`app/modules/example/{api,service,models,schemas}`
2. **Model：** `Mapped[T]` 字段，主键用 `default=next_id`，从 `app.db.base` 导入 `Base`
3. **Schema：** `alias_generator=to_camel`，`@field_serializer` 将 ID 序列化为字符串
4. **Service：** 业务逻辑，永不提交，抛出领域异常，模块级单例
5. **API：** 调用 service，写操作 `await db.commit()`，返回 `ResponseModel`
6. 在 `app/main.py` 注册路由：`app.include_router(router, prefix="/...", tags=[...])`
7. 在 `app/core/exceptions.py` 添加异常子类（仅在必要时）— 优先复用通用异常并传入 `resource_type` 参数
8. 为异常设置 `error_code` 用于前端 i18n：`exc.error_code = "MODULE_RESOURCE_NOT_FOUND"`
9. 执行迁移：`alembic revision --autogenerate -m "..." && alembic upgrade head`

## 常见陷阱

1. **ID 序列化：** 响应 schema 中始终用 `@field_serializer` 将 Snowflake ID 序列化为 `str`
2. **提交分离：** Service 永不提交；API 调用 `await db.commit()`
3. **`get_current_user` 位置：** 在 `app/modules/auth/service.py`，不是 `app/core/auth.py`
4. **Base 导入：** 模型从 `app.db.base` 导入 `Base`（含关联表），不是 `app.db.session`
5. **to_camel 导入：** `from pydantic.alias_generators import to_camel`
6. **禁止 HTTPException：** 使用 `app/core/exceptions.py` 中的领域异常
7. **复用异常：** 直接用 `NotFoundException("资源名")` 而不是为每个实体创建子类
7. **密码处理：** 永不在响应中暴露 `hashed_password`；创建/更新时接受明文 `password`
8. **角色分配：** 用户通过 `role_code`（字符串）接收角色，不是 `role_id`
9. **异步查询：** 始终 `await`；用 `selectinload` 预加载关联关系
10. **i18n_key 字段：** 自动转换不正确时需手动 `Field(alias="i18nKey")`
