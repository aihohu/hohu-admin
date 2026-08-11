# AGENTS.md

本文件适用于 `hohu-admin/` 目录及其所有子目录。仓库根目录的 `../AGENTS.md` 仍然生效；如有冲突，以本文件中更具体的后端规则为准。

## 项目定位

`hohu-admin` 是 hohu 管理平台的 FastAPI 后端，使用 Python 3.12、PostgreSQL、SQLAlchemy 2.0 async、Pydantic v2、Alembic、Redis 和 pytest。

主要目录：

```text
app/
├── core/       # 配置、安全、领域异常等基础能力
├── db/         # 数据库连接、会话和基础模型
├── middleware/ # HTTP 中间件
├── modules/    # 业务模块：ai/auth/job/marketplace/system
├── schemas/    # 跨模块公共 Schema
├── tasks/      # 后台任务
└── utils/      # 无业务状态的通用工具
tests/          # pytest 测试，结构尽量与 app/ 对齐
docs/           # spec、计划、架构与治理文档
```

应用入口是 `app/main.py`。API 文档默认位于 `http://127.0.0.1:8000/docs`。

## 开始工作前

1. 先判断改动属于 trivial、bug fix、单模块功能还是跨模块功能。
2. 新功能、重构、数据模型变化或跨模块改动必须先创建或更新 `docs/<feature>.md`；草案放在 `docs/specs/YYYY-MM-DD-<feature>.md`。
3. 影响多个阶段的实现计划放在 `docs/plans/YYYY-MM-DD-<feature>.md`。
4. 开始实现前阅读与改动相关的既有 spec；架构、安全、测试细则分别见 `docs/ARCHITECTURE-GUIDELINES.md`、`docs/SECURITY.md`、`docs/TESTING-GUIDELINES.md`。
5. 不要顺手修改任务无关文件，不要覆盖用户已有改动。

文档拼写修正和纯测试补充可以直接实现。根因明确的 bug fix 可以直接进入 TDD，但仍需在相关 spec 中补决策与回归记录。数据模型、状态机和跨模块功能不得跳过 spec。

## 开发命令

在本目录执行：

```bash
uv sync
fastapi dev app/main.py
alembic revision --autogenerate -m "description"
alembic upgrade head
ruff check .
ruff format .
pytest
pytest tests/path/to/test_file.py
python scripts/init_db.py
```

使用 `uv` 管理依赖，不要向全局 Python 环境直接 `pip install`。Ruff 行宽为 88。

## TDD 与交付流程

每个行为变化遵循：

1. 先写能复现目标行为或缺陷的失败测试。
2. 运行最小测试范围，确认失败原因符合预期。
3. 写最小实现使测试通过，避免同时进行无关重构。
4. 运行 `ruff check . && ruff format .`。
5. 运行相关测试，再运行全量 `pytest`。
6. 覆盖率不得低于 70%。
7. 回写 spec，将 `⚠️ Plan X gap` 更新为 `✅ Plan X 已完成（YYYY-MM-DD）`，并补充决策与回归测试路径。

决策记录格式：

```text
N. **决策名** — 理由。**反例**: ...。**回归**: tests/path/to/test_x.py。
```

以下改动必须回写 spec：建表、迁移、字段演化、状态机、权限作用域、API 路径/响应/错误码，以及并发兜底逻辑。

## 模块与分层

业务代码遵循 `API -> Service -> Model`：

- API 层负责 HTTP 入参/出参、权限依赖、调用 Service，并在成功后执行 `await db.commit()`。
- Service 层负责业务规则、查询和领域异常，绝不调用 `commit()`；需要原子写入时由 API 层控制事务边界。
- Model 使用 SQLAlchemy 2.0 `Mapped[T]`，主键默认使用 `default=next_id`。
- Schema 使用 Pydantic v2、`alias_generator=to_camel` 和 `populate_by_name=True`；Snowflake/BigInteger ID 必须序列化为字符串。
- Service 在模块底部提供模块级单例，例如 `user_service = UserService()`，不要在每个请求中重复实例化。
- `get_current_user` 位于 `app/modules/auth/service.py`，不要从 `app/core/auth.py` 假设或复制实现。
- 模块间只能调用对方 Service，禁止跨模块直接导入 Model 并查询表。
- 保持依赖单向，禁止循环依赖。

典型模块结构优先保持为：

```text
app/modules/<module>/
├── api/
├── models/
├── schemas/
├── service/
└── __init__.py
```

修改现有模块时遵循该模块已经采用的布局，不要仅为统一目录结构进行大规模搬迁。

## API 契约

所有接口必须保持前后端共享契约：

- 响应包装：`{code, msg, data}`，成功业务码为 `200`。
- 分页数据：`data: {records, total, current, size}`。
- 认证：`Authorization: Bearer <jwt>`，JWT 使用 HS256，默认有效期 7 天。
- 后端字段使用 `snake_case`；通过 Pydantic alias 对外输出 `camelCase`。
- Snowflake ID 在 JSON 中必须是字符串，不能输出为 JavaScript number。
- 新建或已完成迁移的时间列使用 `TIMESTAMP WITH TIME ZONE`，应用内部使用 UTC，API 输出 ISO 8601 UTC。
- 部分遗留 `sys_*` 表仍使用 `TIMESTAMP WITHOUT TIME ZONE`；仅对这些列的范围查询使用 `app.schemas.types.LocalNaiveDatetime` 兼容本地 naive datetime，不得把该兼容方案扩散到新表。
- 新增或修改接口时，以 FastAPI OpenAPI 为契约事实源，并同步相关 spec 和前端类型。

业务代码禁止抛出原生 `HTTPException`。使用 `app/core/exceptions.py` 中的领域异常，如 `NotFoundException`、`DuplicateException`、`AuthenticationException`、`AuthorizationException`、`BusinessRuleException` 和 `InvalidParameterException`。新增业务错误需提供稳定的 `UPPER_SNAKE_CASE` `error_code`。

## 数据库与迁移

- Service 不提交事务；测试也不要通过隐式 commit 规避事务设计。
- 系统表 schema 变化使用 Alembic migration。
- 新业务表和字段演化按相关 spec 选择 `MigrationRunner.create_table` 或 `apply_upgrade`；不能用 `CREATE TABLE IF NOT EXISTS` 代替字段演化。
- 所有 SQL 使用 ORM 或参数化 `text()`；禁止 raw SQL f-string。
- DDL 标识符无法参数化时，必须先通过严格白名单或正则校验。
- 任何新增业务数据表必须评估 `tenant_id`、索引、唯一约束、UTC 时间列和删除策略。
- 新时间列使用 `DateTime(timezone=True)`/`TIMESTAMPTZ`。查询遗留 naive 时间列时复用 `LocalNaiveDatetime`，不要在各模块重复编写转换 validator。
- 不依赖 `created_at DESC` 推断版本顺序；使用明确的 version 或 ID。

## 多租户、权限与安全

- 每个业务查询都必须明确 `tenant_id` scope；即使当前阶段固定 `tenant_id=0` 也不能省略隔离设计。
- 优先复用模块已有的 `scoped()`、权限依赖和数据权限机制。
- 新 API 必须检查直接 ID、查询参数和外键关系是否可造成水平越权。
- 管理员跨租户操作必须有显式 super-admin 限制并写审计日志。
- 外部输入必须经过 Pydantic 校验；不得直接读取并信任 raw body。
- API 响应和日志不得泄露密码、token、密钥或未脱敏 PII。
- 第三方请求必须设置 timeout、限制重试并防 SSRF。
- 上传必须校验扩展名、MIME、magic bytes 和大小，使用安全生成的文件名并防 path traversal/zip slip。
- 禁止硬编码密钥；从环境变量或安全密钥存储读取。
- 禁止 `subprocess(..., shell=True)` 处理外部输入。

权限码格式为 `<module>:<resource>:<action>`。新增权限必须同步菜单/权限数据、角色映射和“无权限拒绝”测试。

## 测试约定

- 异步测试使用 pytest/pytest-asyncio 现有 fixture。
- 数据库测试使用 `db_session` fixture 的事务回滚，确保零污染。
- 每个测试负责清理无法被事务覆盖的 DDL 或外部状态；动态表先 `DROP TABLE IF EXISTS`。
- 测试不得依赖执行顺序、现有开发库数据或前一个测试产生的数据。
- 覆盖成功、领域失败、权限拒绝、租户隔离和并发/幂等边界。
- 修复 bug 时必须增加能在修复前失败的回归测试。
- 优先把测试放在与实现模块对应的 `tests/modules/`、`tests/core/`、`tests/utils/` 等目录。

## 禁止事项

- Service 层 `commit()`。
- 业务代码抛 `HTTPException`。
- 跨模块直接查询对方 Model。
- Snowflake ID 以 JSON number 输出。
- naive datetime 或数据库无时区时间列。
- raw SQL f-string。
- 未带 `tenant_id` scope 的业务查询。
- 返回或记录敏感字段。
- 通过修改生成文件、跳过测试或降低断言来掩盖失败。

## Commit

只有用户明确要求时才提交。提交时：

- 按文件名精确 stage，不使用 `git add -A`。
- commit message 使用一句英文。
- 使用 `git commit -s` 添加 DCO `Signed-off-by`。
- 不添加 `Co-Authored-By`，不使用 `--no-verify`。
- 不 amend 已 push 的 commit。
