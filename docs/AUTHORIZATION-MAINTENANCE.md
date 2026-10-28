# 授权维护的停写与恢复

本手册面向后端维护者，说明直接启动和 Docker Compose 的停写控制。应用仍支持开发环境的 API 与 scheduler 合并运行，以及 API、独立 scheduler 分开运行；AI 启动恢复和过期扫描属于 API 进程。维护工具不要求增加部署平台。

应用通过 `AUTHORIZATION_CONTROL_DIR` 指定持久控制目录，默认 `.local/runtime/authorization`。同一部署的所有 API、scheduler 和维护控制器必须看到同一目录。Compose 应把同一个宿主机目录挂载到这些容器，并配置对应容器内路径。目录仅向部署管理员和应用运行账号开放写权限，不能作为公开上传目录或静态资源目录。

## 停写边界

`stop.json` 存在或控制文件无法读取时，服务端拒绝新 HTTP 请求，返回 HTTP 503、业务码 503、`errorCode=AUTHORIZATION_MAINTENANCE_ACTIVE`；WebSocket 使用关闭码 1013。GET 也被阻断，因为部分读取入口会执行恢复或日志写入。检查发生在认证和业务数据库访问前，不信任客户端版本头，旧 Web 直接发送请求也受限制。

后台调度、AI 恢复与扫描入口同时关闭接纳。已经进入的请求可以完成；新任务不能继承父任务的接纳资格。进程一旦进入停写状态，即使删除控制文件也不会自行重新开放。正常关闭会等待已启动的后台资源清理；Ctrl+C、信号处理器返回、活动任务数或 `quiesced` 记录均不能单独证明停写。

维护控制器还必须核对以下事实：

- 直接启动：清单中的每一个 PID 与内核创建代际匹配，所有登记的存活进程都在清单中；实际进程已经退出。Windows 虚拟环境启动器、reload 进程和 supervisor 可能拥有独立 PID，也必须纳入清单。PID 复用或不可观察状态会被拒绝。
- Compose：精确 project、service 与完整容器 ID 对应，相关服务没有额外实例；重启策略已设为 `no`，容器不处于运行、重启、暂停或异常 dead 状态。原重启策略保存于控制记录。停止命令成功不能替代再次检查。
- PostgreSQL：目标数据库已设置 `ALLOW_CONNECTIONS=false`，没有应用会话、在途事务或 prepared transaction。包括空闲应用池连接在内的任何额外会话都会阻断证明。

数据库开关会阻止包括超级用户在内的新连接；`CONNECTION LIMIT 0` 不具有相同保证。控制连接使用同一服务器上的 `postgres` 数据库（目标为 `postgres` 时使用 `template1`）。关闭前预热两个维护池连接，逐一记录 PID 与 backend_start；观察时它们必须全部空闲且无事务。维护动作随后从这个池创建新的严格会话。[PostgreSQL 数据库连接开关](https://www.postgresql.org/docs/16/sql-alterdatabase.html)

当前控制器要求数据库管理员权限，并拒绝启用逻辑复制订阅或未知 `shared_preload_libraries` 的目标；允许空配置和 `pg_stat_statements`。这些额外数据库写者需要独立适配，不能通过忽略检查放行。

## 清单与命令

先检查帮助：

```bash
python -m tools.ops.authorization_maintenance --help
```

清单是最多 64 KiB 的运维 JSON，包含且仅包含 `manifest`、`application_roles`、`processes`。`manifest` 包含 `deployment_id`、40 位小写十六进制 `build_sha`、`mode`（`direct` 或 `compose`）和 `surfaces: ["api", "scheduler"]`。合并运行仍覆盖这两个 surface。`build_sha` 必须与控制器的 `RELEASE_BUILD_SHA` 相同。

直接启动的 `processes` 为 `{"mode":"direct","identities":[{"pid":12345,"birth":"..."}]}`。`birth` 必须是实际内核代际：应用登记文件中的 `identity` 可用于收集运行实例；启动器和 supervisor 使用 `app.core.process_identity.ProcessIdentity.read(pid)` 读取。不能用 PID 数字、进程名称或手工时间戳替代。控制目录的进程记录不能删掉以规避清单完整性检查。

Compose 的 `processes` 为 `{"mode":"compose","project":"deployment","services":{"api":["完整容器ID"],"scheduler":["完整容器ID"]}}`；只选择应用服务，不选择 PostgreSQL 或 Redis。每个服务都要列出全部实例；合并模式只列实际承载两种职责的服务。

`application_roles` 列出该目标库的应用数据库角色。数据库连接信息只通过 `AUTHORIZATION_MAINTENANCE_DATABASE_URL` 环境变量传给控制器，不能放入清单或命令参数。工具不输出连接串或密码。

```bash
python -m tools.ops.authorization_maintenance stop --inventory deployment.json --control-dir CONTROL_DIRECTORY
python -m tools.ops.authorization_maintenance observe --inventory deployment.json --control-dir CONTROL_DIRECTORY
python -m tools.ops.authorization_maintenance execute --inventory deployment.json --control-dir CONTROL_DIRECTORY --action seed.settings
python -m tools.ops.authorization_maintenance resume --inventory deployment.json --control-dir CONTROL_DIRECTORY
```

`stop` 和 `execute` 都从尚未关闭的部署开始，独占创建控制记录；不能在 `stop` 之后再运行 `execute`。需要维护动作时直接使用 `execute`，它在一次控制会话内完成关闭、停止、独立观察、固定 owner 执行和最终空闲核验。控制器退出后不会自动开放数据库或入口。

当前固定动作是 `seed.system`、`seed.agents`、`seed.settings`、`principal.create` 和 `principal.permissions`；不接收任意 SQL、脚本、回调或 session factory。种子管理员密码或平台主体密码由 `AUTHORIZATION_MAINTENANCE_PASSWORD` 传入。平台主体动作另需 `--payload` JSON，包含 `principal_name`、`permissions`、`reason`、`ticket_id`、`correlation_id`，创建时另含 `display_name`。密码不能放入 payload。既有业务校验、权限清单、审计和唯一事务完成规则仍然生效。

## 失败与显式恢复

失败、取消、控制器崩溃或观察不确定时保留停写状态。新的控制器可以通过控制数据库执行 `observe` 或 `resume`；不能在目标库仍关闭时重新创建维护池并重放未知动作。业务提交返回 `AUTHORIZATION_COMMIT_UNKNOWN` 时保持未知，不按普通失败重试。

`resume` 重新核对清单、实际进程停止及数据库空闲；最近签发的 30 秒证明尚有效时拒绝恢复。成功后恢复原数据库连接开关并移除停止标记，不启动应用，也不恢复 Docker 重启策略。部署管理员应先确认维护结果和待启动构建，再按记录恢复所需重启策略并启动已核对的实例。原进程不能靠删除文件重新进入。

当前维护入口和停写机制已经接入实际启动路径；完整业务授权 owner 的生产迁移仍有未接入入口。这些控制能力不能作为全业务授权屏障已经开放、字段策略已启用或版本已经发布的证明。
