# 发布规范（RELEASE）

> **主视角**：CTO / 运维
> **受众**：maintainer / 发布管理员
> **目的**：把版本号、发布流程、CI/CD、迁移、回滚标准化，让每次发布都可预测、可回滚、可审计。

---

## 1. 版本号（SemVer）

采用 [Semantic Versioning 2.0.0](https://semver.org/)：

```
MAJOR.MINOR.PATCH
   1     . 2    . 3
```

| 版本位 | 何时 bump | 例 |
|---|---|---|
| **MAJOR** | 不兼容的 API 变更 | v0.x → v1.0 |
| **MINOR** | 向后兼容的新功能 | v1.2 → v1.3 |
| **PATCH** | 向后兼容的 bug fix | v1.2.3 → v1.2.4 |

### 1.1 预发布版本

```
1.0.0-alpha.1     # 早期预览，功能可能不完整
1.0.0-beta.1      # 功能完整，可能不稳定
1.0.0-rc.1        # release candidate，预期即最终版
1.0.0             # 正式发布
```

### 1.2 各子项目独立版本

hohu 是 monorepo（多 git 仓库），**每个子项目独立版本号**：

| 子项目 | 当前版本位置 | 发布到 |
|---|---|---|
| hohu-admin | `pyproject.toml` `[project] version` | PyPI |
| hohu-admin-web | `package.json` `version` | npm |
| hohu-admin-app | `package.json` `version` | （暂不发布） |
| hohu-admin-docs | `package.json` `version` | VitePress 站点 |
| hohu-admin-desktop | `package.json` `version` | GitHub Release |
| hohu-cli | `pyproject.toml` `[project] version` | PyPI |

### 1.3 0.x 期约定

主版本号 0 期间（0.y.z），MINOR 位 bump 即允许 breaking change：
- `0.1.0 → 0.2.0` 可以有 breaking change
- `0.1.0 → 0.1.1` 必须 100% 兼容

1.0 正式版后严格遵守 SemVer。

---

## 2. 发布流程

### 2.1 整体流程

```
1. 准备 release branch
2. 跑完整 CI（含 PG + Redis + alembic + pytest）
3. 更新 CHANGELOG.md
4. bump 版本号
5. 打 tag
6. 推 tag → 触发 publish workflow
7. 创建 GitHub Release（带 notes）
8. 通知用户（社群 / announcement）
```

### 2.2 Release Branch

小版本（PATCH）可直接在 `main` 上 tag，不必 release branch。

大版本（MINOR / MAJOR）开 release branch：

```bash
git checkout main
git pull
git checkout -b release/v1.2.0

# 仅做版本号 / CHANGELOG / 文档相关改动，不接 feature
# 必要时 cherry-pick main 上的 critical fix
```

### 2.3 CHANGELOG

每个子项目的 `CHANGELOG.md` 按版本倒序列出改动。格式参考 [Keep a Changelog](https://keepachangelog.com/)：

```markdown
# Changelog

## [1.2.0] - 2026-06-26

### Added
- 应用市场支持多 model 模式（每个 model 独立建表）
- ...

### Changed
- `_create_app_tables` 改走 `apply_upgrade`，支持 v1→v2 schema 演化
- ...

### Fixed
- 重装时新字段未通过 ALTER TABLE 添加的 bug（决策 #74）
- ...

### Deprecated
- `app_data_<slug>` 单表模式将于 v2.0 移除，迁移到 `app_data_<slug>_<model>`
- ...

### Removed
- 移除已废弃的 `MODULE-CORE` API（v0.x 中已 deprecate）
- ...

### Security
- 修复 tenant_app 越权访问漏洞（CVE-XXX）
- ...

### Contributors
@zhangsan, @lisi, @claude
```

**自动化**：可用 [changesets-action](https://github.com/changesets/action) 或 [release-please](https://github.com/googleapis/release-please) 半自动生成。

### 2.4 版本号 bump

#### 后端（Python）

```bash
# pyproject.toml
[project]
version = "1.2.0"   # 手动改

# 或用 bump-my-version
bump-my-version bump minor
```

#### 前端（npm）

```bash
pnpm version minor -m "chore: release v%s"
# 自动改 package.json + commit + tag
```

### 2.5 打 Tag

```bash
git tag -a v1.2.0 -m "Release v1.2.0"
git push origin v1.2.0
```

**命名约定**：`v<MAJOR>.<MINOR>.<PATCH>`（带 `v` 前缀）。

### 2.6 GitHub Release

```markdown
# Release v1.2.0

## Highlights

<3-5 条核心改动，附截图/GIF。>

## 🚀 New Features

- **多 model 模式**：一个应用可声明多个独立 model，每个 model 一张表。详见 [docs](...).

## 🐛 Bug Fixes

- 修复 v1→v2 重装时 schema 演化不生效的问题（#1234）

## ⚠️ Breaking Changes

无（本版本兼容 v1.1.x）。

## 📦 Install / Upgrade

```bash
uv sync                    # 后端
pnpm install               # 前端
alembic upgrade head       # 迁移
```

## 🙏 Contributors

@zhangsan, @lisi

**Full Changelog**: https://github.com/aihohu/hohu-admin/compare/v1.1.0...v1.2.0
```

---

## 3. CI/CD

### 3.1 PR 阶段 CI

每次 PR 触发，必绿才能合并：

- `ruff check . && ruff format --check .`
- `pytest --cov-fail-under=70`
- `pnpm lint && pnpm typecheck`
- `pnpm test:unit --coverage`
- `alembic upgrade head`（PG service container）

详见 [`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md) §8。

### 3.2 Publish Workflow

tag 推送时触发：

```yaml
# .github/workflows/publish.yml
on:
  push:
    tags: ['v*']

jobs:
  publish-backend:
    runs-on: ubuntu-latest
    services: { postgres: ..., redis: ... }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install uv && uv sync
      - run: pytest --cov-fail-under=70
      - run: uv build
      - run: uv publish --token ${{ secrets.PYPI_TOKEN }}

  publish-web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v3
      - uses: actions/setup-node@v4
      - run: cd hohu-admin-web && pnpm install && pnpm build
      - run: pnpm publish --no-git-checks
        env: { NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }} }
```

### 3.3 镜像构建（如适用）

Docker 镜像发布到 GHCR：

```bash
docker build -t ghcr.io/aihohu/hohu-admin:v1.2.0 .
docker push ghcr.io/aihohu/hohu-admin:v1.2.0

# latest tag 同步
docker tag ghcr.io/aihohu/hohu-admin:v1.2.0 ghcr.io/aihohu/hohu-admin:latest
docker push ghcr.io/aihohu/hohu-admin:latest
```

---

## 4. 数据库迁移

### 4.1 迁移文件

```bash
alembic revision --autogenerate -m "add mk_app_version table"
alembic upgrade head
```

### 4.2 CI 验证

每次 PR 必须验证：
- `alembic upgrade head`：从空库开始能干净升级
- `alembic downgrade -1`：单步回滚（仅安全迁移）
- `alembic upgrade head`：再次升级（验证幂等）

### 4.3 生产迁移

```bash
# 1. 备份
pg_dump -h prod-db -U postgres hohu > backup_$(date +%Y%m%d).sql

# 2. 滚动升级（推荐：先停一半实例，迁移，再切流）
kubectl scale deployment hohu-admin --replicas=2  # 从 4 减到 2

# 3. 跑迁移（在维护窗口，避免长事务锁）
alembic upgrade head

# 4. 升级剩余实例
kubectl set image deployment/hohu-admin hohu-admin=ghcr.io/aihohu/hohu-admin:v1.2.0
kubectl scale deployment hohu-admin --replicas=4

# 5. 观察 30 分钟
kubectl logs -f deployment/hohu-admin --tail=100
```

### 4.4 Breaking migration

涉及数据转换或不可逆操作的迁移需要 deprecation 周期：

```
v1.0: 新加列 nullable + default
v1.1: 应用层填充新列
v1.2: 加约束 NOT NULL（数据已填齐）
v1.3: 删除旧列（deprecated 一个版本）
```

详见 [`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md) §6.3。

---

## 5. 回滚

### 5.1 代码回滚

```bash
# GitHub Release 页面找到上一个稳定版
git revert <bad-commit-sha>    # 单次回滚
git checkout v1.1.0            # 整版回滚（更稳）

# 或在 k8s：
kubectl rollout undo deployment/hohu-admin
```

### 5.2 数据库回滚

```bash
# 安全迁移（加列、加索引）可 downgrade
alembic downgrade -1

# 危险迁移（删列、改类型）→ 不可逆，必须从备份恢复
psql -h prod-db -U postgres hohu < backup_20260626.sql
```

**铁律**：删列 / 改类型迁移发布前必须备份。alembic 的 `downgrade()` 仅对安全迁移生效。

### 5.3 配置回滚

通过 feature flag（环境变量）控制：

```bash
# .env
ENABLE_LOWCODE_ENGINE=false  # 出问题时一键关闭
```

新功能 default 必须有 feature flag，发布后 1-2 个版本再移除。

---

## 6. 监控与告警

### 6.1 关键指标

| 指标 | 告警阈值 |
|---|---|
| 错误率 | > 1% / 5min |
| P99 延迟 | > 500ms |
| 5xx 错误 | > 10 / min |
| DB 连接池占用 | > 80% |
| Redis 内存 | > 80% |
| Disk 使用 | > 85% |

### 6.2 业务指标

| 指标 | 含义 |
|---|---|
| 日活租户 | 多少租户每天用 |
| install / uninstall 比例 | > 5:1 健康，< 2:1 需排查 |
| 应用市场审核通过率 | < 50% 需审核标准 |
| MCP 工具调用 P99 | 反映 AI 性能 |

### 6.3 Oncall

- **P0**（线上故障）：oncall 30 分钟内响应
- **P1**（严重 bug）：当天响应
- 告警渠道：Slack / 飞书 / 钉钉（择一）
- Postmortem：P0 / P1 事故必写（5 whys + 改进项）

---

## 7. 发布检查清单

发布前逐项打勾：

### 7.1 代码

- [ ] 所有 plan issue 已 close 或推迟（推迟的标 milestone）
- [ ] CHANGELOG.md 已更新
- [ ] 版本号已 bump（pyproject.toml / package.json）
- [ ] CI 全绿（含覆盖率）
- [ ] 全量测试通过

### 7.2 数据库

- [ ] 新迁移已在 staging 跑过
- [ ] 生产备份脚本已准备
- [ ] 回滚 SQL 已准备（对危险迁移）

### 7.3 文档

- [ ] spec 决策已回写
- [ ] 用户手册已更新（VitePress）
- [ ] API 文档（OpenAPI 自动生成）
- [ ] 升级指南（如有 breaking change）

### 7.4 通讯

- [ ] GitHub Release notes 写好
- [ ] 社区公告（Discussions / 社群）
- [ ] Contributors 已致谢

### 7.5 监控

- [ ] 发布后 30 分钟观察关键指标
- [ ] 发布后 24 小时观察业务指标
- [ ] Oncall 待命

---

## 8. 反模式（Don't）

| 反模式 | 正解 |
|---|---|
| 周五下午发布 | 周二/周三上午，留时间观察 |
| 跳过 staging 直接 prod | 必经 staging |
| 发布不写 CHANGELOG | 每个版本必写 |
| 删列迁移无 deprecation 周期 | 至少 1 个 minor 版本 deprecate |
| Breaking change 不写 release notes 显著位置 | ⚠️ Breaking Changes 单独成节 |
| tag 命名 `1.2.0`（不带 v） | `v1.2.0`（带 v 前缀） |
| publish token 写进代码 | 用 GitHub Secrets |
| 发布后立刻关电脑 | 至少留 30 分钟观察 |

---

## 9. 参考

- CI 配置：[`.github/workflows/`](../.github/workflows/)
- 测试规范：[`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md)
- 架构演进：[`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md) §6
- 安全响应：[`SECURITY.md`](./SECURITY.md)
