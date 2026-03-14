# CLAUDE.md (中文版)

本文件为 Claude Code (claude.ai/code) 在此代码库中工作时提供指导。

## 项目概述

**HoHu Admin** 是一个基于 FastAPI 的管理后台，具有构建管理面板的全栈能力。它使用 SQLAlchemy 2.0（支持异步）、PostgreSQL、Redis 和 Alembic 进行数据库迁移。

**核心技术:**
- FastAPI（异步 Web 框架）
- SQLAlchemy 2.0（异步 ORM）
- PostgreSQL（数据库）
- Redis（缓存、会话）
- Alembic（数据库迁移）
- Pydantic（数据验证、自动 snake_case → camelCase 转换）
- Snowflake ID 生成器（分布式唯一 ID）

## 开发命令

### 环境搭建
```bash
# 安装依赖（需要 uv）
uv sync

# 激活虚拟环境
source .venv/bin/activate
```

### 运行应用
```bash
# 开发模式（支持热重载）
fastapi dev app/main.py

# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 数据库操作
```bash
# 创建新的迁移文件
alembic revision --autogenerate -m "描述"

# 应用迁移
alembic upgrade head

# 回滚一个迁移
alembic downgrade -1
```

### Docker
```bash
# 构建并启动服务
docker-compose up -d

# 停止服务
docker-compose down
```

### 代码质量
```bash
# 代码检查和格式化（使用 ruff）
ruff check .
ruff format .
```

### 测试
```bash
# 运行所有测试
pytest

# 运行特定测试文件
pytest tests/test_main.py

# 运行测试并生成覆盖率报告
pytest --cov=app tests/
```

## 架构概览

### 模块结构
应用采用模块化架构，位于 `app/modules/` 下：

```
app/modules/
├── auth/              # 认证与授权
│   ├── api.py        # 认证接口（登录、刷新令牌）
│   ├── service.py    # AuthService、get_current_user()、build_menu_tree()
│   └── schemas/      # 请求/响应模型
├── system/            # 系统管理（User、Role、Menu、Dict）
│   ├── api/          # 各实体的 CRUD 接口
│   ├── crud/         # 业务逻辑层
│   ├── models/       # SQLAlchemy ORM 模型
│   └── schemas/      # 带自动 camelCase 转换的 Pydantic 模式
└── business/          # 自定义业务模块占位符
```

### 核心架构模式

**1. 认证流程:**
- 通过 `/auth/login` 登录 → 生成 JWT 令牌
- 令牌存储在请求头 `Authorization: Bearer <token>`
- `get_current_user()` 依赖验证 JWT 并预加载 `roles.menus` 用于 RBAC
- Redis 黑名单用于退出登录功能（计划中）

**2. RBAC（基于角色的访问控制）:**
- 三层模型：用户 → 角色 → 菜单
- 菜单具有 `permission` 字段（如 "sys:user:list"）
- 使用 `check_permissions("sys:user:list")` 或 `require_permissions()` 装饰器
- 超级管理员绕过所有权限检查

**3. 数据库会话管理:**
- 通过 `get_db()` 依赖提供异步会话
- 成功时自动提交，错误时自动回滚
- 连接池配置了 `pool_pre_ping=True`

**4. ID 生成:**
- 所有主键使用 Snowflake ID，通过 `app.core.id_generator` 的 `next_id()` 生成
- 响应中 ID 自动序列化为字符串，防止 JavaScript BigInt 精度丢失

**5. 响应格式:**
- 所有接口返回 `ResponseModel[T]`，格式为 `{code, msg, data}`
- 使用 `ResponseModel.success(data=...)` 或 `ResponseModel.error(msg=...)`

**6. 命名规范:**
- **后端（Python/SQLAlchemy）**: `snake_case`（如 `user_name`）
- **前端（JSON）**: `camelCase`（如 `userName`）
- **自动转换**: Pydantic 的 `alias_generator=to_camel` 处理转换
- **特殊情况**: 对于 `i18n_key` 等无法正确转换的字段，手动指定 `Field(alias="i18nKey")`

**7. Schema 模式:**
```python
# 请求 schema
class UserCreate(BaseModel):
    user_name: str
    roles: list[str] = []  # 角色代码，不是 ID
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

# 带有 ID 序列化的响应 schema
class UserItemOut(BaseModel):
    user_id: int
    # ... 其他字段

    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)  # 防止 BigInt 精度丢失

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
```

**8. 路由注册:**
- 所有路由在 `app/main.py` 中注册
- 模式: `app.include_router(router, prefix="/module/entity", tags=["Module"])`

## 添加新模块

1. 在 `app/modules/<module_name>/` 下创建目录
2. 在 `models.py` 中定义模型，使用 `app.db.base.Base` 和 `next_id()` 作为主键
3. 在 `schemas.py` 中创建模式，使用 `alias_generator=to_camel`
4. 在 `api.py` 中实现 API 接口，使用 `get_current_user` 进行认证
5. 在 `app/main.py` 中注册路由
6. 创建迁移: `alembic revision --autogenerate -m "add <module>"`

## 重要配置文件

- **`.env`**: 环境变量（DATABASE_URL、SECRET_KEY、Redis 配置）
- **`alembic.ini`**: 数据库迁移配置
- **`alembic/env.py`**: 异步迁移运行器设置
- **`pyproject.toml`**: Ruff 代码检查规则、pytest 配置、项目元数据
- **`requirements.txt`**: Python 依赖

## 常见陷阱

1. **ID 序列化**: 在响应 schema 中始终将 Snowflake ID 序列化为字符串，防止前端 BigInt 精度丢失
2. **密码更新**: 永远不要在响应中暴露 `hashed_password`；只在创建/更新操作中接受明文 `password`
3. **角色分配**: 用户通过 `role_code`（字符串）接收角色，而不是 `role_id`（整数）
4. **数据库提交**: `get_db()` 依赖会自动处理提交/回滚
5. **异步查询**: 始终对异步 SQLAlchemy 操作使用 `await`，并使用 `selectinload` 进行关系预加载
