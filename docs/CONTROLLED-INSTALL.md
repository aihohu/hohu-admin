# 0.2 受控安装与 CLI 交接

本文适用于 Backend/Web 0.2.0 候选。最终支持的来源、镜像、平台与 CLI 组合以正式产品 `release.json` 为准；版本元数据和源码测试不能替代最终产物验收。

CLI 0.1.16 使用 `hohu deploy`，不读取产品清单，也不自动生成停写证明。其默认 migrator 在迁移后执行裸 `python -m scripts.init_db`；0.2 的初始化要求维护 inventory，因此不能直接使用这一默认步骤。保留已发布 CLI，通过以下宿主受控准备和显式启动交接安装。无需新增部署平台；直接启动与 Docker Compose 都使用现有维护控制器。

CLI 使用独立 Python 虚拟环境，安装固定版本 `python -m pip install "hohu==0.1.16"`，再用 `hohu --version` 核验。记录发布包来源、哈希及该环境依赖；不要用本地 CLI main checkout 或未固定版本的安装结果代替已验收包。CLI 的 Python 最低版本为3.10，与下述 Backend 控制器环境分别管理。

## 前置条件

- 使用与目标 Backend 相同完整 SHA 的只读源码和锁定依赖，设置准确的 `RELEASE_BUILD_SHA`。控制器需要 Python >=3.12；Compose 控制器在宿主使用 Docker CLI，不在普通 API 容器内挂 Docker socket。
- 管理员先建立目标数据库与一个独立、非 superuser 的应用角色，赋予该角色目标数据库及 `public` schema 的迁移权限。API/scheduler 使用这个应用角色；宿主 `AUTHORIZATION_MAINTENANCE_DATABASE_URL` 使用独立管理身份，连接同一目标数据库。不要把维护 URL、维护密码放进应用的共享 `.env`。
- 新装准备空数据库，禁止先启动应用；设置受控的 `AUTHORIZATION_MAINTENANCE_PASSWORD` 作为初始管理员密码。升级只接受 `d01010100101`、`e0204a000001` 或当前 `e020f1200001`；其他起点先走单独验证的升级路径。此入口只支持单应用数据库角色。
- 备份配套数据库、公开/私有文件、原密钥、配置和撤权事实。旧 API、scheduler、API 内 AI 恢复/过期任务、维护脚本及外部写者全部纳入清点；用户入口保持维护状态。
- 控制目录由维护人员保护，直接启动实例与宿主控制器共享正确的运行登记目录。每次新操作保留之前证据，不能通过删除 `stop.json` 或失败回执来绕过恢复检查。

维护 inventory 与 [授权维护协议](AUTHORIZATION-MAINTENANCE.md) 的入口共用。新装文件使用以下结构，替换实际部署名、完整 Backend SHA 和应用角色；字符串占位符不是有效资格。

```json
{
  "manifest": {
    "deployment_id": "my-deployment",
    "build_sha": "<40-character-backend-sha>",
    "mode": "compose",
    "surfaces": ["api", "scheduler"]
  },
  "application_roles": ["hohu_app"],
  "bootstrap": true
}
```

升级文件删除 `bootstrap`，加入真实 `processes`。Compose 使用 `{"mode":"compose","project":"实际项目名","services":{"实际服务名":["64位实际容器ID"]}}`，必须列全该项目的应用写者，不能只登记一台 API。直接启动的 manifest.mode 为 `direct`，processes 为 `{"mode":"direct","identities":[{"pid":实际PID,"birth":"实际进程出生标识"}]}`；使用既有进程身份采集工具，不能手编 birth。普通 Ctrl+C 或 compose down 的退出状态不是可信证据。

## 准备、恢复 admission 与启动

以下命令由维护者在目标 Backend checkout 的锁定环境执行。文件路径属于本次受控部署，不使用其他项目的 inventory。

```bash
# 新装：空库检查、固定迁移、bootstrap seed；成功仍保持关闭
python -m tools.ops.controlled_install prepare \
  --bootstrap --inventory /protected/install.json --control-dir /protected/control

# 升级/再次同步种子：使用实际旧进程 inventory，省略 --bootstrap
python -m tools.ops.controlled_install prepare \
  --inventory /protected/upgrade.json --control-dir /protected/control
```

两者是替代路径，不连续执行。控制器先持有数据库维护锁，预热两条连接并关闭新连接，停止旧进程、禁重启及观察数据库在途事务；迁移复用原连接，以应用角色运行。迁移失败不执行 seed。成功返回 `prepared-admission-closed`，生成独占的 `control/install.json` 回执，绑定 build、数据库、目标 revision、模式和停写记录摘要。数据库新连接仍被拒绝，程序不会启动 CLI、API 或 scheduler。

确认迁移/seed回执和配套产物后，等待最后一个维护 permit 超过30秒，再显式恢复数据库 admission；bootstrap 模式必须与 prepare 一致：

```bash
python -m tools.ops.controlled_install resume \
  --bootstrap --inventory /protected/install.json --control-dir /protected/control
```

升级使用原 upgrade.json 并省略 `--bootstrap`。回执缺失、篡改、build或模式不同、旧进程仍活跃、目标库存在连接或permit未过期均拒绝。成功仅恢复数据库 admission，不恢复用户流量、进程或重启策略。数据库 admission 的重新开放必须与旧实例/旧凭据的运维隔离配合，不能让旧版本重新进入。

在独立项目执行 `hohu deploy init` 后，人工 Compose override 固定 db-migrator/API/scheduler 的同一 Backend digest、Web digest及已验收基础设施，设置实际平台与 `pull_policy: never`。不要把完整 digest 放进仍拼接 `:IMAGE_TAG` 的变量。发布前的候选优先 `linux/amd64`、PG15/Redis7；CLI 默认 PG18/Redis8.6 不属于同一验收组合。准备并核验全部镜像，记录 Compose 最终配置和实际镜像 ID；不要打印包含秘密的展开配置。

仅在上述 prepare、resume 及镜像预检都成功后，从 CLI 项目目录启动：

```bash
hohu deploy --source official --no-migrate
```

这里的 `--no-migrate` 是在必需迁移与 seed 已完成后的交接，不是跳过它们。CLI 本身不校验回执，操作者必须保留关联证据；不能用这个选项绕过失败步骤。保持入口仅供验收，scheduler 暂用同一 Backend 镜像的等待命令，确认受控任务后才恢复原命令与重启策略。直接启动采用同样的 prepare/resume 协议，再用部署方原有启动方式运行配套进程。

此路径实际迁移执行者是宿主控制器，不会再运行CLI模板的db-migrator容器。该服务配置仍固定到同一Backend引用以防误用，但不能将其声明为已运行的迁移镜像。最终产物验收还须核对宿主源码、迁移、种子与锁定依赖的来源，证明与目标Backend镜像对应，并分别记录控制器运行环境和应用镜像身份。

## 失败与恢复

通用策略需要受控编辑时，使用 `policy-prepare` 替代上述 `prepare`，可加 `--policy-port 8765`。其余 inventory、bootstrap、build、角色和控制目录要求相同。入口仅监听本机并使用原有管理 JWT，不启动普通业务任务；详见[通用字段策略存储](FIELD-POLICY-STORAGE.md)。普通 `prepare` 只安装结构和种子，不自动迁入通用授权状态。

安装回执 schema 为 2，额外绑定准备记录的摘要；旧 schema 回执不能用于当前恢复。一旦建立 `policy-prepare.json`，当前版本的正常恢复入口将拒绝开放 admission，即使准备 HTTP 会话已成功结束。全系统对象接线和完整资格尚未完成，不能据此恢复生产流量；禁止删标记或回用旧回执绕过。

异常、取消、迁移/seed或回执写入失败均不自动恢复 admission，也不自动重试业务或启动服务。`stop.json` 表示维护尚未解除；失败时保持入口隔离，先核对已提交的迁移/seed事实及备份。已迁移但seed未知不能重新当作空库bootstrap；通过现有维护修复协议处理，不能删记录或`stamp head`伪装成功。成功回执只记录这一阶段，不是正式发布资格或镜像正确性的证明。

激活前恢复要求原版本、数据库、文件与密钥配套。激活后不得回接0.1.6后端或直接开放旧备份，须证明撤权/字段策略连续性和故障后的副作用，再向前恢复；详见[数据库迁移与恢复](DATABASE-MIGRATIONS.md)。最终产品仍需实际镜像组合的新装、升级、恢复、旧客户端拒绝与AI写一致性验收。
