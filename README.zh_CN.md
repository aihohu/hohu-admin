# HoHu Admin

项目文档：[正式文档入口](docs/README.md)。

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
  <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="license" />
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


### 使用 HoHu CLI

项目统一使用 [HoHu CLI](https://github.com/aihohu/hohu-cli) 完成初始化、部署和升级，
无需手动逐个执行后端脚本。

```bash
uv tool install hohu
hohu create my-project
cd my-project
hohu init
hohu dev
```

Docker 部署：

```bash
hohu deploy init
hohu deploy
```

CLI 自动生成初始管理员密码，保存在 `.hohu/deploy/.env` 的 `HOHU_ADMIN_PASSWORD`；
本地开发对应后端 `.env`。部署自动完成迁移和基础数据同步，重复执行不清库、不重置密码。
开发维护者可查阅 [部署内部契约](docs/SCRIPTS-DEPLOYMENT.md)。

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

完整维护入口见 [文档索引](docs/README.md)。扩展前先阅读：

- [模块开发](docs/MODULE-DEVELOPMENT-GUIDE.md)：模型、迁移、Service 和 API。
- [架构与契约](docs/ARCHITECTURE-GUIDELINES.md)：ID、字段命名、响应与租户边界。
- [按钮权限](docs/button-permission-guide.md)及[数据权限](docs/data-scope-guide.md)：功能授权与数据范围。
- [分页查询](docs/pagination-guide.md)：租户过滤、计数与稳定排序。
- [贡献指南](docs/CONTRIBUTING.md)及[测试指南](docs/TESTING-GUIDELINES.md)：开发流程和验证。

## 许可证

[Apache License 2.0](./LICENSE) &copy; HoHu

授权范围和分发要求见 [NOTICE](./NOTICE) 与[许可证政策](./docs/LICENSE-POLICY.md)。
