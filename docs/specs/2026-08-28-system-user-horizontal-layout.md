# System User 水平分层收口短 spec

状态：✅ 已完成（2026-08-28）

## 1. 背景

`app/modules/system/user/` 最初用于渐进迁移 User 聚合，但最终只承载用户导入导出，
核心 CRUD 仍位于 `models/`、`schemas/`、`service/` 和 `api/`。目录名因此错误表达了
所有权，并让同一业务模块同时存在水平分层和未完成的垂直分层。

本次重构以 `app/modules/system/` 现有水平分层为目标，属于无行为变化的结构收口。

## 2. 目标结构

```text
app/modules/system/
├── api/user.py
├── models/user.py
├── models/user_transfer.py
├── schemas/user.py
├── schemas/user_transfer.py
├── service/
│   ├── user_service.py
│   ├── user_import_service.py
│   ├── user_import_parser.py
│   ├── user_import_state.py
│   ├── user_import_validator.py
│   ├── user_import_template_service.py
│   └── user_export_service.py
└── ai_tools/
```

`app/modules/system/user/` 必须删除；应用代码和测试不得继续导入
`app.modules.system.user.*`。

## 3. 边界

- HTTP 路径、权限码、Pydantic 字段、错误码和状态机保持不变。
- ORM 表名、列、索引和 PostgreSQL enum 保持不变，不生成 Alembic migration。
- 普通 User ORM 由 `models/user.py` 拥有；导入批次与导出任务 ORM 由同层的
  `models/user_transfer.py` 拥有，避免把生命周期不同的审计聚合塞入核心 User 文件。
- 普通 User 与导入导出 API schema 分别位于同层的 `schemas/user.py` 和
  `schemas/user_transfer.py`。
- 解析、引用校验、状态机、模板、导入编排和导出编排保持独立 service 文件；这些
  文件已分别达到约 144–1755 行，继续合并会制造巨型模块。
- 原 `user/__init__.py` 与 `user/service.py` facade 不保留；仓库内调用方一次性切换到
  唯一导入路径，避免双路径长期漂移。

## 4. TDD 与验收

1. 先增加架构契约测试，要求新入口可导入、旧目录不存在、生产代码无旧 import。
2. 迁移代码并更新所有应用与测试 import/monkeypatch target。
3. 运行用户导入导出、AI 用户工具、清理任务相关测试。
4. 运行 Ruff 和后端全量 pytest，覆盖率不得低于 70%。

## 5. 决策记录

1. **System User 统一回归水平分层** — 当前模块已稳定采用 `api/models/schemas/service`
   布局，保留只覆盖导入导出的 `system/user/` 会错误表达领域所有权。
   **反例**: 继续通过 facade 逐步把全部 User 代码迁入 `system/user/`，会迫使 Dept、Role、
   Menu 同步进行大规模垂直切片迁移。**回归**:
   `tests/modules/system/test_user_module_layout.py`。
2. **按职责保留六个导入导出 service 文件** — 解析、安全校验、CAS 状态机、模板和编排
   的变化原因不同，保留边界可避免 2000 行以上单文件；独立 ORM/schema 文件属于标准
   水平层，删除独立 constants、helpers 和 facade 文件已经完成必要收敛。**反例**:
   把解析、状态机和导入编排合并成一个文件，
   会让纯函数、安全预算和数据库事务代码互相耦合。**回归**:
   `tests/modules/system/test_user_module_layout.py` 断言唯一入口集合。
3. **不提供旧包兼容层** — 本仓库内调用方可原子迁移，兼容层只会保留第二事实源并允许
   新代码继续使用旧路径。**反例**: 留下 `system/user/__init__.py` 或 `sys.modules` alias，
   架构测试无法阻止路径回流。**回归**:
   `tests/modules/system/test_user_module_layout.py::test_legacy_user_package_is_removed`。

## 6. 完成证据

- 架构契约：`tests/modules/system/test_user_module_layout.py`，3 项通过。
- Alembic 模型入口：`python -m alembic heads` 成功加载唯一 head。
- 静态质量：`ruff check .` 与 `ruff format --check .` 通过。
- 全量回归：2289 项通过，32 项既有 SQLAlchemy warning，无失败。
- CI 覆盖率：76.64%，高于 70% 门禁。
- 审查修复：发现并修正 `alembic/env.py` 遗留旧 ORM import；架构测试已扩展扫描
  `alembic/app/scripts/tests`，防止旧路径再次进入独立运行入口。
