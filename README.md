# 🥞 HoHu Admin

<div align="center">
  <span>中文 | <a href="./README.en_US.md">English</a></span>
</div>

[![license](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![github stars](https://img.shields.io/github/stars/aihohu/hohu-admin)](https://github.com/aihohu/hohu-admin)
[![github forks](https://img.shields.io/github/forks/aihohu/hohu-admin)](https://github.com/aihohu/hohu-admin)
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

### 1. 环境准备

确保已安装 uv, Python 3.10+, PostgreSQL, Redis。

### 2. 安装依赖

安装所有依赖项：

```bash
uv sync
```

使用以下命令激活虚拟环境：

```bash
source .venv/bin/activate
```

### 3. 配置环境变量

拷贝 `.env.example` 并更名为 `.env`，配置你的数据库和 Redis 连接：

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/hohu_admin
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=your-super-secret-key
```

### 4. 数据库迁移与初始化

```bash
# 执行迁移
alembic upgrade head

# 运行初始化脚本
python scripts/seed_data.py
```

### 5. 启动服务

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

你好！我是**编码助手**。

这是一个非常棒的主意！在 `README.md` 或项目的 `docs/conventions.md` 中记录这些“坑”和规范，对于团队协作至关重要。

以下我为你准备了一份专门针对 **Pydantic 字段别名与驼峰转换** 的说明文档。你可以直接复制到你的项目中。

------



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

### 如何增加新模块？

1. 在 `app/modules/` 下新建文件夹。
2. 定义 `models.py` (SQLAlchemy 实体)。
3. 定义 `schemas.py` (Pydantic 模型，建议开启 `alias_generator=to_camel`)。
4. 在 `api.py` 编写接口并使用 `get_current_user` 进行权限保护。
5. 在 `app/main.py` 挂载路由。
