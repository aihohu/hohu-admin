# 初始化与部署脚本

用户统一使用 hohu-cli 的 `hohu init`、`hohu deploy init` 和 `hohu deploy`。本目录是 CLI 调用的内部实现，不要求用户逐个执行脚本。

## 执行顺序

本地初始化入口为 `scripts/init.py`；部署先运行 `alembic upgrade head`，成功后调用 `python -m scripts.init_db`，再启动应用。任一步失败即停止，不能通过清库或 `stamp head` 跳过错误。

统一种子在一个事务内补齐默认租户、菜单、设置、内置 Agent/Prompt 以及首次管理员。首次默认租户安装需要 `HOHU_ADMIN_PASSWORD`，由 CLI 生成并写入部署配置；初始化不交互、不清库、不打印密码。

## 重复执行与职责

- 重跑保留已有密码、改名的管理员、自定义设置和 Prompt、角色授权及禁用状态，只按规则补缺。
- 菜单定义唯一来源为 [menu_seed.py](../app/modules/system/menu_seed.py)，由 `sync_menus.py` 同步；不从默认租户可编辑的数据复制到新租户。
- hosted 菜单按显式能力集合同步，只处理已 bootstrap 的租户，不自动开通 prepared 租户。
- `seed_settings.py` 补齐 `sys_setting` 的内置设置；`sys_config` 自定义参数不由种子生成。
- `seed_ai_agents.py` 同步内置 Agent 与默认 Prompt，保留部署方自定义内容。工具启用的 fresh/upgrade 默认值有区别，不覆盖显式禁用状态。
- 数据库结构由 Alembic 负责，迁移和数据初始化不能互相代替。

脚本清单见 [scripts/README.md](../scripts/README.md)，设置行为见 [系统设置](SYSTEM-SETTINGS.md)，支持的升级路径见 [数据库迁移](DATABASE-MIGRATIONS.md)。

## 维护与验证

平台运维在 `tools/ops`，静态检查在 `tools/checks`，可选演示在 `tools/demo`，测试与发布验收在 `tests/`。部署不会自动执行演示或测试数据脚本。

修改初始化时验证首次安装、幂等重跑、失败回滚、密码保护、自定义内容保留以及租户隔离。回归入口：[tests/scripts](../tests/scripts)。CLI 与后端需同步升级，不维护多份旧初始化逻辑。

## 决策记录

1. **唯一菜单目录** — fresh、同步与 hosted 初始化使用同一维护来源和显式能力集合。**反例**: 从可编辑的默认租户菜单复制权限。**回归**: tests/scripts/test_deployment_seed.py。
2. **无交互且无清库的统一事务** — CLI 根据退出码可靠阻断失败，重复运行不重置用户数据。**反例**: 容器中的 input 阻塞，或清库后初始化中途失败。**回归**: tests/scripts/test_init_db_seed.py、tests/scripts/test_init_migration_failure.py。
3. **空环境变量的注释独占一行** — dotenv 必须把无密码 Redis 配置解析为空字符串。**反例**: `REDIS_PASSWORD=  # comment` 将注释解析为密码，破坏连接 URL。**回归**: tests/tools/test_env_example.py；CLI 独立项目初始化与登录验收。✅ Plan redis-template 已完成（2026-09-29）。
