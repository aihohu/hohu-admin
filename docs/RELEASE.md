# 发布与升级维护

本文面向维护者。用户安装、部署与升级通过 hohu-cli；底层迁移说明见 [数据库迁移](DATABASE-MIGRATIONS.md)。

## 版本与产物

后端版本维护在 [pyproject.toml](../pyproject.toml)，变更说明维护在 [CHANGELOG.md](../CHANGELOG.md)。`Unreleased` 是待发布内容，不等于发布记录。各子项目独立版本，但跨项目契约变更必须给出配套版本及升级顺序。

当前 [release.yml](../.github/workflows/release.yml) 在 GitHub Release 发布时运行：先执行 hosted 发布验证，通过后构建并推送 GHCR 的 linux/amd64、linux/arm64 镜像。不要把本地能构建 wheel 或前端包等同于已配置 PyPI/npm 自动发布。

## 发布前验证

1. 明确待发布提交、版本号、Release 说明和配套 CLI/客户端。
2. 通过 [CI](../.github/workflows/ci.yml)：Ruff、测试、至少 70% 覆盖率、hosted 隔离验证；通过文档检查。
3. 对迁移验证空库安装、支持的旧版本升级及关键数据保留。数据库安全触发器也属于验收范围。
4. 在专用环境验证实际部署、初始化重复运行、登录、关键业务、上传和权限拒绝。真实 Provider 或浏览器未验证时如实列明。
5. 准备数据库和持久文件备份、恢复演练、旧构建与回滚窗口。
6. 复核源码、文档和构建产物，排除本地草案、凭据、业务数据及临时附件。

发布验证代码位于 [tests/release](../tests/release)。报告绑定构建 SHA，CI 和 release profile 的证据作为工作流产物保存；不能复用其他提交的通过记录，也不能在生产数据库运行这些测试。

## 升级与恢复

升级前确认当前迁移版本和备份可用，暂停可能冲突的写入，再由 CLI 编排迁移、幂等种子及应用启动。迁移或种子失败应停止，不使用 `stamp head` 隐藏失败。

代码回滚不自动恢复数据库或私有文件。破坏性 downgrade 不能代替数据恢复；采用验证过的备份和匹配应用版本恢复，并在恢复后核查认证、租户状态、审计和后台任务。

生产 hosted 配置使用准确的 `RELEASE_BUILD_SHA`。监控、凭据、外部代理和备份恢复属于部署方的运行职责，工作流通过不表示这些环境条件已经完成。

## Gitee 源码镜像

[sync-gitee.yml](../.github/workflows/sync-gitee.yml) 将官方 GitHub 仓库的全部分支、标签和提交历史单向同步到 Gitee，跟随强推和删除。GitHub 为唯一维护入口，Gitee 仅供 clone/pull。支持事件触发、手动运行和每日补偿；不包含 Git LFS 对象、Release 附件和平台设置。

在 GitHub Settings → Secrets and variables → Actions 配置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Secret | `GITEE_SSH_PRIVATE_KEY` | 专用同步账号的完整 SSH 私钥，无口令 |
| Variable | `GITEE_REPOSITORY` | `<namespace>/hohu-admin` |
| Variable | `GITEE_KNOWN_HOSTS` | 已核验的 `gitee.com` SSH 主机公钥记录 |
| Variable | `GITEE_MIRROR_ENABLED` | 配置完成后设为 `true`，默认关闭 |

对应公钥添加到有目标仓库写权限的 Gitee 账号的**账户 SSH 公钥**。工作流发布到 GitHub 默认分支后，手动运行 **Sync Gitee mirror**，确认两端引用 SHA 一致并实际 clone。失败时查看 Actions 日志，修正配置后重跑；停用时将启用变量设为 `false`，正在运行的任务需另行取消。

实现见 [sync_git_mirror.py](../tools/ops/sync_git_mirror.py)，隔离 Git 回归见 [test_sync_git_mirror.py](../tests/tools/test_sync_git_mirror.py)。

状态：✅ Plan mirror-code 已完成（2026-10-05）；⚠️ Plan mirror-live gap — 维护者配置密钥、工作流发布及线上 clone 验收待完成。

1. **单向且限定引用范围** — 保持 GitHub 为事实来源，只同步 heads/tags。**反例**: 双向写入或镜像平台内部引用。**回归**: `tests/tools/test_sync_git_mirror.py` 的强推、删除及额外引用排除测试。
2. **默认关闭、串行同步并核验快照** — 配置就绪后启用，重试读取最新来源，推送后核验全部引用。**反例**: 用户 fork 写入官方镜像，或旧事件覆盖新提交。**回归**: 工作流官方仓库条件与并发配置、重试及 SHA 核验测试。
