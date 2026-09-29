# 后端测试指南

测试代码放在 `tests/`，部署初始化放在 `scripts/`，运维工具放在 `tools/`。不要用生产数据库或共享开发数据库跑测试。

## 环境与命令

在后端仓库执行 `uv sync --all-extras --dev`。测试服务配置以 [.env.test](../.env.test) 和 [CI 工作流](../.github/workflows/ci.yml) 为参考；先确认数据库、Redis 地址及命名空间属于专用测试环境，再执行迁移和测试。`ENV=test` 本身不能把一个生产 DATABASE_URL 变安全。

```bash
uv run pytest tests/test_main.py
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=70
uv run python -m tools.checks.check_ai_tools
uv run python -m tools.checks.check_docs
```

## 回归设计

- 纯业务规则优先单元测试；SQL 查询、约束和事务行为使用真实 PostgreSQL 集成测试。
- 数据库测试复用事务回滚 fixture；自行建立的表、文件和 Redis 键由测试清理。不要假定执行顺序或依赖上个测试的数据。
- 排序断言使用明确的 ID、版本或业务排序键，不依赖恰好不同的创建时间。
- API 同时覆盖成功、参数拒绝、无权限、跨租户、禁用状态及异常回滚。
- 涉及撤权、幂等或并发时测试实际竞态；涉及迁移时验证空库、支持的升级边界和数据保留。
- Mock Provider 测试只能证明受控输入下的行为，不能代替真实 Provider、浏览器或 Docker 部署验证。

## CI 与发布验证

当前 CI 在 Python 3.12、3.13、3.14 上运行测试，服务使用 PostgreSQL 15 和 Redis 7，覆盖率门槛为 70%。具体命令以工作流为准；不要在手册维护易过期的“最新测试通过数量”。

hosted 发布验证由 [tests/release](../tests/release) 提供，使用专用临时环境和绑定构建 SHA 的证据。不得在生产数据库上试跑发布验证。PR 中注明执行的测试范围、结果以及尚未验证的部分。
