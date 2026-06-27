# 开发规范（DEV-GUIDELINES）

> **主视角**：研发经理
> **受众**：所有写代码的人（含外部贡献者）
> **目的**：把「spec → 开发 → 测试 → 回写」的循环标准化；保证不同人写的代码风格一致、可被彼此接手。
>
> 详细测试规则见 [`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md)，架构边界见 [`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)。

---

## 1. 开发流程（Phase 0-4）

每个非 trivial 功能必须走完整 Phase 0-4。trivial 的判断见 §1.6。

### Phase 0: Brainstorm（脑暴）

**触发条件**：跨模块改动 / 涉及数据模型 / 涉及用户体验决策 / 不确定怎么做。

**输入**：用户故事（As a... I want... so that...）+ 约束清单。

**做法**：
- 用 `superpowers:brainstorming` skill 引导（可选）
- 列出所有 reasonable alternatives，逐条评估
- 输出 spec 大纲（章节列表）

**反例**：跳过脑暴直接写代码 → 中途发现要做的事比想象大 3 倍。

### Phase 1: 写 spec

**位置**：`docs/specs/YYYY-MM-DD-<feature>.md`（草稿） → 评审后挪到 `docs/<feature>.md`。

**必含章节**（详见 [`PRODUCT-GUIDELINES.md`](./PRODUCT-GUIDELINES.md) §spec 模板）：
- 目标 / 非目标
- 术语表（参考 APP-MARKETPLACE §0）
- 架构 / 数据模型 / 状态机 / API
- 决策记录（编号从 #1 开始）
- 待办块（`⚠️ Plan X gap`）

**标杆样本**：[`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)（4400+ 行，含 74 条决策记录）。

### Phase 2: 拆 plan

**位置**：`docs/plans/YYYY-MM-DD-<feature>.md`。

**工具**：`superpowers:writing-plans` skill。

**任务粒度**：每步 2-5 分钟可完成。每步含：
- 文件路径（Create / Modify / Test）
- 完整代码（**禁止**「TODO」「类似上面」等占位符）
- 验证命令（`pytest tests/...`）+ 期望结果

**TDD 节奏**：每任务遵循「写失败测试 → 跑测试见失败 → 最小实现 → 跑测试见通过 → commit」。

### Phase 3: TDD 实现

**两种模式**：
- **大功能**（>10 任务）：`superpowers:subagent-driven-development` —— 每任务派一个 subagent，主对话只做 review
- **小修**（<10 任务）：`superpowers:executing-plans` —— 主对话批量执行 + 检查点

**每个任务完成后必跑**：
- 后端：`ruff check . && ruff format .`
- 前端：`pnpm lint && pnpm typecheck`
- 全量测试：`pytest` 或 `pnpm test`

### Phase 4: Spec 回写

**触发条件**（满足任一即必须回写）：
- 建表 / 迁移 / 字段演化
- 状态机变化（新状态、状态转换规则）
- 权限 / 作用域模型
- API 契约（路径、响应、错误码、字段名）
- 并发兜底逻辑（如 install 的 IntegrityError 退化）
- 决策被推翻或补强

**回写动作**：
1. spec 新增决策记录（`N. **决策名** — 理由。**反例**: ...。**回归**: ...`）
2. 翻转 warning 块：`⚠️ Plan X gap` → `✅ Plan X 已完成（YYYY-MM-DD）`
3. 补 regression 测试到决策记录里（贴测试路径）

**反例**：测试通过就完事，spec 不更新 → 半年后无人知道为何这样实现。

### 1.6 何时可以跳过 Phase 0-2

| 改动 | 跳到 |
|---|---|
| 文档错别字 | 直接 Phase 3 |
| 单测补充 | 直接 Phase 3 |
| Bug fix（根因清晰） | 直接 Phase 3 + spec 决策 #N |
| 新 API endpoint（不涉数据模型） | Phase 1（简版）+ Phase 3 |
| 跨模块功能 | 必须 Phase 0-4 |
| 数据模型变更 | 必须 Phase 0-4 |
| 状态机变更 | 必须 Phase 0-4 |

**拿不准时**：从 Phase 0 开始。脑暴 30 分钟比返工 3 天便宜。

---

## 2. 后端代码规范

### 2.1 工具链

- Python >= 3.12（CLI 工具 >= 3.10）
- 依赖管理：`uv sync`（**禁用** `pip install` 直接装全局）
- Lint / Format：`ruff check . && ruff format .`（行宽 88）
- 测试：`pytest`（async 用 `pytest-asyncio`）
- 每次 code change 后必跑（见 CLAUDE.md）

### 2.2 模块结构

```
app/modules/<module>/
├── __init__.py
├── api/                    # FastAPI router，按资源拆文件
│   ├── __init__.py
│   └── <resource>.py
├── service/                # 业务逻辑
│   ├── __init__.py
│   ├── base.py             # (可选) 通用基类
│   └── <resource>_service.py
├── models/                 # SQLAlchemy 实体
│   ├── __init__.py
│   └── <resource>.py
└── schemas/                # Pydantic DTO
    ├── __init__.py
    └── <resource>.py
```

### 2.3 Service 层铁律

```python
class UserService(MarketplaceBaseService):
    async def create(self, db: AsyncSession, req: UserCreate) -> User:
        # ✅ 业务逻辑、领域异常、DB 查询
        # ❌ 禁止 await db.commit()
        ...
        return user


# 模块底部必加单例
user_service = UserService()
```

**例外**：`AuthService` 因涉及 session 状态管理，允许在内部 commit —— 但仅此一例。

### 2.4 异常处理

```python
# ✅ 正确：领域异常
from app.core.exceptions import NotFoundException

raise NotFoundException(f"User {user_id}")

# ❌ 错误：HTTPException
from fastapi import HTTPException
raise HTTPException(404, "User not found")
```

**error_code 必填**：
```python
exc = NotFoundException("User")
exc.error_code = "USER_NOT_FOUND"  # UPPER_SNAKE_CASE
raise exc
```

### 2.5 Pydantic Schema

```python
from pydantic import BaseModel, field_serializer
from pydantic.alias_generators import to_camel


class UserOut(BaseModel):
    model_config = {"alias_generator": to_camel, "populate_by_name": True}

    id: int
    user_name: str

    @field_serializer("id")
    def serialize_id(self, v: int) -> str:
        return str(v)  # 防 JS BigInt 精度丢失
```

### 2.6 SQLAlchemy Model

```python
from sqlalchemy.orm import Mapped, mapped_column
from app.core.id_generator import next_id
from app.db.base import Base


class User(Base):
    __tablename__ = "sys_user"

    user_id: Mapped[int] = mapped_column(primary_key=True, default=next_id)
    user_name: Mapped[str] = mapped_column(String(50))
    # ❌ 不要用旧式 Column(...)
```

### 2.7 分页

```python
from app.utils.pagination import paginate, build_filters, QueryParams


class UserQuery(QueryParams):  # 默认带 current / size
    user_name: str | None = None


async def list_users(db: AsyncSession, query: UserQuery) -> PageResult:
    filters = build_filters(User, {"user_name": "contains"}, user_name=query.user_name)
    return await paginate(db, User, query, filters=filters)
```

---

## 3. 前端代码规范（hohu-admin-web）

### 3.1 工具链

- Node >= 20
- 包管理：`pnpm install`（**禁用** npm / yarn）
- Lint：`pnpm lint`
- Type check：`pnpm typecheck`
- 路由：`@elegant-router`（**不需要手动** `pnpm gen-route`）

### 3.2 目录约定

详见 [`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md) §3。

### 3.3 命名

- 组件：`PascalCase`（`ReviewDetailDrawer.vue`）
- 变量 / 函数：`camelCase`
- 常量：`UPPER_SNAKE_CASE`
- 类型：`PascalCase`（`Review`、`App`）
- data-testid：`kebab-case` + 模块前缀（`review-detail-drawer`、`review-detail-approve`）

### 3.4 i18n

```typescript
// ✅ 正确
import { $t } from '@/locales';
$t('page.marketplace.review.title')

// ❌ 错误：硬编码
'审核详情'
```

i18n 文件：`src/locales/langs/zh-cn.json` / `en-us.json`。

### 3.5 API 调用

```typescript
// service/api/<module>.ts
import { request } from '@/service/request';

export function fetchReviewDetail(id: string) {
  return request<Review.Detail>({ url: `/marketplace/review/${id}`, method: 'get' });
}
```

返回类型放 `typings/api/<module>.d.ts`，禁止 inline `any`。

---

## 4. 移动端规范（hohu-admin-app）

### 4.1 框架约束

- uni-app（`@dcloudio`）：必须兼容 H5 / 微信小程序 / App 三端
- 组件库：wot-design-uni
- 网络：alova

### 4.2 平台差异

用条件编译处理差异，不要写「H5 用 A，其他平台用 B」的运行时分支：

```vue
<!-- #ifdef H5 -->
<WebOnlyComponent />
<!-- #endif -->
<!-- #ifdef MP-WEIXIN -->
<WxOnlyComponent />
<!-- #endif -->
```

### 4.3 i18n

同前端，必经 `$t()`。

---

## 5. 桌面端规范（hohu-admin-desktop）

### 5.1 进程隔离

- main 进程：Node.js + Electron API
- preload：暴露受限 IPC 给 renderer
- renderer：浏览器环境（**Node 集成默认关闭**）

跨进程通信走 `ipcRenderer.invoke(channel, ...args)` + `ipcMain.handle(channel, handler)`。

### 5.2 安全

- renderer **禁止** 直接 `require` Node 模块
- 所有 Node 操作走 preload 暴露的白名单 API
- 外部链接用 `shell.openExternal(url)`，禁止 `window.open`

---

## 6. CLI 规范（hohu-cli）

### 6.1 输出

```python
# ✅ 正确：rich.console
from rich.console import Console
console = Console()
console.print("[green]✓[/green] 安装成功")

# ❌ 错误：print
print("安装成功")  # 禁止
```

### 6.2 i18n

```python
from hohu.i18n import i18n

console.print(i18n.t("install.success"))
```

翻译文件：`hohu-cli/hohu/locales/{en,zh}.json`。新增 key 必须同时更新两个文件。

---

## 7. Git 工作流

### 7.1 分支命名

```
main                            # 生产
feature/<short-name>            # 新功能
fix/<short-name>                # bug fix
chore/<short-name>              # 配置 / 依赖升级
release/v<X.Y.Z>                # 发布准备
```

**例**：`feature/lowcode-engine`、`fix/install-apply-upgrade`、`chore/upgrade-fastapi`。

### 7.2 Commit 规范

**格式**：一句话英文，无 `Co-Authored-By`。

```bash
# ✅ 正确
git commit -m "fix(marketplace): reinstall routes through apply_upgrade for v1->v2 schema evolution"

# ❌ 错误
git commit -m "fix"                              # 太空
git commit -m "修复重装问题"                       # 中文（commit msg 用英文）
git commit -m "..." --trailer "Co-Authored-By: Claude ..."  # 禁用
```

**type 前缀**（推荐）：
- `feat:` 新功能
- `fix:` bug 修复
- `refactor:` 重构
- `test:` 测试
- `docs:` 文档
- `chore:` 杂项
- `perf:` 性能

### 7.3 DCO（Developer Certificate of Origin）

每个 commit 必含 `Signed-off-by`：

```bash
git commit -s -m "..."
# 自动加：Signed-off-by: Your Name <your.email@example.com>
```

外部贡献者 PR 缺 `Signed-off-by` 直接 block。详见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)。

### 7.4 Stage 策略

```bash
# ✅ 正确：按文件名
git add app/modules/marketplace/service/install_service.py
git add tests/modules/marketplace/test_install_service_lowcode.py

# ❌ 错误：git add -A 会扫进 .env / 编译产物
git add -A
```

### 7.5 不允许的操作

- `--no-verify`（除非用户明确要求）
- `--no-gpg-sign` / `-c commit.gpgsign=false`
- `git reset --hard` 已 push 的分支
- `git push --force` 到 main / master
- `git rebase -i`（interactive，无法在 CI 复现）
- amend 已 push 的 commit（开新 commit 修复）

### 7.6 合并策略

- 默认：**squash merge**（PR 多 commit 合为一个）
- 里程碑（如大版本发布）：**merge commit**（保留完整历史）

---

## 8. 代码评审 Checklist

PR review 时逐项打勾：

### 8.1 功能正确性

- [ ] 实现符合 spec 描述
- [ ] 边界条件覆盖（空值 / 0 / 超长 / 越界）
- [ ] 异常路径覆盖（DB 错误 / 网络错 / 权限拒绝）
- [ ] 并发场景考虑（双请求 / 竞态 / 死锁）
- [ ] 权限校验到位（`require_permissions` / scope）

### 8.2 代码质量

- [ ] 分层正确（Service 不 commit、API 不写业务逻辑）
- [ ] 异常用领域层级（无 HTTPException）
- [ ] 命名清晰（无需注释就能懂）
- [ ] 无半完成实现（`TODO` 必须配触发条件 + 责任人）
- [ ] 无过早抽象（三相似行不构成抽象）

### 8.3 性能

- [ ] 无 N+1 查询（用 `selectinload`）
- [ ] 大表查询有索引 / 分页
- [ ] 无同步阻塞 async IO（`time.sleep` / 同步 http client）

### 8.4 安全

- [ ] 输入校验（Pydantic 边界 / 长度 / 类型）
- [ ] 无 SQL 注入（参数化，禁 f-string）
- [ ] 无 XSS（前端 v-html 慎用）
- [ ] 敏感字段脱敏（MaskUtil）

### 8.5 文档同步

- [ ] spec 已回写（决策记录 / warning 块翻转）
- [ ] OpenAPI 自动生成的文档可读
- [ ] 用户手册（如适用）已更新
- [ ] CHANGELOG（如适用）已加条目

---

## 9. 依赖管理

### 9.1 安装

- 后端：`uv sync`（基于 `pyproject.toml` + `uv.lock`）
- 前端 / 移动端 / 桌面端 / 文档：`pnpm install`（基于 `package.json` + `pnpm-lock.yaml`）
- CLI：`uv sync`

### 9.2 升级

- 单独 PR 升级（便于回滚）
- 升级前看 changelog / migration guide
- 升级后跑全量测试 + 手测关键路径
- major 版本升级必写 ADR

### 9.3 安全漏洞

```bash
# 后端
pip audit

# 前端
pnpm audit
```

每周跑一次（可配 CI cron）。Critical 漏洞 24h 内修，High 7d 内修。

---

## 10. 调试与日志

### 10.1 后端日志

```python
import logging
logger = logging.getLogger(__name__)

logger.info("User %s installed app %s", user_id, app_slug)  # ✅ 懒求值
logger.error("Install failed", exc_info=True)
```

**禁止** `print()`。**禁止** f-string（用 `%s` 懒求值，避免无 bug 时也拼字符串）。

### 10.2 前端日志

```typescript
// ✅ dev 环境
import { isDev } from '@/utils';
if (isDev) console.log('debug:', data);

// ❌ 上线必删
console.log(data);
```

### 10.3 审计日志

关键操作留审计：install/uninstall/权限变更/角色分配/敏感数据导出。

审计日志 append-only，禁修改，保留期合规驱动（详见 [`SECURITY.md`](./SECURITY.md)）。

---

## 11. 反模式（Don't）

| 反模式 | 正解 |
|---|---|
| 半完成实现（`# 留给下个 PR`） | 拆 PR，每个 PR 完整可测 |
| 过早抽象 | 三相似行不抽象，五相似行再抽 |
| 注释解释 WHAT（"这是个循环"） | 删注释，让命名自解释 |
| 注释引用具体 PR / issue（"added for #123"） | 写在 PR 描述里，不进代码 |
| 写测试为了覆盖率（无意义的 `assert True`） | 测真实行为，宁可覆盖率不达标 |
| 用 `git add -A` | 按文件 stage |
| amend 已 push 的 commit | 开新 commit |
| 跳过 lint / typecheck | 必须跑绿才能 commit |
| commit msg 写中文 | 用英文（便于国际贡献者） |
| `console.log` 上线 | 用 dev 条件包裹 |

---

## 12. 参考

- 架构边界：[`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)
- 测试规则：[`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md)
- 安全要求：[`SECURITY.md`](./SECURITY.md)
- 标杆 spec：[`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)
- 各子项目 CLAUDE.md（顶层 [`../../CLAUDE.md`](../../CLAUDE.md) 索引）
