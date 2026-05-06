# HoHu Admin

<p align="center">
  <b>AI 驱动的现代化全栈管理平台 · 后端</b>
</p>

<p align="center">
  <a href="https://show.hohu.org">Demo</a> ·
  <a href="https://github.com/aihohu/hohu-admin-web">前端</a> ·
  <a href="https://github.com/aihohu/hohu-admin-app">移动端</a> ·
  <a href="https://hohu.org/guide/introduction.html">文档</a>
</p>

<p align="center">
  <a href="./README.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="license" />
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-3776AB.svg" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-0.127-009688.svg" alt="FastAPI" />
  <img src="https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg" alt="SQLAlchemy" />
  <img src="https://img.shields.io/badge/PostgreSQL-17-4169E1.svg" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/Redis-7-DC382D.svg" alt="Redis" />
  <img src="https://img.shields.io/github/stars/aihohu/hohu-admin" alt="GitHub stars" />
  <img src="https://img.shields.io/github/forks/aihohu/hohu-admin" alt="GitHub forks" />
</p>

---

**HoHu Admin** 是一个基于 **Python** 与 **FastAPI** 构建的现代化、高性能、模块化后台管理系统模板。它采用 SQLAlchemy 2.0（异步） 作为核心 ORM，专为前后端分离架构设计，开箱即用地提供一整套生产级后端基础设施——包括用户认证、基于角色的权限控制（RBAC）、分布式 ID 生成、数据库迁移、日志监控、API 文档集成等完整能力。


在 AI 应用快速落地的时代，**HoHu Admin** 致力于让开发者从重复的底层搭建中解放出来，专注业务创新与智能集成。无论是快速原型验证，还是构建可扩展的企业级应用，HoHu Admin 都能显著降低技术门槛，缩短开发周期，提升代码质量与系统安全性——让开发者更轻松地拥抱 AI 时代。

## ✨ 特性亮点

* **异步高性能**: 基于 Python 类型提示与 FastAPI，全链路异步处理（Async/Await）。
* **分布式唯一 ID**: 主键统一采用 **Snowflake（雪花算法）**，时间有序且高性能，自动解决前端 `BigInt` 精度丢失问题。
* **优雅的鉴权**:
* 同时兼容 **OAuth2 表单 (Swagger UI)** 与 **JSON (SPA 应用)** 登录。
* 内置 **Redis 黑名单** 机制，支持真正的后端“退出登录”。


* **标准 RBAC 模型**: 基于用户-角色-菜单的权限体系，支持按钮级权限校验。
* **统一响应体**: 所有接口遵循 `code`, `message`, `data` 统一封装结构。
* **自动驼峰转换**: 后端遵循 PEP8 (snake_case)，接口自动转换为前端友好的 camelCase。

## 🛠️ 技术栈

- 后端
  - FastAPI
  - SQLAlchemy 2.0
  - PostgreSQL
  - Redis
  - Alembic

- 前端
  - Vue3
  - Vite
  - Naive UI
  - TypeScript
  - UnoCSS

- 移动端
  - Vue3
  - UniAPP

## 📁 目录结构

```text
hohu-admin/
├── app/
│   ├── core/              # 核心框架配置 (Security, JWT, Redis, Config)
│   ├── db/                # 数据库连接与基础 Base 模型
│   │
│   ├── modules/           # 🧩 模块化目录
│   │   ├── auth/          # 认证模块 (登录、Token刷新)
│   │   ├── system/        # 系统管理模块 (User, Role, Menu, Dict)
│   │   │   ├── api/       # 系统接口
│   │   │   ├── crud/      # 系统逻辑
│   │   │   ├── models/    # 系统模型
│   │   │   └── schemas/   # 系统 Schema
│   │   │
│   │   └── business/      # 🚀 二次开发业务占位模块
│   │       ├── __init__.py
│   │       ├── api/       # 用户自己的接口
│   │       └── models/    # 用户自己的模型
│   │
│   └── main.py            # 聚合所有模块的路由
├── scripts/               # 数据初始化脚本
├── alembic/               # 数据库迁移脚本
└── .env                   # 环境变量配置
```

## 🚀 快速开始


### 使用HoHu CLI (推荐)
**[HoHu CLI](https://github.com/aihohu/hohu-cli)** 是为 `hohu-admin` 生态量身打造的现代化命令行工具。它集成了项目脚手架生成、自动化环境初始化和多语言切换等功能，旨在提升HoHu Admin开发者的生产力。

1. 安装CLI 
	使用 `uv` (推荐) 或 `pip` 进行全局安装：
	```bash
	# 使用 uv
	uv tool install hohu
	
	# 或使用 pip
	pip install hohu
	```
2. 创建新项目
	```bash
	hohu admin create my-project
	```
3. 初始化环境
	```bash
	hohu admin init
	```
4. 运行项目
	```
	hohu admin dev
	```



### 手动配置项目

#### 1. 环境准备

确保已安装 uv, Python 3.10+, PostgreSQL, Redis。

#### 2. 安装依赖

使用以下命令激活虚拟环境：

```bash
source .venv/bin/activate
```

安装所有依赖项：

```bash
uv sync
```


#### 3. 配置环境变量

拷贝 `.env.example` 并更名为 `.env`，配置你的数据库和 Redis 连接：

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/hohu_admin
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=your-super-secret-key
```

#### 4. 数据库迁移与初始化

```bash
# 执行迁移
alembic upgrade head

# 运行初始化脚本
python scripts/init_db.py
```

#### 5. 启动服务

```bash
fastapi dev app/main.py
```

访问：[http://127.0.0.1:8000/docs](https://www.google.com/search?q=http://127.0.0.1:8000/docs) 查看交互式文档。



## 📝 接口规范

### 统一响应格式

```json
{
  "code": 200,
  "msg": "success",
  "data": { ... }
}
```

### ID 处理

由于使用 Snowflake 算法，所有的 `user_id` 等主键在 JSON 序列化时会**自动转换为字符串**，防止前端 `JSON.parse` 导致的精度截断。

------



## 📚 开发文档

详细的开发指南和最佳实践，请查看相关文档：

- **[TODO.md](./TODO.md)** - 项目待办事项清单，包含代码审查发现的所有优化点
- **[docs/pagination-guide.md](./docs/pagination-guide.md)** - 详细的分页查询使用说明，包括基础查询和复杂查询的完整示例
- **复杂 SQL 处理** - JOIN、子查询、聚合查询等各种场景的解决方案

## 📝 项目开发规范

### 字段命名与前后端对接

#### 1. 命名规范

- **后端（Python/SQLAlchemy/Pydantic）**：统一使用 `snake_case`（蛇形命名），例如 `i18n_key`。
- **前端（JSON/JavaScript）**：统一使用 `camelCase`（驼峰命名），例如 `i18nKey`。

#### 2. 自动转换机制

项目通过 Pydantic 的 `alias_generator` 实现自动转换。在基类或模型中通过以下配置开启：

```python
model_config = ConfigDict(
    alias_generator=to_camel, # 自动将 snake_case 转换为 camelCase
    populate_by_name=True,    # 允许同时通过别名或原始字段名赋值
    from_attributes=True      # 支持从数据库模型对象直接转换
)
```

#### 3. 常见陷阱：特殊缩写处理（如 i18n）

##### 问题描述

Pydantic 默认的 `to_camel` 算法在处理包含数字的字段名时，会将数字后的字母视为新单词。

- **预期**：`i18n_key` $\rightarrow$ `i18nKey`
- **实际**：`i18n_key` $\rightarrow$ `i18NKey`（注意大写的 **N**）

##### 解决方案

对于此类不符合预期的特殊字段，**必须手动指定别名**以覆盖自动生成逻辑。请在模型定义中使用 `Field(alias="...")`。

##### 正确示例

```python
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

class MenuSchema(BaseModel):
    parent_id: int | None = None  # 自动转为 parentId
    # 手动指定，确保输入和输出都是 i18nKey
    i18n_key: str | None = Field(None, alias="i18nKey")

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )
```

#### 4. 数据流向说明

| **场景**         | **使用方法**               | **字段名 (示例)**         |
| ---------------- | -------------------------- | ------------------------- |
| **前端请求后端** | JSON Body                  | `{ "i18nKey": "..." }`    |
| **后端模型内部** | `menu.i18n_key`            | 使用蛇形变量名            |
| **后端入库**     | `menu.model_dump()`        | 导出 `i18n_key` (匹配 DB) |
| **后端返回前端** | `ResponseModel(data=menu)` | 自动转为 `i18nKey`        |

> **注意**：在调用 `.model_dump()` 时，除非是为了调试查看，否则**不要**添加 `by_alias=True`，以免导出驼峰字段导致数据库写入失败。



## 🛠️ 二次开发指导

### 通用分页工具

HoHu Admin 提供了统一的分页查询工具 `app/utils/pagination.py`，帮助开发者快速实现标准化的分页接口。

#### 功能特点

- ✅ **消除重复代码**：统一的分页逻辑，避免每个接口重复编写
- ✅ **灵活的过滤条件**：支持多种查询操作（模糊匹配、精确匹配、范围查询等）
- ✅ **预加载支持**：自动处理关联数据的 N+1 查询问题
- ✅ **类型安全**：完整的类型提示，提高代码可维护性

#### 基础用法

##### 1. 使用 `paginate()` 函数

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserQuery, UserItemOut
from app.utils.pagination import paginate

router = APIRouter()

@router.get("/list", response_model=ResponseModel[PageResult[UserItemOut]])
async def get_user_list(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
):
    # 使用通用分页查询
    page_data = await paginate(
        db=db,
        model=User,
        query_params=query,
        order_by=User.create_time.desc(),  # 可选：排序条件
    )

    return ResponseModel.success(data=page_data)
```

##### 2. 使用 `build_filters()` 构建查询条件

```python
from app.utils.pagination import build_filters

# 定义字段映射
field_mapping = {
    "user_name": ("user_name", "contains"),   # 模糊匹配
    "status": ("status", "=="),               # 精确匹配
    "role_id": ("role_id", "in_"),          # 在列表中
}

# 构建过滤条件
filters = build_filters(
    User,
    field_mapping,
    **query.model_dump()  # 展开查询参数
)
```

#### 高级用法

##### 1. 预加载关联数据

```python
from sqlalchemy.orm import selectinload

# 分页时预加载角色信息，避免 N+1 查询
page_data = await paginate(
    db=db,
    model=User,
    query_params=query,
    filters=filters,
    order_by=User.create_time.desc(),
    eager_loads=[selectinload(User.roles)],  # 预加载关联
)
```

##### 2. 自定义过滤逻辑

```python
from app.utils.pagination import build_filters

# 使用可调用对象实现自定义过滤
def custom_name_filter(model, value):
    if not value:
        return None
    return model.user_name.ilike(f"%{value}%")

field_mapping = {
    "user_name": custom_name_filter,  # 自定义函数
    "status": ("status", "=="),
}

filters = build_filters(User, field_mapping, **query.model_dump())
```

#### 完整示例

```python
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    UserCreate,
    UserItemOut,
    UserQuery,
    UserUpdate,
)
from app.utils.pagination import build_filters, paginate

router = APIRouter()

@router.get("/list", response_model=ResponseModel[PageResult[UserItemOut]])
async def get_user_list(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. 构建查询条件
    field_mapping = {
        "user_name": ("user_name", "contains"),
        "nickname": ("nickname", "contains"),
        "user_gender": ("user_gender", "contains"),
        "user_phone": ("user_phone", "contains"),
        "user_email": ("user_email", "contains"),
        "status": ("status", "=="),
    }
    filters = build_filters(User, field_mapping, **query.model_dump())

    # 2. 使用通用分页查询
    page_data = await paginate(
        db=db,
        model=User,
        query_params=query,
        filters=filters,
        order_by=User.create_time.desc(),
        eager_loads=[selectinload(User.roles)],
    )

    # 3. 转换数据格式（如需要）
    user_list = []
    for u in page_data.records:
        item = UserItemOut.model_validate(u)
        item.roles = [r.role_code for r in u.roles]
        user_list.append(item)

    # 4. 返回结果
    return ResponseModel.success(
        data=PageResult(
            records=user_list,
            total=page_data.total,
            current=page_data.current,
            size=page_data.size,
        )
    )
```

#### 参数说明

##### `paginate()` 函数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `db` | `AsyncSession` | 是 | 数据库会话 |
| `model` | `Any` | 是 | SQLAlchemy 模型类 |
| `query_params` | `QueryParams` | 是 | 查询参数（包含 `current`, `size`） |
| `filters` | `list[Any] \| None` | 否 | 查询条件列表 |
| `order_by` | `Any \| None` | 否 | 排序条件（如 `Model.field.desc()`） |
| `eager_loads` | `list[Any] \| None` | 否 | 预加载的关联关系列表 |

##### `build_filters()` 函数

| 参数 | 类型 | 说明 |
|------|------|------|
| `model` | `Any` | SQLAlchemy 模型类 |
| `field_mapping` | `dict[str, str \| tuple \| Callable]` | 字段映射字典 |
| `**kwargs` | - | 查询参数键值对 |

##### 字段映射格式

| 格式 | 说明 | 示例 |
|------|------|------|
| `("field_name", "contains")` | 模糊匹配 | `("user_name", "contains")` |
| `("field_name", "==")` | 精确匹配 | `("status", "==")` |
| `("field_name", "in_")` | 在列表中 | `("role_id", "in_")` |
| `("field_name", ">=")` | 大于等于 | `("create_time", ">=")` |
| `("field_name", "<=")` | 小于等于 | `("create_time", "<=")` |
| `"field_name"` | 简单字段名，默认精确匹配 | `"user_name"` |
| `Callable` | 自定义过滤函数 | `lambda m, v: m.field.ilike(v)` |

#### 注意事项

1. **SQLAlchemy 表达式对象**：`order_by` 参数不能直接用于布尔判断，内部已使用 `is not None` 进行检查
2. **空值过滤**：`build_filters()` 会自动跳过 `None` 和空字符串的参数
3. **分页参数**：查询 Schema 需要继承或包含 `current` 和 `size` 字段
4. **类型转换**：如需对返回数据进行特殊处理，建议在获取 `page_data.records` 后手动转换

### 如何增加新模块？

1. 在 `app/modules/` 下新建文件夹。
2. 定义 `models.py` (SQLAlchemy 实体)。
3. 定义 `schemas.py` (Pydantic 模型，建议开启 `alias_generator=to_camel`)。
4. 在 `api.py` 编写接口并使用 `get_current_user` 进行权限保护。
5. 在 `app/main.py` 挂载路由。
