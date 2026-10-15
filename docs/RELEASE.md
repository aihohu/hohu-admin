# 发布与升级维护

本文面向维护者。用户安装、部署与升级通过 hohu-cli；底层迁移说明见 [数据库迁移](DATABASE-MIGRATIONS.md)。

## 版本与产物

后端版本维护在 [pyproject.toml](../pyproject.toml)，变更说明维护在 [CHANGELOG.md](../CHANGELOG.md)。`Unreleased` 是待发布内容，不等于发布记录。各子项目独立版本，但跨项目契约变更必须给出配套版本及升级顺序。

当前 [release.yml](../.github/workflows/release.yml) 在官方仓库推送 `vMAJOR.MINOR.PATCH` Tag 时运行：校验发布元数据，调用完整 CI，再执行 hosted 发布验证，构建并推送 GHCR 的 linux/amd64、linux/arm64 镜像；启用 ACR 镜像后复制并核验，最后自动创建 GitHub Release。默认分支也支持手动构建测试镜像。后端正式产物是容器镜像，此流程不发布 PyPI 包。

## Tag 自动发布

正式发布仅支持稳定的三段版本，例如 `v0.1.5`；预发布、构建后缀和无 `v` 前缀的标签不支持。工作流必须包含在待发布提交内，该提交必须已合入仓库默认分支。手动在 GitHub 页面发布 Release 不会启动本流程。

1. 更新 `pyproject.toml` 中的版本，执行 `uv lock` 同步根包版本，并完成 `CHANGELOG.md` 对应条目。
2. CHANGELOG 使用唯一的 `## [v0.1.5]` 标题，也可附加 `(YYYY-MM-DD)` 或 `- YYYY-MM-DD` 日期；必须是文件中首个版本条目，正文不能为空，标题不能保留 `Unreleased`。配套客户端、CLI 版本和升级注意事项应写入该条目。
3. 完成发布前验证，将变更提交并合入默认分支，确认本地检出的待发布提交正确。
4. 为该提交创建并推送一个尚未发布的 Tag。以下以首次发布 `v0.1.5` 为例；其他版本应替换版本号，已有标签不要强制移动：

   ```bash
   git tag -a v0.1.5 -m "Release v0.1.5"
   git push origin v0.1.5
   ```

5. 在 Actions → Release 查看运行结果。全部发布任务成功后，Release 正文自动取自该提交 CHANGELOG 的对应版本内容，无需再复制到 GitHub 页面。

[prepare_release.py](../tools/ops/prepare_release.py) 校验 Tag、`pyproject.toml` 和 `uv.lock` 根包版本一致，并提取发布说明。可在推送前本地检查：

```bash
uv run python -B tools/ops/prepare_release.py --tag v0.1.5 --notes-file .local/artifacts/release-notes.md
```

CI 通过可复用工作流检查同一提交的 Ruff、全量测试、至少 70% 覆盖率和 hosted CI profile；正式镜像还必须通过 release profile。版本校验或任何必需验证失败时不开始镜像发布。镜像版本标签与 OCI 元数据从已校验 Tag 生成，ACR 复制同一次构建的 digest。源码 checkout 固定提交 SHA，创建公告前再次核对远端 Tag 未被移动。

发布使用仓库自带的 `GITHUB_TOKEN`，无需新增 PAT。默认仅授予 `contents: read`，镜像任务授予 `packages: write`，最后创建公告的任务授予 `contents: write`；沿用 `docker` Environment 的保护规则，需允许正式 Tag 使用该环境。测试、构建和公告在同一流程中显式串联，不依赖自动创建 Release 后再次触发 Actions。

失败时在原运行中选择 **Re-run failed jobs**。GHCR 成功、ACR 失败时不会创建公告，重跑失败任务可继续同步已有 digest；ACR 未启用时允许跳过同步并创建公告。已存在的正式 Release 保留正文；同名草稿在发布链路成功后使用 CHANGELOG 正文发布。镜像仓库与 GitHub Release 之间没有原子事务，失败不会自动回滚已经推送的镜像或 `latest`。修改代码或工作流后，应准备新的版本和 Tag，旧运行的重试仍使用旧提交。

## v0.1.5 发布核对

完整变更维护在 [CHANGELOG](../CHANGELOG.md)。发布时复核下列兼容边界，并在 Release 说明中记录实际配套的 Web、CLI 和构建提交：

- 数据库从空库或 `v0.1.4` 支持边界升级；内置设置拆表、模型授权回填和安全触发器必须随迁移落地。兼容范围和数据处理以 [数据库迁移](DATABASE-MIGRATIONS.md) 为准。
- 账号自助服务迁移关闭注册，邮件配置初始为空。部署方需验证 SMTP 和邮件送达，再显式启用所需租户的注册或找回密码；已有邮箱不能仅凭历史字段作为已验证的恢复地址。
- 管理端必须配套支持系统角色、租户设置、模型策略和持久确认。系统管理使用默认租户系统超级管理员的普通会话和必需审计上下文，不沿用旧平台 Token 访问这些管理接口；具体边界见 [AI 安全](AI-SECURITY.md)。
- 私有文件与公开上传分别持久保存，核查旧公开路径的代理拒绝规则；使用原加密密钥恢复 Provider 和邮件凭据。
- 主应用未开放 Marketplace、Contributes 或 Lowcode 数据路由，不能用历史代码或存量菜单作为发布能力证明；见 [应用市场状态](APP-MARKETPLACE.md)。

## ACR 应用镜像与手动验收

后端和 Web 各自的 `Release` 工作流复用真实应用构建。手动运行仅接受默认分支，标签为 `test-<提交前 12 位 SHA>-<run_id>-<run_attempt>`，不创建 GitHub Release，也不更新正式版本和 `latest`。后端手动模式不执行 release profile 资格验证，镜像链路验收不能替代正式发布验证。

GHCR 与 ACR 使用同一次构建的运行镜像。为兼容 ACR，构建显式关闭附加的 provenance/SBOM 证明清单，保留 `linux/amd64`、`linux/arm64` 两种运行架构及跨仓库摘要一致性。若旧镜像同步报 `unknown manifest class for application/vnd.oci.empty.v1+json`，需发布修复后的工作流并重新构建；重试旧 digest 无法消除不兼容的清单。

在两个 GitHub 仓库的 Settings → Secrets and variables → Actions 分别配置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Secret | `ACR_USERNAME` | 有目标仓库推送权限的 ACR 登录用户名 |
| Secret | `ACR_PASSWORD` | ACR 镜像仓库登录密码 |
| Variable | `ACR_REGISTRY` | 控制台给出的公网域名，不含协议和路径 |
| Variable | `ACR_REPOSITORY` | `命名空间/hohu-admin`；Web 为 `命名空间/hohu-admin-web` |
| Repository variable | `ACR_MIRROR_ENABLED` | 配置完成后设为 `true`；未启用时正式发布仍只推 GHCR；任务启动前判断，不能只配置为 Environment variable |

将工作流和工具提交到默认分支后，在 Actions → Release → Run workflow 选择默认分支。手动模式会在构建前检查 ACR 配置；沿用现有 `docker` Environment 的访问规则。发布资格验证在手动模式中跳过，GHCR 构建成功后仍执行启用的 ACR 同步；构建失败、跳过或运行取消时不执行同步。同步失败可重跑失败任务，不必重新构建应用；修改工作流后应重新 Run workflow，旧运行的 Re-run 不使用新工作流。

[sync_image_mirror.py](../tools/ops/sync_image_mirror.py) 按构建 digest 完整复制多架构镜像，检查两种架构、目标标签 digest，并使用无凭据的完整 OCI 拉取验证镜像层公开可读。已移动到其他 digest 的来源标签跳过，避免延迟任务覆盖新别名；不降级单架构或转换 digest。成功报告显示 `verified_tags`、`skipped_tags`、`platforms` 和 `anonymous_pull`，保存在工作流摘要及 `acr-mirror-*` Artifact 中。隔离回归无需应用服务：`python -B -m unittest discover -s tests/tools -p test_sync_image_mirror.py -v`。

配置公开仓库后，使用报告中的测试标签执行 `docker pull <ACR_REGISTRY>/<ACR_REPOSITORY>:<test-tag>`，再在隔离环境验收应用启动。镜像复制与公开拉取通过不表示业务部署验收完成。本节应用镜像同步覆盖后端和 Web；基础设施镜像同步与部署来源选择由 [hohu-cli](https://github.com/aihohu/hohu-cli) 维护，按所用 CLI 版本的说明配置。

## 发布前验证

1. 明确待发布提交、版本号、Release 说明和配套 CLI/客户端。
2. 通过 [CI](../.github/workflows/ci.yml)：Ruff、测试、至少 70% 覆盖率、hosted 隔离验证；通过文档检查。
3. 对迁移验证空库安装、支持的旧版本升级及关键数据保留。数据库安全触发器也属于验收范围。
4. 在专用环境验证实际部署、初始化重复运行、登录、关键业务、上传和权限拒绝。真实 Provider 或浏览器未验证时如实列明。
5. 准备数据库和持久文件备份、恢复演练、旧构建与回滚窗口。
6. 复核源码、文档和构建产物，排除本地草案、凭据、业务数据及临时附件。

发布验证代码位于 [tests/release](../tests/release)。报告绑定构建 SHA，CI 和 release profile 的证据作为工作流产物保存；不能复用其他提交的通过记录，也不能在生产数据库运行这些测试。

正式验收要求干净 Git 工作区。本地未提交代码的演练不能替代绑定提交的发布证明。

资格验证通过默认租户系统超级管理员的普通登录会话管理租户，覆盖多个租户独立启停、租户管理员拒绝全局管理，以及切回 single 模式时撤销非默认租户访问；同时核对系统操作日志中的授权与完成关联。验证入口为 [qualify_tenant_hosted_release](../tests/release/qualify_tenant_hosted_release.py)。

API 完全关闭后，验证工具只在显式声明的临时测试 Redis 非默认数据库清理带过期时间的限流桶和限流配置缓存。未知或持久键仍导致验收失败，不能使用 `FLUSHDB` 掩盖残留。失败输出失败码，HTTP 失败补充状态与脱敏错误码，日志不得包含凭据。

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

工作流只接受官方仓库事件，执行默认分支中的受信任工具；启用开关默认关闭，同一仓库串行同步。每次重试重新获取来源快照，推送后核验所有分支与标签，防止延迟事件回写旧引用。

同步前先验证目标仓库可读取，再获取最新来源并推送；SSH 保活用于检测失联，单次推送限时 600 秒，总任务限时 30 分钟。连接检查失败不推送，失败日志保留阶段和超时前的诊断。

其他组件由各自仓库独立维护镜像工作流、工具、密钥和目标仓库变量。客户端镜像任务必须限定自身官方仓库，不能沿用后端仓库白名单或依赖后端 checkout。配置完成后逐仓库核验引用一致性和实际 HTTPS clone，不把代码检查作为线上同步已成功的证据。
