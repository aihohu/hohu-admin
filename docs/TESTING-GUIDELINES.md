# 测试规范（TESTING-GUIDELINES）

> **主视角**：测试经理
> **受众**：所有写代码的人（含外部贡献者）
> **目的**：定义测试金字塔、隔离策略、覆盖率门禁，让「测试通过」意味着「功能真的可用」，而不只是「断言没失败」。
>
> 架构边界见 [`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)，开发流程见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md)。

---

## 1. 测试金字塔

```
        /\
       /E2E\         ~10%   关键用户路径，慢但真实
      /------\
     /Integra-\     ~20%   跨 service / DB / Redis
    /-tion    \
   /------------\
  /    Unit     \   ~70%   快、隔离、覆盖率高
 /----------------\
```

**反例**：倒金字塔（多数 E2E、少数 unit）→ 跑得慢、定位难、CI 拖延。

### 1.1 工具矩阵

| 子项目 | Unit | Integration | E2E |
|---|---|---|---|
| hohu-admin | pytest + pytest-asyncio | pytest + 真 PG + Redis | (暂无) |
| hohu-admin-web | vitest | @vue/test-utils | playwright |
| hohu-admin-app | vitest | @vue/test-utils | (H5 only) playwright |
| hohu-admin-desktop | vitest | electron-mocha | (手动) |
| hohu-cli | pytest | pytest + CliRunner | (无) |

---

## 2. 后端测试（pytest）

### 2.1 命名规则

```
tests/
├── conftest.py                          # 全局 fixture
├── modules/
│   └── marketplace/
│       ├── conftest.py                  # 模块级 fixture
│       ├── test_install_service.py
│       ├── test_install_service_lowcode.py
│       └── test_review_api.py
└── test_main.py
```

**文件命名**：`test_<被测对象>.py`。被测对象是 service / api / model 名。

**类命名**：按场景分组，`Test<场景>`：
```python
class TestInstallCreatesTables: ...
class TestUninstallDropsTables: ...
class TestReinstallSchemaEvolution: ...
```

**方法命名**：`test_<场景>_<预期行为>`：
```python
async def test_reinstall_adds_new_column_preserves_data(self): ...
async def test_install_with_hyphenated_slug_creates_table(self): ...
```

### 2.2 conftest.py fixture 模式

#### 全局 `db_session`（事务回滚零污染）

```python
# tests/conftest.py
@pytest_asyncio.fixture
async def db_session(db_engine):
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        async with session.begin():
            yield session
            await session.rollback()  # 测试结束自动回滚，零污染
```

**关键**：`session.begin()` + `rollback()` 保证每个测试对 DB 的修改都被还原。**禁止**测试里调 `db.commit()`（会被 rollback 兜底，但违反 Service 不 commit 铁律）。

#### 业务 fixture

```python
# tests/modules/marketplace/conftest.py
@pytest.fixture
async def lowcode_app_with_schema(db_session):
    """已发布的低代码应用，manifest 含 data_schema"""
    app = App(tenant_id=0, name="...", slug="lowcode_test_crm", ...)
    db_session.add(app)
    await db_session.flush()
    ...
    return app
```

**约定**：
- fixture 名描述「对象状态」（`lowcode_app_with_schema` 而非 `app1`）
- fixture 内不做断言
- fixture 间依赖显式（参数注入）

### 2.3 测试隔离

#### 残留清理

涉及手动建表（`app_data_*`）的测试，开头清理：

```python
async def test_xxx(self, db_session):
    table_name = "app_data_lowcode_test_crm"
    await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    ...
```

虽然 `db_session` 会回滚事务，但 `CREATE TABLE` 在某些 PG 配置下是 DDL（隐式 commit），所以显式 DROP 更稳。

#### 不依赖测试顺序

```python
# ❌ 错误：依赖 test_a 创建的数据
async def test_b(self, db_session):
    user = (await db_session.execute(select(User))).scalar_one()  # 假设有数据？

# ✅ 正确：自己 setup
async def test_b(self, db_session):
    user = User(...)
    db_session.add(user)
    await db_session.flush()
    ...
```

### 2.4 时间戳敏感场景

**坑**：`get_latest_approved` 按 `created_at DESC` 排序，但同事务内多条记录 `created_at` 可能完全相同 → 非确定性。

```python
# ❌ 错误：依赖 created_at DESC 排序
v1 = AppVersion(..., version="1.0.0")
v2 = AppVersion(..., version="2.0.0")
db_session.add_all([v1, v2])
await db_session.flush()
# get_latest_approved 可能返回 v1！

# ✅ 正确：显式指定 version
await install_service.install(
    db_session,
    InstallCreate(app_slug="...", version="2.0.0"),
    user_id=1,
)
```

**生产环境**通常不会触发（INSERT 间隔微秒级），但测试同事务 INSERT 会撞。

### 2.5 必覆盖类别

每个 service 方法 / API endpoint 必须覆盖：

| 类别 | 说明 |
|---|---|
| **正向** | happy path，业务正常流转 |
| **边界** | 空值 / 0 / 最大长度 / 分页第 0 页和最后一页 |
| **异常** | 资源不存在 / 权限不足 / 唯一约束冲突 |
| **并发** | 双请求并发，IntegrityError 退化路径 |
| **权限** | 跨租户不能访问 / 无权限码拒绝 |
| **回归** | 历史 bug 的复现测试（贴在 spec 决策 #N 里） |

**例**：install_service.install 的覆盖矩阵

```
test_install_creates_app_data_table              # 正向
test_install_creates_table_with_user_columns     # 正向 + 字段验证
test_install_without_data_schema_no_table        # 边界（无 schema）
test_install_creates_multiple_tables             # 正向（多 model）
test_install_with_hyphenated_slug_creates_table  # 回归（PG 语法 bug）
test_uninstall_drops_table_and_records_retained  # 正向（uninstall）
test_reinstall_adds_new_column_preserves_data    # 回归（v1→v2 演化）
test_reinstall_widens_varchar_via_alter          # 回归（widening）
test_concurrent_install_falls_back_to_update     # 并发
test_install_other_tenant_403                     # 权限
```

### 2.6 async 测试写法

```python
import pytest

@pytest.mark.asyncio
async def test_xxx(db_session):
    ...
```

或在 `pyproject.toml` 配 `asyncio_mode = "auto"`，省 `@pytest.mark.asyncio`。

### 2.7 跑测试

```bash
pytest                                       # 全量
pytest tests/modules/marketplace/            # 单模块
pytest tests/modules/marketplace/test_install_service_lowcode.py::TestReinstallSchemaEvolution::test_reinstall_adds_new_column_preserves_data
                                             # 单测试
pytest -k "reinstall"                        # 关键字筛选
pytest --cov=app --cov-report=term-missing   # 覆盖率
```

---

## 3. 前端测试（hohu-admin-web）

### 3.1 工具链

- vitest（unit）
- @vue/test-utils（组件）
- msw（API mock）
- playwright（E2E）

### 3.2 命名

```
src/views/marketplace-review/
├── index.vue
└── __tests__/
    └── review-detail-drawer.spec.ts
```

测试文件与组件同目录，命名 `<component>.spec.ts`。

### 3.3 组件测试

```typescript
import { mount } from '@vue/test-utils';
import ReviewDetailDrawer from '../review-detail-drawer.vue';
import { fetchReviewDetail } from '@/service/api/marketplace';

vi.mock('@/service/api/marketplace');

it('renders manifest from API', async () => {
  vi.mocked(fetchReviewDetail).mockResolvedValue({ data: { id: '1', manifest: {...} }, error: null });
  const wrapper = mount(ReviewDetailDrawer, { props: { visible: true, reviewId: '1' } });
  await flushPromises();
  expect(wrapper.find('[data-testid="review-detail-manifest"]').exists()).toBe(true);
});
```

### 3.4 data-testid 命名

- kebab-case
- 模块前缀：`review-detail-` / `app-card-` / `install-button-`
- 描述用途不描述实现：`review-detail-approve`（不是 `review-detail-green-button`）

### 3.5 E2E（playwright）

```typescript
// e2e/marketplace-review.spec.ts
test('approve a pending review', async ({ page }) => {
  await page.goto('/marketplace-review');
  await page.click('[data-testid="review-row-1"]');
  await page.click('[data-testid="review-detail-approve"]');
  await expect(page.locator('text=审核成功')).toBeVisible();
});
```

E2E 必须用 `data-testid`，禁用文本选择器（文本会随 i18n 变）。

---

## 4. 移动端测试（hohu-admin-app）

### 4.1 平台约束

- H5：可跑 vitest + playwright
- 微信小程序：仅 vitest（无 E2E）
- App：仅手动测

### 4.2 平台差异测试

```typescript
// #ifdef H5 的代码用条件编译隔离
// 单元测试只测纯 TS 逻辑（stores / utils），不测平台 API
```

---

## 5. 桌面端测试（hohu-admin-desktop）

### 5.1 main 进程

用 `electron-mocha` 测 main 进程逻辑（IPC handler / 文件操作）。

### 5.2 renderer 进程

走前端测试规范（vitest + @vue/test-utils）。

### 5.3 IPC 边界

集成测试覆盖 preload 暴露的 API：

```typescript
import { expect } from 'chai';
import { ipcRenderer } from 'electron';

it('preload exposes only allowlisted APIs', () => {
  expect(ipcRenderer).to.have.property('app');
  expect(ipcRenderer).to.not.have.property('shell');  // 禁止暴露
});
```

---

## 6. CLI 测试（hohu-cli）

```python
from typer.testing import CliRunner
from hohu.cli import app

runner = CliRunner()

def test_admin_create():
    result = runner.invoke(app, ["admin", "create", "--name", "test"])
    assert result.exit_code == 0
    assert "Created" in result.stdout
```

子进程测试用临时目录隔离：

```python
def test_init(tmp_path):
    os.chdir(tmp_path)
    runner.invoke(app, ["init"])
    assert (tmp_path / "hohu-admin").exists()
```

---

## 7. 覆盖率门禁

### 7.1 目标

| 范围 | 阈值 | 阻断 |
|---|---|---|
| 后端整体 | ≥ 70% lines | CI fail |
| 后端 `app/modules/` | ≥ 80% lines | CI warn |
| 前端整体 | ≥ 70% lines | CI fail |
| 前端 `src/views/` | ≥ 60% lines | CI warn |
| CLI | ≥ 70% lines | CI fail |

**理由**：70% 是行业「可发布」最低线，前端 views 因 UI 难测放宽到 60%，后端核心模块收紧到 80%。

### 7.2 配置

#### 后端 `pyproject.toml`

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["app"]
omit = ["app/main.py", "tests/*"]

[tool.coverage.report]
fail_under = 70
show_missing = true
```

#### 前端 `vitest.config.ts`

```typescript
test: {
  coverage: {
    provider: 'v8',
    thresholds: {
      lines: 70,
      branches: 60,
      functions: 70,
    },
  },
}
```

### 7.3 CI 阻断

GitHub Actions workflow 必含：

```yaml
- name: Test with coverage
  run: |
    pytest --cov=app --cov-report=xml --cov-fail-under=70
    # 前端
    pnpm test:unit -- --coverage --coverage.threshold.lines=70
```

覆盖率不达标 → CI fail → 阻塞 PR 合并。

### 7.4 排除项

允许低覆盖率的代码（在 `# pragma: no cover` 或 coverage 配置显式排除）：
- `if __name__ == "__main__":` 入口
- 不可达的防御性分支（`raise RuntimeError("unreachable")`）
- 第三方类型兼容垫片（`TYPE_CHECKING` 块）

**禁止**用 `# pragma: no cover` 跳过真实业务分支 —— 这是作弊。

---

## 8. CI 质量门禁

### 8.1 必绿检查项

| 子项目 | 检查 |
|---|---|
| hohu-admin | `ruff check .` + `ruff format --check .` + `pytest --cov-fail-under=70` |
| hohu-admin-web | `pnpm lint` + `pnpm typecheck` + `pnpm test:unit --coverage` |
| hohu-admin-app | `pnpm lint` + `pnpm type-check` + `pnpm test:unit` |
| hohu-admin-desktop | `pnpm lint` + `pnpm typecheck` + `pnpm test` |
| hohu-cli | `ruff check .` + `ruff format --check .` + `pytest --cov-fail-under=70` |

### 8.2 数据库迁移检查

后端 CI 必跑：

```bash
alembic upgrade head    # 迁移能干净执行
alembic downgrade -1    # 回滚也安全（仅安全迁移）
alembic upgrade head    # 再次 upgrade 验证幂等
```

### 8.3 服务容器

后端 CI 用 GitHub Actions service container 起 PG + Redis：

```yaml
services:
  postgres:
    image: postgres:15
    env:
      POSTGRES_PASSWORD: test
    ports: ['5432:5432']
  redis:
    image: redis:7
    ports: ['6379:6379']
```

### 8.4 失败阻塞

任何检查 fail → PR 不可合并（GitHub branch protection 强制）。

---

## 9. 性能与压测

### 9.1 关键 API

每个 release 跑一次 locust / k6 压测，关注：
- 列表 API（`GET /<module>` 分页）
- 高频写 API（如 install / uninstall）
- 跨表联查 API

### 9.2 慢查询监控

```sql
SELECT query, mean_exec_time, calls
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 20;
```

P99 > 100ms 的查询需优化。

---

## 10. 安全测试

详见 [`SECURITY.md`](./SECURITY.md)。

- 输入 fuzzing（关键 API）
- 依赖扫描（`pip audit` / `pnpm audit`）
- 静态扫描（ruff security rules / eslint-plugin-security）
- 越权检查（跨租户、跨用户）

---

## 11. 缺陷管理

### 11.1 Bug 流程

```
发现 bug → 开 issue（含复现步骤） → 修 bug → 加 regression test → 回写 spec 决策 #N → close
```

**关键**：每个 bug fix 必须有对应的 regression test。没有 test 的 fix 不算修完。

### 11.2 严重度

| 级别 | 含义 | SLA |
|---|---|---|
| P0 | 阻塞 / 数据丢失 / 安全漏洞 | 24h |
| P1 | 严重功能损坏 / 性能崩溃 | 7d |
| P2 | 一般 bug / 体验问题 | 当前 sprint |
| P3 | 美化 / 边角案例 | backlog |

---

## 12. 反模式（Don't）

| 反模式 | 正解 |
|---|---|
| 测试不隔离（共享数据） | `db_session` fixture + rollback |
| 测试依赖执行顺序 | 每个 test 自己 setup |
| mock 一切（测了 mock 不是代码） | 只 mock 边界（DB / 外部 API） |
| `assert True` 凑覆盖率 | 测真实行为 |
| 测试名 `test1` `test2` | `test_<场景>_<预期>` |
| 测试里调 `db.commit()` | 走 fixture 事务 |
| 依赖 `created_at DESC` 排序 | 显式指定 version / id |
| `# pragma: no cover` 跳业务分支 | 真的写测试覆盖 |
| E2E 用文本选择器 | 用 `data-testid` |
| Bug 修完不加 regression test | test + fix 同 PR |

---

## 13. 参考

- 开发流程：[`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md)
- 架构边界：[`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)
- 标杆测试：[`tests/modules/marketplace/test_install_service_lowcode.py`](../tests/modules/marketplace/test_install_service_lowcode.py)
- 标杆 spec：[`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)
