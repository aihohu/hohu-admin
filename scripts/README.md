# 部署内部脚本

用户通过 **hohu-cli** 初始化、部署与升级，无需逐个执行此目录文件。

| 文件 | 职责 |
| --- | --- |
| init.py | `hohu init` 的本地环境准备、迁移和种子编排 |
| init_db.py | 部署统一种子入口：单个事务、安全补齐、首次管理员 |
| sync_menus.py | 默认租户菜单及相应角色菜单关系同步 |
| seed_settings.py | 独立 sys_setting 表的租户设置补缺，自定义参数不播种 |
| seed_ai_agents.py | 内置 Agent 与默认 Prompt 同步，保留部署方自定义 |

数据库结构由 Alembic 管理。统一数据入口为 `python -m scripts.init_db --inventory INVENTORY`，
首次安装增加 `--bootstrap`；密码由 `AUTHORIZATION_MAINTENANCE_PASSWORD` 提供。
`scripts/init.py` 从环境读取 `AUTHORIZATION_MAINTENANCE_INVENTORY`、
`AUTHORIZATION_CONTROL_DIR` 和 `AUTHORIZATION_BOOTSTRAP`，并保留
`HOHU_ADMIN_PASSWORD` 作为维护密码后备。缺少清单时不执行迁移或种子。
成功后数据库仍停写，需要[显式恢复](../docs/AUTHORIZATION-MAINTENANCE.md)。
首次安装和更新共用 `app/modules/system/menu_seed.py` 的菜单定义。

新初始化的“系统管理”按以下顺序展示：

```text
系统管理
├── 系统设置
├── 字典管理
├── 自定义参数
├── 文件管理
├── 监控管理
│   ├── 操作日志
│   └── 登录日志
└── 演示管理
    ├── 仪表盘
    └── 数据权限演示
```

字典数据保留为隐藏辅助页面；hosted 租户的演示管理只包含仪表盘。
已有系统菜单保留其分组和排序，增量同步仅补齐缺少的菜单，不按上述结构重排历史记录。
默认清单不包含应用市场、应用详情、应用管理及其子菜单和按钮权限；已有记录及授权不自动删除。

开发检查位于 `tools/checks`，可选演示数据位于 `tools/demo`，
平台运维和发布审计位于 `tools/ops`，隔离发布验收位于 `tests/release`。
运维工具使用 `python -m tools.ops.<工具名>`，发布验收使用
`python -m tests.release.qualify_tenant_hosted_release`，参数保持各工具的 `--help` 契约。

详细行为与验证记录见 [SCRIPTS-DEPLOYMENT.md](../docs/SCRIPTS-DEPLOYMENT.md)。

系统设置与自定义参数分别使用 `sys_setting` 和 `sys_config`。拆表由 Alembic 自动搬迁旧值，再由初始化补齐两个菜单；见 [SYSTEM-SETTINGS.md](../docs/SYSTEM-SETTINGS.md)。
