# HoHu Admin

<p align="center">
  <b>企业 AI 原生应用平台 · 后端</b>
</p>

<p align="center">
  <a href="https://hohu.org/zh/">官网</a> ·
  <a href="https://show.hohu.org">在线体验</a> ·
  <a href="https://hohu.org/zh/guide/user/">使用文档</a> ·
  <a href="https://hohu.org/zh/guide/development/">开发指南</a>
</p>

<p align="center">
  简体中文 · <a href="./README.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="Apache License 2.0" />
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-3776AB.svg" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/FastAPI-009688.svg" alt="FastAPI" />
  <img src="https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg" alt="SQLAlchemy 2.0" />
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1.svg" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/Redis-DC382D.svg" alt="Redis" />
  <img src="https://img.shields.io/github/stars/aihohu/hohu-admin" alt="GitHub stars" />
</p>

**HoHu 是企业 AI 原生应用平台，帮助开发者构建业务应用，让用户通过业务界面与 AI 助手完成工作。**

本仓库提供 HoHu 的 FastAPI 后端，包含身份认证、权限与数据范围、组织管理、文件、定时任务，以及 AI 对话、业务工具调用和操作确认等能力，可与 Web、移动端和桌面端配合使用。

## 核心能力

- **应用内 AI 助手**：流式对话、会话历史、模型与助手配置，以及受权限约束的业务工具调用和操作确认。
- **AI 辅助开发**：通过 HoHu Skills，让编程助手按项目约定创建项目、开发业务模块并执行验证。
- **身份与权限**：JWT 登录与会话管理、角色与菜单权限、数据范围及租户边界；注册和找回密码需启用账号设置并配置邮件服务。
- **管理基础能力**：用户、部门、字典、系统设置、自定义参数，以及带格式、大小和访问校验的文件处理。
- **任务与运维**：定时任务和执行日志、独立 scheduler 进程，以及 CLI 编排的 Docker Compose 部署。
- **后端开发基础**：FastAPI、SQLAlchemy 2.0 异步会话、Pydantic 校验、Alembic 迁移和统一错误处理。

## 快速开始

以下流程在本机创建并运行 Backend 与 Web。先准备 Git、Python 3.12+、uv、Node.js、pnpm，以及可连接的独立开发数据库 PostgreSQL 和 Redis；具体版本与连接配置见[开发环境指南](https://hohu.org/zh/guide/quick-start.html)。

安装 HoHu CLI：

```bash
uv tool install hohu
hohu --version
```

### 通过 AI 创建与开发

在准备创建项目的父工作区执行：

```bash
npx skills@latest add aihohu/hohu-skills
```

选择 `hohu-project`、`hohu-business-module`，以及你使用的编程助手，例如 Claude Code、Cursor 或 Codex。在该工作区重新打开助手，确认它能识别 Skills，再提出：

> 使用 hohu-project 创建 my-project，只需要 Backend 和 Web。检查开发环境和数据库配置，初始化并启动项目，完成后给出访问地址与验证结果。

已有项目时，在 Backend/Web 的共同项目根目录安装并使用 Skills。项目运行后，可以继续描述业务需求，例如：

> 使用 hohu-business-module 开发设备借用模块，支持设备列表、借用、归还及借用记录。普通用户只能查看和归还自己的记录，同一设备不能重复借用。提供 Web 页面、AI 查询和确认后执行的借用操作，并验证权限与业务约束。

设备借用是开发示例，需要在项目中实现。Skills 提供开发工作流，不代替 CLI、数据库和开发环境安装；应用内 AI 的模型配置也独立于编程助手。详细步骤见[安装 Skills](https://hohu.org/zh/guide/cli/skills.html)和 [AI 辅助开发](https://hohu.org/zh/guide/ai-coding.html)。

### 通过 CLI 创建项目

也可以直接运行命令，按创建向导选择 **Backend** 和 **Frontend**：

```bash
hohu create my-project
cd my-project
hohu init
hohu dev
```

`hohu init` 安装依赖、准备后端 `.env`，执行迁移和基础数据同步。首次运行若数据库连接失败，修改 `hohu-admin/.env` 中的 PostgreSQL、Redis 连接后重新运行 `hohu init`。保留生成的 `SECRET_KEY` 和 `HOHU_ADMIN_PASSWORD`；已有配置与用户密码不会因再次初始化而重置。

### 运行后访问

| 入口 | 默认地址 |
| --- | --- |
| Web 应用 | <http://localhost:9527> |
| 后端 API | <http://127.0.0.1:8000> |
| 开发环境 OpenAPI | <http://127.0.0.1:8000/docs> |

端口变化时以终端输出为准。首次登录使用用户名 `admin`，密码为后端 `.env` 中的 `HOHU_ADMIN_PASSWORD`；如果已经修改，使用修改后的密码。登录后更换初始密码。

应用内 AI 还需配置模型提供商、模型及相应授权，见 [AI 配置与运维](https://hohu.org/zh/guide/operations/ai.html)。

## 部署

服务器部署使用 HoHu CLI 与 Docker Compose。准备 Docker 和 Compose 后，可在专用空目录初始化镜像部署，无需先创建源码项目：

```bash
mkdir my-hohu-deploy
cd my-hohu-deploy
hohu deploy init
```

编辑 `.hohu/deploy/.env`，确认配套镜像版本、数据库、Redis、访问端口和持久存储；默认不发布 Web/API 端口。保留生成的密钥及首次管理员密码，配置访问方式后执行：

```bash
hohu deploy
```

部署会执行迁移和基础数据同步。正式上线还需配置域名与 HTTPS，并验证应用功能。首次安装见[部署指南](https://hohu.org/zh/guide/deploy.html)，已有实例先看[升级、备份与恢复](https://hohu.org/zh/guide/operations/upgrade.html)。

## 技术栈与相关项目

| 层级 | 技术 |
| --- | --- |
| 运行时与 API | Python 3.12+、FastAPI、Pydantic v2、Uvicorn |
| 数据与迁移 | PostgreSQL、SQLAlchemy 2.0 async、Alembic |
| 缓存与认证 | Redis、JWT、bcrypt |
| AI | OpenAI SDK、pydantic-ai |
| 任务 | APScheduler |
| 工具与测试 | uv、Ruff、pytest |

各组件在独立仓库维护：

| 项目 | 用途 |
| --- | --- |
| [hohu-admin-web](https://github.com/aihohu/hohu-admin-web) | Vue 3、Naive UI、TypeScript Web 管理界面 |
| [hohu-admin-app](https://github.com/aihohu/hohu-admin-app) | 基于 uni-app 和 Vue 3 的移动端与小程序 |
| [hohu-admin-desktop](https://github.com/aihohu/hohu-admin-desktop) | Electron 桌面客户端 |
| [hohu-cli](https://github.com/aihohu/hohu-cli) | 项目创建、初始化、开发与部署工具 |
| [hohu-skills](https://github.com/aihohu/hohu-skills) | 项目创建与业务模块开发 Skills |
| [hohu-admin-docs](https://github.com/aihohu/hohu-admin-docs) | 官网与中英文帮助文档 |

## 开发与参与贡献

业务扩展从[开发指南](https://hohu.org/zh/guide/development/)开始，按[新增业务模块](https://hohu.org/zh/guide/development/module.html)接入模型、迁移、API、权限和页面，需要应用内 AI 时继续接入[业务工具](https://hohu.org/zh/guide/development/ai-tools.html)。

普通 JSON 接口使用 `{code, msg, data}`，成功码为 `200`；Snowflake ID 以字符串传输，Schema 通过别名提供 camelCase 字段。SSE、文件下载、健康检查和指标接口使用各自的响应格式。详细约定见 [API 参考](https://hohu.org/zh/guide/reference/api.html)，接口清单以目标版本 OpenAPI 为准；`ENV=prod` 时内置 OpenAPI 页面关闭。

修改核心代码前阅读[贡献指南](docs/CONTRIBUTING.md)、[架构与契约](docs/ARCHITECTURE-GUIDELINES.md)和[测试指南](docs/TESTING-GUIDELINES.md)。完整索引见[架构与维护文档](docs/README.md)。

在后端目录执行检查，测试环境准备要求见测试指南：

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --basetemp=.local/tests/pytest
uv run python -m tools.checks.check_docs
```

报告问题请提供版本、复现步骤和脱敏日志。安全漏洞按[安全政策](docs/SECURITY.md)报告。

## 许可证

[Apache License 2.0](./LICENSE) &copy; HoHu

授权范围和分发要求见 [NOTICE](./NOTICE) 与[许可证政策](docs/LICENSE-POLICY.md)。
