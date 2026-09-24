# 部署内部脚本

用户通过 **hohu-cli** 初始化、部署与升级，无需逐个执行此目录文件。

| 文件 | 职责 |
| --- | --- |
| init.py | `hohu init` 的本地环境准备、迁移和种子编排 |
| init_db.py | 部署统一种子入口：单个事务、安全补齐、首次管理员 |
| sync_menus.py | 默认租户与已初始化 hosted 租户的菜单同步 |
| seed_config.py | 租户范围内的基础配置补缺 |
| seed_ai_agents.py | 内置 Agent 与默认 Prompt 同步，保留部署方自定义 |

数据库结构由 Alembic 管理。统一数据入口为 `python -m scripts.init_db`，
首次安装从 `HOHU_ADMIN_PASSWORD` 读取密码；已有部署不需要重新输入，也不会重置密码。
首次安装和更新共用 `app/modules/system/menu_seed.py` 的菜单定义。

开发检查位于 `tools/checks`，可选演示数据位于 `tools/demo`，
平台运维和发布审计位于 `tools/ops`，隔离发布验收位于 `tests/release`。
运维工具使用 `python -m tools.ops.<工具名>`，发布验收使用
`python -m tests.release.qualify_tenant_hosted_release`，参数保持各工具的 `--help` 契约。

详细行为与验证记录见 [SCRIPTS-DEPLOYMENT.md](../docs/SCRIPTS-DEPLOYMENT.md)。
