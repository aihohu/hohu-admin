# HoHu Admin

<p align="center">
  <b>AI-native enterprise application platform · Backend</b>
</p>

<p align="center">
  <a href="https://hohu.org/">Website</a> ·
  <a href="https://show.hohu.org">Live demo</a> ·
  <a href="https://hohu.org/guide/user/">User guide</a> ·
  <a href="https://hohu.org/guide/development/">Developer guide</a>
</p>

<p align="center">
  English · <a href="./README.zh_CN.md">简体中文</a>
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

**HoHu is an AI-native enterprise application platform for building business applications and completing work through application interfaces and AI assistants.**

This repository contains HoHu's FastAPI backend, including authentication, permissions and data access controls, organization management, files, scheduled jobs, AI conversations, business tool calls, and operation confirmations. It works with the Web, mobile, and desktop clients.

## Core capabilities

- **Application AI assistants**: Streaming conversations, message history, model and assistant configuration, and permission-controlled business tool calls with operation confirmations.
- **AI-assisted development**: HoHu Skills guide coding assistants through project creation, business module development, and verification using project conventions.
- **Identity and access**: JWT authentication and session management, role and menu permissions, data scopes, and tenant boundaries. Registration and password recovery require account settings and mail configuration.
- **Administration**: Users, departments, dictionaries, system settings, custom parameters, and file handling with format, size, and access checks.
- **Jobs and operations**: Scheduled jobs and execution logs, a separate scheduler process, and Docker Compose deployment through the CLI.
- **Backend foundations**: FastAPI, SQLAlchemy 2.0 async sessions, Pydantic validation, Alembic migrations, and shared error handling.

## Quick start

The following paths create and run Backend and Web locally. Prepare Git, Python 3.12+, uv, Node.js, pnpm, and reachable PostgreSQL and Redis services for development. See the [development setup guide](https://hohu.org/guide/quick-start.html) for versions and connection configuration.

Install HoHu CLI:

```bash
uv tool install hohu
hohu --version
```

### Create and develop with AI

Run this in the parent workspace where you plan to create the project:

```bash
npx skills@latest add aihohu/hohu-skills
```

Select `hohu-project`, `hohu-business-module`, and your coding assistant, such as Claude Code, Cursor, or Codex. Reopen the assistant in that workspace, confirm it recognizes the Skills, and ask:

> Use hohu-project to create my-project with Backend and Web only. Check the development environment and database configuration, initialize and start the project, then report the URLs and verification results.

For an existing project, install and use Skills from the common Backend/Web project root. Once the project is running, describe a business feature, for example:

> Use hohu-business-module to build an equipment lending module with an equipment list, borrowing, returns, and loan records. Ordinary users may only view and return their own loans, and equipment cannot be borrowed twice at the same time. Provide Web pages, AI queries, and borrowing with confirmation, and verify permissions and business constraints.

Equipment lending is a development example to implement in your project. Skills provide workflows; they do not replace CLI, database, or development environment installation. Application AI models are configured separately from the coding assistant. See [install Skills](https://hohu.org/guide/cli/skills.html) and [AI-assisted development](https://hohu.org/guide/ai-coding.html).

### Create a project with CLI

You can also run the commands directly. Select **Backend** and **Frontend** in the creation wizard:

```bash
hohu create my-project
cd my-project
hohu init
hohu dev
```

`hohu init` installs dependencies, prepares the backend `.env`, runs migrations, and synchronizes built-in data. If the first run fails to connect, update PostgreSQL and Redis settings in `hohu-admin/.env` and run `hohu init` again. Keep the generated `SECRET_KEY` and `HOHU_ADMIN_PASSWORD`; repeating initialization preserves existing configuration and user passwords.

### Access the running application

| Entry | Default address |
| --- | --- |
| Web application | <http://localhost:9527> |
| Backend API | <http://127.0.0.1:8000> |
| Development OpenAPI | <http://127.0.0.1:8000/docs> |

Use the terminal output if ports differ. For first sign-in, use `admin` and the backend `.env` value of `HOHU_ADMIN_PASSWORD`. If you have already changed it, use the updated password. Change the initial password after signing in.

Application AI requires a provider, model, and appropriate authorization. See [AI configuration and operations](https://hohu.org/guide/operations/ai.html).

## Deployment

Server deployment uses HoHu CLI and Docker Compose. With Docker and Compose ready, initialize image deployment in a dedicated empty directory without creating a source project first:

```bash
mkdir my-hohu-deploy
cd my-hohu-deploy
hohu deploy init
```

Edit `.hohu/deploy/.env` to select compatible images and configure PostgreSQL, Redis, access ports, and persistent storage. Web/API ports are not published by default. Keep the generated secrets and initial administrator password, configure access, then run:

```bash
hohu deploy
```

Deployment runs migrations and synchronizes built-in data. Configure the domain and HTTPS and verify application behavior before launch. See the [deployment guide](https://hohu.org/guide/deploy.html) for new installations and [upgrade, backup, and recovery](https://hohu.org/guide/operations/upgrade.html) for existing instances.

## Technology and related projects

| Layer | Technologies |
| --- | --- |
| Runtime and API | Python 3.12+, FastAPI, Pydantic v2, Uvicorn |
| Data and migrations | PostgreSQL, SQLAlchemy 2.0 async, Alembic |
| Cache and authentication | Redis, JWT, bcrypt |
| AI | OpenAI SDK, pydantic-ai |
| Jobs | APScheduler |
| Tooling and tests | uv, Ruff, pytest |

Components are maintained in separate repositories:

| Project | Purpose |
| --- | --- |
| [hohu-admin-web](https://github.com/aihohu/hohu-admin-web) | Vue 3, Naive UI, and TypeScript Web administration UI |
| [hohu-admin-app](https://github.com/aihohu/hohu-admin-app) | Mobile application and mini-program built with uni-app and Vue 3 |
| [hohu-admin-desktop](https://github.com/aihohu/hohu-admin-desktop) | Electron desktop client |
| [hohu-cli](https://github.com/aihohu/hohu-cli) | Project creation, initialization, development, and deployment |
| [hohu-skills](https://github.com/aihohu/hohu-skills) | Skills for project creation and business module development |
| [hohu-admin-docs](https://github.com/aihohu/hohu-admin-docs) | Website and bilingual documentation |

## Development and contributions

Start with the [developer guide](https://hohu.org/guide/development/). Follow [add a module](https://hohu.org/guide/development/module.html) to implement models, migrations, APIs, permissions, and pages, then connect [business tools](https://hohu.org/guide/development/ai-tools.html) when the module needs application AI.

Ordinary JSON endpoints use `{code, msg, data}` with success code `200`. Snowflake IDs are strings, and Schema aliases expose camelCase fields. SSE, downloads, health checks, and metrics use their own response formats. See [API reference](https://hohu.org/guide/reference/api.html) for conventions and the target release's OpenAPI for the endpoint inventory. Built-in OpenAPI pages are disabled with `ENV=prod`.

Before changing core code, read the [contribution guide](docs/CONTRIBUTING.md), [architecture contracts](docs/ARCHITECTURE-GUIDELINES.md), and [testing guide](docs/TESTING-GUIDELINES.md). The full index is in [architecture and maintenance documentation](docs/README.md).

Run checks from the backend directory after preparing the test environment described in the testing guide:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --basetemp=.local/tests/pytest
uv run python -m tools.checks.check_docs
```

Issue reports should include versions, reproduction steps, and redacted logs. Report vulnerabilities according to the [security policy](docs/SECURITY.md).

## License

[Apache License 2.0](./LICENSE) &copy; HoHu

See [NOTICE](./NOTICE) and the [license policy](docs/LICENSE-POLICY.md) for scope and distribution requirements.
