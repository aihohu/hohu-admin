# 部署脚本与基础数据统一

状态：✅ Plan scripts-deployment 已完成（2026-09-24）。

## 目标与边界

v0.1.5 以 hohu-cli 为用户配置、部署、升级入口，CLI 与后端同步更新，不保留旧脚本兼容包装。保留已完成的核心多租户行为（见 MULTI-TENANCY.md），不扩大 Marketplace/Lowcode hosted 能力。

## 设计

- `scripts/init.py` 负责本地环境准备、迁移和调用统一数据入口；`scripts/init_db.py` 在一个种子事务中完成默认租户、菜单、配置、内置 Agent/Prompt 和首次管理员授权。后者无 input、不清库、不打印密码。
- `sync_menus.py` 负责菜单同步，`init_db.py` 不再维护菜单定义。System 模块保存唯一静态菜单目录，默认安装与 hosted 开通使用显式能力集合，禁止从可编辑的租户 0 数据复制到新租户。
- 首次默认租户初始化需要 HOHU_ADMIN_PASSWORD；已有默认用户时不再创建管理员，允许管理员改名。部署重复运行保留密码、自定义配置、角色授权及禁用状态。
- 配置按 tenant_id/config_key 补缺；fresh 显式启用 file.parse，upgrade 补缺保持为空。Agent 及 Prompt 统一编排，保留部署方自定义内容。
- 已 bootstrap 的其他租户同步其 hosted 菜单；prepared 且未 bootstrap 的租户不自动开通。数据库结构仍由 Alembic 负责，种子不能 stamp 或替代迁移。
- 平台管理和发布审计移至 tools/ops；静态检查移至 tools/checks；demo 移至 tools/demo；隔离发布验收及 worker 移至 tests/release。所有调用、测试、镜像和维护文档同步更新。
- CLI Compose 使用失败即停的单次 migrator，迁移成功后始终运行统一种子，再启动应用；自动生成初始管理员密码写入部署 .env，日志只提示存储位置。无需 --init 选择首次安装。

## 决策记录

1. **唯一菜单目录与显式 hosted 集合** — 消除 fresh、sync、hosted 三份定义漂移，同时保持既有租户能力边界。**反例**: 新租户从默认租户可编辑菜单复制，或把 Marketplace 权限直接发给 hosted。**回归**: tests/scripts/test_deployment_seed.py、tests/scripts/test_init_db_seed.py。
2. **无交互且无清库的统一种子事务** — CLI 只需处理退出码，重跑不会丢失数据或重置密码。**反例**: input 在容器中 EOF，或 TRUNCATE 后下一阶段失败。**回归**: tests/scripts/test_deployment_seed.py、tests/scripts/test_init_migration_failure.py。
3. **CLI 与后端同步升级** — 当前没有真实用户，无需旧路径兼容层；保留明确的内部入口即可。**反例**: 同一逻辑继续保留多个独立实现。**回归**: hohu-cli/tests/test_deployment_bootstrap.py。

## 验收

- 首次安装具备全部基础配置与内置 Prompt；重复执行不重复写入、不扩大已有角色授权。
- 跨租户同名菜单/配置不互相影响，hosted bootstrap 仍使用相同不可变定义。
- 迁移或种子失败阻断服务启动；密码不进入参数和日志。
- 后端及 CLI Ruff、定向测试与全量测试；后端覆盖率至少 70%。

### 验证记录（2026-09-24）

- 后端全量测试：2861 passed，覆盖率 79.76%；CLI 全量测试：40 passed。
- 后端及 CLI Ruff 检查与格式检查通过；33 个 AI 工具通过 13 项静态检查；git diff --check 通过。
- 已验证初始化幂等、事务回滚、租户隔离和部署失败阻断；未执行实际 Docker 部署及独立环境发布验收。
