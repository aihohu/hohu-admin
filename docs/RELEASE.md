# 发布与升级维护

本文面向维护者。用户安装、部署与升级通过 hohu-cli；底层迁移说明见 [数据库迁移](DATABASE-MIGRATIONS.md)。

## 版本与产物

后端版本维护在 [pyproject.toml](../pyproject.toml)，变更说明维护在 [CHANGELOG.md](../CHANGELOG.md)。`Unreleased` 是待发布内容，不等于发布记录。遵循 [版本与兼容规范](VERSIONING.md)：Backend/Web 完整同号，App/Desktop 同兼容系列并独立递增补丁，CLI 独立版本，Docs 按产品系列维护。版本分支完成验收后合入默认分支，再使用本文的正式发布入口。

本文描述两仓当前发布保护与后端发布工具；两仓分别成功不代表产品组合已经验收。正式产品发布还需最终 Backend/Web 组合验证和主规范要求的 `release.json` 配套附件。产品清单使用已交付的 [离线/在线核验及不可覆盖上传工具](PRODUCT-RELEASE.md)，由维护者单独执行；组件流水线不会自动生成清单或完成跨仓产品编排。当前发布状态只在 [Docs 更新说明](https://github.com/aihohu/hohu-admin-docs/blob/main/docs/zh/guide/reference/versions.md) 维护。组件构建只写完整版本，保留系列标签与 `latest` 的原值；不得把旧别名宣称为新产品版本，生产升级以已验收清单的固定版本和摘要为准。

当前 [release.yml](../.github/workflows/release.yml) 通过官方仓库的 Actions → Release → Run workflow 启动，可选择测试镜像或正式发布。正式发布依次校验元数据、运行完整 CI 与 hosted 发布验证、创建 Tag、推送 GHCR 的 linux/amd64、linux/arm64 镜像；启用 ACR 后复制并核验，最后创建 GitHub Release。后端正式产物是容器镜像，此流程不发布 PyPI 包。

## 前置检查与复核时点

这些是维护者执行的流程门禁；除下文明确列出的现有自动检查外，不表示 CI 已自动执行全部检查。检查记录包含时间、负责人、目标仓库/平台、客户端及版本、结果和证据位置，不包含凭据值。

| 时点 | 必须完成的检查 | 未通过时 |
| --- | --- | --- |
| 版本范围确定、开发启动 | 核对 GHCR/ACR 写入身份、旧任务和外部写者；两仓秘密位于受保护 docker 环境且只允许默认分支；同名仓库级副本不再可用；记录凭据最后更新时间及关联仓库 | 记录阻塞、负责人和恢复条件，不标记发布前提就绪 |
| 首个可运行候选 | 按测试指南跑完登录、权限、文件、AI 状态及操作后的下一步；涉及真实事务协议时提前验证断链、静默 ACK 和取消清理假设 | 在扩展实现或候选冻结前修复，不等用户最终手测发现 |
| 正式构建前 | 在同一受保护环境中，使用实际交付客户端及同一身份完成认证和仓库访问；核对唯一写者证据仍有效、历史写入任务已停止 | 不启动正式构建；secret 名称存在、登录成功和目标推送授权分别记录，不能互相代替 |
| 最终产物与产品公告前 | 保留实际 SHA CI、固定镜像组合、安装/升级/恢复和匿名完整拉取验证；校验并不可覆盖上传清单 | 不宣布产品完成；早期检查不替代最终复验 |

认证路径必须与实际交付一致，例如 ACR 同步使用 Skopeo 时，单独的 HTTP 认证查询不能代替该客户端的认证检查。不得为核验读取或展示现有 secret；通过受保护任务使用它。写权限证明需要外部权限核对；若必须试推，只能使用已授权的测试仓库或唯一测试标签，不能写正式版本或稳定别名。轮换共享 ACR 密码时同步更新所有使用仓库，并重新检查后再恢复交付。

在开发启动阶段即从实际 runner/工具容器检查 GitHub API、Release 附件、GHCR 和启用的 ACR：DNS/TLS、认证域名、代理出口、限流及完整镜像层下载均须覆盖，不能只做首页或 manifest HEAD 请求。可以使用已有稳定产物在隔离缓存中先测一次完整下载，并据实际耗时准备时间预算。代理只用于任务进程，凭据不进入 URL、命令日志或公开记录。正式发布前，以及网络、代理、凭据、镜像目标或写者变化后复核；超时和查询失败保持未知，不关闭 TLS 校验或推断远端不存在。

唯一写者记录保存核验人、时间、仓库范围、凭据更新时间和失效条件。`RELEASE_EXCLUSIVE_WRITER_CONFIRMED=true` 只表示该记录已核实；凭据、权限、历史任务或外部写入入口变化后须撤回并重新确认，不能把永久布尔值当成永久证明。

## 网页正式发布

发布入口仅监听 `workflow_dispatch`，本地推送 Tag 或直接在 Releases 页面发布公告均不会启动工作流。两种模式都只接受官方仓库默认分支，运行使用点击 Run workflow 时绑定的提交 SHA，后续默认分支的新提交不会混入本次构建。

1. 先完成上文前置检查；更新 `pyproject.toml` 中的版本，执行 `uv lock` 同步根包版本，并完成 `CHANGELOG.md` 对应条目；工作流不会自动修改版本号或编写升级说明。
2. CHANGELOG 使用唯一的 `## [vMAJOR.MINOR.PATCH]` 标题，也可附加 `(YYYY-MM-DD)` 或 `- YYYY-MM-DD` 日期；必须是文件中首个版本条目，正文不能为空，标题不能保留 `Unreleased`。配套客户端、CLI 版本和升级注意事项应写入该条目。
3. 完成发布前验证，将代码、工作流及文档合入默认分支；若合入下一系列，先验证当前稳定系列的独立热修复发布路径，见 [版本规范](VERSIONING.md#8-热修复支持与恢复)。
4. 打开 GitHub 仓库 **Actions → Release → Run workflow**，分支选择默认分支（当前为 `main`），`mode` 选择 **release**，在 `version` 填写本次尚未发布的 **vMAJOR.MINOR.PATCH**，然后点击 **Run workflow**。
5. 查看运行进度，按仓库配置完成 `docker` Environment 的审批。全部发布任务成功后，Release 正文自动取自该提交 CHANGELOG 的对应版本内容，无需在 Releases 页面手动创建。

`mode` 默认值是 **test**，用于测试镜像，此时 `version` 必须留空；正式发布必须显式选择 **release** 并填写版本。版本仅支持 `vMAJOR.MINOR.PATCH`，不接受预发布、构建后缀或无 `v` 前缀的格式。

[prepare_release.py](../tools/ops/prepare_release.py) 校验输入版本、`pyproject.toml` 和 `uv.lock` 根包版本一致，并提取发布说明。初始检查拒绝已存在的版本 Tag，原运行重试除外。CI 通过可复用工作流检查同一提交的 Ruff、全量测试、至少 70% 覆盖率和 hosted CI profile；再通过 release profile 后，由 [release_tag.py](../tools/ops/release_tag.py) 创建指向该 SHA 的附注 Tag。校验或验收失败不会创建新 Tag 或推送正式镜像。

镜像版本标签与 OCI 元数据显式使用校验过的输入版本，不从运行分支名推断；ACR 复制同一次构建的 digest。Tag 注释记录创建它的 Actions 运行编号；创建公告前再次校验其运行编号与提交 SHA，拒绝被移动或替换的标签。

发布使用仓库自带的 `GITHUB_TOKEN`，无需新增 PAT。默认仅授予 `contents: read`，镜像任务授予 `packages: write`，创建 Tag 和公告的任务授予 `contents: write`。沿用 `docker` Environment 的保护规则，需允许默认分支使用该环境，并允许发布任务创建版本 Tag。各阶段在同一工作流内衔接，不依赖创建 Tag 或 Release 后再次触发 Actions。

正式构建仅允许原运行的第一次尝试。`run_attempt > 1` 会在构建准备前失败，包括 **Re-run all jobs** 和失败构建的 **Re-run failed jobs**；即便确认尚未上传，也不重新构建同号版本。测试模式仍使用每次尝试唯一的测试标签。

首次构建前，[release_registry.py](../tools/ops/release_registry.py) 建立认证上下文并读取仓库，检查 GHCR 与启用的 ACR 完整版本引用；仅明确的 `404 / MANIFEST_UNKNOWN` 允许继续。匿名成功、401/403、普通404、超时、限流和响应损坏都不能解释为不存在。已存在的任何完整版本都拒绝新构建。仓库必须预先可认证查询；HTTP认证只接受同源HTTPS入口；北京ACR另允许其已核实的精确认证入口 `https://dockerauth.cn-hangzhou.aliyuncs.com/auth`，该例外仅绑定 `registry.cn-beijing.aliyuncs.com`。其他外部入口、带查询参数或凭据的地址均拒绝，不带凭据跟随重定向。

维护者还须核实正式写权限仅供受控工作流使用、历史任务已终止、外部写者无写能力，然后配置仓库变量 `RELEASE_EXCLUSIVE_WRITER_CONFIRMED=true`。未配置或撤回后正式构建与下游恢复均停止。这是已核实操作前置的声明，不能代替权限审计；共享并发组只能协调纳入组的任务。

- 构建成功时，[release_build.py](../tools/ops/release_build.py) 核验源index和平台子manifest，保存 `component-build.json` 到唯一的 `component-build-<run_id>-1` Actions artifact。记录绑定仓库、源码SHA、完整版本、原run/attempt、源index digest、两种平台子digest、镜像目标及构建前不存在证明。上传失败使build job失败，不能恢复。
- 仅当第一次build job成功，才可在**同一原运行**重试失败的 ACR/公告任务。恢复从固定artifact读取，验证原job结果、工作流来源、artifact归属/时间/保留状态及压缩包SHA256，严格验证记录后重新核对源tag和digest。不能手输digest、使用其他run的记录或从当前标签猜测来源。
- 完整目标版本已是同一digest时只核验，不再次上传；不同digest、来源完整tag漂移或查询未知时硬失败。复制ACK丢失后，重试先读回目标；只有原digest才能幂等继续，不能覆盖冲突。
- 构建失败/取消且远端可能已有镜像、原成功记录缺失或过期、来源被改变、必须改代码或重建时，保留原版本和失败证据，确认写者停止，Backend/Web共同换新产品号。不得删除tag或artifact来重新使用旧版本。

原记录在Actions保留90天。公告阶段重新验证产物与匿名完整OCI拉取，先创建或保留draft，按字节核对并上传原 `component-build.json` 和确定性的 `component-delivery.json`，然后发布并读回。两个附件是组件构建/交付证据，不是最终产品 `release.json`。同名同字节可继续，不同字节或查询未知则拒绝，永不clobber；已有正式公告正文保持。上传或公告回执丢失时，在同一原run重试读取既有事实，不重新构建。去密记录随Release长期保留，不能只依赖短期Actions artifact。

同一运行可复用自身创建且仍指向原提交的 Git Tag；重新点击 Run workflow 会生成不同运行编号，已有 Tag 将被拒绝，即使提交 SHA 相同。这只是 Git Tag 的恢复规则，不替代上面的镜像状态判定。ACR未启用时可只交付GHCR；恢复时当前镜像配置必须与原记录一致，不能通过关闭镜像跳过已绑定的交付义务。

Tag、镜像仓库和GitHub Release之间没有原子事务，失败不会自动删除Tag或回滚镜像；本流程不更新系列/latest。Tag已创建后，如需修改代码，应准备新版本；原运行的重试始终使用原提交。使用 `GITHUB_TOKEN` 创建 Tag 不会触发其他 `push` 工作流，Gitee 标签镜像可通过既有定时或手动同步补齐。

## v0.1.5 历史兼容边界

以下仅说明 0.1.5 的历史边界，不代表当前版本的升级承诺；当前状态与支持来源见 [Docs 更新说明](https://github.com/aihohu/hohu-admin-docs/blob/main/docs/zh/guide/reference/versions.md)，历史变更见 [CHANGELOG](../CHANGELOG.md)。维护该版本时复核下列兼容边界，并在 Release 说明中记录实际配套的 Web、CLI 和构建提交：

- 数据库从空库或 `v0.1.4` 支持边界升级；内置设置拆表、模型授权回填和安全触发器必须随迁移落地。兼容范围和数据处理以 [数据库迁移](DATABASE-MIGRATIONS.md) 为准。
- 账号自助服务迁移关闭注册，邮件配置初始为空。部署方需验证 SMTP 和邮件送达，再显式启用所需租户的注册或找回密码；已有邮箱不能仅凭历史字段作为已验证的恢复地址。
- 管理端必须配套支持系统角色、租户设置、模型策略和持久确认。系统管理使用默认租户系统超级管理员的普通会话和必需审计上下文，不沿用旧平台 Token 访问这些管理接口；具体边界见 [AI 安全](AI-SECURITY.md)。
- 私有文件与公开上传分别持久保存，核查旧公开路径的代理拒绝规则；使用原加密密钥恢复 Provider 和邮件凭据。
- 主应用未开放 Marketplace、Contributes 或 Lowcode 数据路由，不能用历史代码或存量菜单作为发布能力证明；见 [应用市场状态](APP-MARKETPLACE.md)。

## ACR 应用镜像与手动验收

后端和 Web 各自的 `Release` 工作流复用真实应用构建。测试镜像仅接受默认分支，标签为 `test-<提交前 12 位 SHA>-<run_id>-<run_attempt>`，不创建 GitHub Release，也不更新正式版本和 `latest`。后端选择 `mode=test`、版本留空；Web 使用其现有手动测试入口。后端测试镜像模式不执行 release profile 资格验证，镜像链路验收不能替代正式发布验证。

GHCR 与 ACR 使用同一次构建的运行镜像。为兼容 ACR，构建显式关闭附加的 provenance/SBOM 证明清单，保留 `linux/amd64`、`linux/arm64` 两种运行架构及跨仓库摘要一致性。若旧镜像同步报 `unknown manifest class for application/vnd.oci.empty.v1+json`，需发布修复后的工作流并重新构建；重试旧 digest 无法消除不兼容的清单。

在两个GitHub仓库分别配置以下镜像设置。ACR凭据仅放在受保护的 `docker` Environment，环境限定默认分支；不能在仓库级保留供普通PR引用的同名副本。

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| docker Environment secret | `ACR_USERNAME` | 有目标仓库推送权限的 ACR 登录用户名 |
| docker Environment secret | `ACR_PASSWORD` | ACR 镜像仓库登录密码 |
| Variable | `ACR_REGISTRY` | 控制台给出的公网域名，不含协议和路径 |
| Variable | `ACR_REPOSITORY` | `命名空间/hohu-admin`；Web 为 `命名空间/hohu-admin-web` |
| Repository variable | `ACR_MIRROR_ENABLED` | 配置完成后设为 `true`；未启用时正式发布仍只推 GHCR；任务启动前判断，不能只配置为 Environment variable |
| Repository variable | `RELEASE_EXCLUSIVE_WRITER_CONFIRMED` | 核实正式写者隔离后设为 `true`；正式构建与恢复都要求保持，测试镜像不据此取得正式发布资格 |

将工作流和工具提交到默认分支后，在 Actions → Release → Run workflow 选择默认分支；后端保留默认的 `mode=test`，`version` 留空。测试镜像模式会在构建前检查 ACR 配置，沿用现有 `docker` Environment 的访问规则。发布资格验证在测试镜像模式中跳过，GHCR 构建成功后仍执行启用的 ACR 同步；构建失败、跳过或运行取消时不执行同步。同步失败可重跑失败任务，不必重新构建应用；修改工作流后应重新 Run workflow，旧运行的 Re-run 不使用新工作流。

[sync_image_mirror.py](../tools/ops/sync_image_mirror.py) 按构建digest完整复制多架构镜像，并使用无凭据的完整OCI拉取验证镜像层公开可读。完整版本的源漂移和目标冲突硬失败；同digest目标只校验。工具保留可变别名“源已移动则跳过”的独立语义，但当前工作流不会向它传入系列/latest。无单架构降级或digest转换。报告含 `verified_tags`、`skipped_tags`、`platforms`、`anonymous_pull`，保存在摘要和 `acr-mirror-*` Artifact。隔离回归无需应用服务：`uv run python -B -m unittest discover -s tests/tools -p 'test_*release*.py' -v`，镜像复制另运行 `test_sync_image_mirror.py`。

配置公开仓库后，使用报告中的测试标签执行 `docker pull <ACR_REGISTRY>/<ACR_REPOSITORY>:<test-tag>`，再在隔离环境验收应用启动。镜像复制与公开拉取通过不表示业务部署验收完成。本节应用镜像同步覆盖后端和 Web；基础设施镜像同步与部署来源选择由 [hohu-cli](https://github.com/aihohu/hohu-cli) 维护，按所用 CLI 版本的说明配置。

## 发布前验证

配套清单格式、离线/在线来源核验和不可覆盖上传见[配套发布清单](PRODUCT-RELEASE.md)。最终文件只有在 Backend/Web 实际产物及全部支持平台的安装、升级、恢复和 AI 写一致性证据齐备后才能公开；合成测试样本和未完成草稿不提供发布资格。

1. 明确待发布提交、同号 Backend/Web 版本、Release 说明和配套 CLI/客户端，按 [版本与兼容规范](VERSIONING.md) 准备配套清单；记录实际支持范围，不从版本号推断兼容。
2. 通过 [CI](../.github/workflows/ci.yml)：Ruff、测试、至少 70% 覆盖率、hosted 隔离验证；通过文档检查。
3. 对迁移验证空库安装、支持的旧版本升级及关键数据保留。数据库安全触发器也属于验收范围。
4. 在专用环境验证实际部署、初始化重复运行、登录、关键业务、上传和权限拒绝。真实 Provider 或浏览器未验证时如实列明。
5. 准备数据库和持久文件备份、恢复演练、旧构建与回滚窗口。
6. 复核源码、文档和构建产物，排除本地草案、凭据、业务数据及临时附件。
7. 组件产物生成后，用最终 Backend/Web 摘要完成组合验收，补齐并发布配套清单，再宣布产品发布；一端失败时按主规范保持产品未完成状态。

发布验证代码位于 [tests/release](../tests/release)。报告绑定构建 SHA，CI 和 release profile 的证据作为工作流产物保存；不能复用其他提交的通过记录，也不能在生产数据库运行这些测试。

正式验收要求干净 Git 工作区。本地未提交代码的演练不能替代绑定提交的发布证明。

资格验证通过默认租户系统超级管理员的普通登录会话管理租户，覆盖多个租户独立启停、租户管理员拒绝全局管理，以及切回 single 模式时撤销非默认租户访问；同时核对系统操作日志中的授权与完成关联。验证入口为 [qualify_tenant_hosted_release](../tests/release/qualify_tenant_hosted_release.py)。

回滚验收分别检查访问拒绝和业务状态不变：前者必须返回 `401 / TENANT_HOSTED_ACCESS_DISABLED`，失败码为 `ROLLBACK_ACCESS_NOT_REVOKED`；后者比较租户数据库快照和 Redis 键值摘要，失败码为 `ROLLBACK_SIDE_EFFECT_DETECTED`。Redis 比较只排除名称匹配且剩余 TTL 不超过运行上限的限流桶（60 秒）和限流配置缓存（30 秒），允许其正常过期、重建及计数变化；其他键的变化仍阻断发布。报告记录数据库与缓存比较结果、前后键数量、HTTP 状态及脱敏错误码，不公开 Redis 键名或内容。

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
