# hohu monitoring CLI 集成方案

> 状态：⚠️ Plan 未开始 | 创建日期：2026-07-29
>
> 配套 hohu-admin v1.5+ Prometheus 接入（commit `7ea6e8f` 已 ship 到 main via PR #5），补齐
> 客户运维侧"如何起 Prometheus + Grafana 抓 hohu-admin metrics" 的体验缺口。
>
> **相关文档**：[AI Tool Gateway 设计](./2026-07-02-ai-tool-gateway-design.md) §14 v1.5+ Roadmap；
> 告警规则源 [`monitoring/alerts.yml`](../monitoring/alerts.yml)（6 条规则，critical/warning）；
> 现有 deploy 实现参考 `hohu-cli/hohu/commands/admin/deploy.py`。

## 背景

hohu-admin 端 `/metrics` endpoint 已 ship（commit `7ea6e8f`，8 个 metric + 6 条告警规则），但
客户运维侧没有配套的部署工具：要自己手写 docker-compose 起 Prometheus + Grafana，自己配置
Prometheus target、自己复制 alerts.yml、自己 provisioning Grafana datasource。这是 v1.5+
接入和实际运维之间的体验断裂。

本 spec 定义 `hohu monitoring` CLI 命令组，让客户运维用一行命令把监控栈拉起来：

```bash
hohu deploy          # 起业务栈（已存在）
hohu monitoring init # 一次性：复制 prometheus.yml + 复制 alerts + 自动填 .env
hohu monitoring up   # 起 Prometheus + Grafana
```

## 参考借鉴

| 项目 | 是否借鉴 | 说明 |
|---|---|---|
| `hohu deploy` 命令组（`deploy.py`） | ✅ | 复用 `_compose_cmd()` / `_ensure_deploy_dir()` / `_sync_templates()` 骨架；复用 `_generate_secrets()` 自动填密码 |
| docker compose `profiles` 特性 | ✅ | monitoring 服务用 `profiles: [monitoring]` 隔离，跟业务栈共享同一个 compose project 和 `hohu-network` |
| Grafana provisioning（datasources/dashboards） | ✅ | 自动配 Prometheus datasource，dashboard JSON 留 PR-2 |
| Prometheus alert `rule_files` | ✅ | 从 `hohu-admin/docs/monitoring/alerts.yml` 单向复制，避免规则双源维护 |
| Alertmanager | ❌ | 留 PR-3，本 spec 不实现 |
| Prometheus federation / 多实例 | ❌ | 单实例足够覆盖 TOB 单租户私有部署场景 |

## 决策记录

### 1. **Profile 方案，而非独立 stack** —

客户运维心智负担最关键。memory `project_monitoring_cli_todo.md` 原写"独立 stack"，但
重新对比后发现 profile 方案在三个维度都更优：

| 维度 | Profile（采用） | 独立 stack + external network |
|---|---|---|
| 客户心智 | 1 个 stack、1 套 `.env` | 2 个 stack、网络依赖、要记得起停顺序 |
| 网络坑 | 无（同 compose 的 `hohu-network`） | external network name 漂移、创建顺序坑 |
| Prometheus target | `http://hohu-admin-api:8000/metrics` 直通 | 要跨 stack DNS 解析或走 host network |
| 配置漂移 | 无 | 有 |
| 模板改动 | docker-compose.yml 追加 2 个 service 区段 | 改 networks + 新 stack 全套文件 |

"独立 stack" 在 memory 里只是实现路径，本意是"减少客户运维负担、可插拔"。profile 方案对
这两点都比独立 stack 做得更好。

**反例**：独立 stack + external network → 客户先 `hohu deploy up` 再 `hohu monitoring up`，
顺序错了就报 "network hohu-net not found"，体验断裂。

**回归**：`tests/test_monitoring.py::test_compose_cmd_includes_profile_flag` 验证
`hohu monitoring up` 生成的 compose 命令含 `--profile monitoring`。

### 2. **`hohu monitoring` 命令组保留，作为 profile 透传 wrapper** —

虽然 monitoring 服务已合并进 deploy 的 compose，但命令组语义清晰（客户从命令名直接知道
在操作什么），且 `init` 步骤需要独立空间（就位 prometheus.yml、复制 alerts、配置 Grafana
provisioning），跟 `hohu deploy` 的 `_generate_secrets` 不耦合。

命令组底层是 `docker compose --profile monitoring ...` 的 wrapper：

| 命令 | 等价 docker compose |
|---|---|
| `hohu monitoring init` | 复制 prometheus.yml + alerts.yml + 自动填 .env |
| `hohu monitoring up` | `docker compose --profile monitoring up -d prometheus grafana` |
| `hohu monitoring down` | `docker compose --profile monitoring stop prometheus grafana` + `docker compose --profile monitoring rm -f prometheus grafana` |
| `hohu monitoring logs` | `docker compose logs prometheus grafana` |
| `hohu monitoring ps` | `docker compose ps prometheus grafana` |
| `hohu monitoring restart` | `docker compose restart prometheus grafana` |

> **为什么 `down` 用 stop+rm 而非 `compose down`**：`docker compose down` 会移除整个
> project 的容器与网络（profile 只决定哪些服务"参与"，不影响 `down` 的清理范围）。
> 直接 `--profile monitoring down` 在某些 compose 版本会连带把业务栈容器一起停掉、把
> `hohu-network` 拆掉。stop+rm 显式锁定服务名，作用域不外溢。

**反例**：把 monitoring 合并进 `hohu deploy --with-monitoring` flag → flag 越加越多
（`--with-monitoring` / `--with-logging` / `--with-tracing`），违反单一职责。

**回归**：`tests/test_monitoring.py::test_command_group_registered` 验证 `hohu monitoring --help`
列出全部 6 个子命令。

### 3. **模板放 `templates/deploy/`，而非 `templates/monitoring/`** —

profile 服务跟 deploy 同 stack 同模板，物理上分开会导致 sync 时跨目录、心智负担。
templates/deploy/ 当前结构是 `docker-compose.yml + nginx/`，追加 `prometheus/` 和 `grafana/`
两个子目录即可。

新模板布局：

```
hohu-cli/hohu/templates/deploy/
├── docker-compose.yml             # 追加 prometheus/grafana service (profile: [monitoring])
├── nginx/                         # 已存在
├── prometheus/
│   ├── prometheus.yml             # 固定 target=hohu-admin-api:8000，由 _sync_templates 直接复制
│   └── rules/
│       └── ai-tool-gateway.yml    # 维护者从 hohu-admin/docs/monitoring/alerts.yml 同步
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── prometheus.yml     # 自动配 Prometheus datasource
        └── dashboards/
            └── .gitkeep           # 留 PR-2 加 JSON
```

**反例**：模板放 `templates/monitoring/` → `_sync_templates()` 要扩成跨目录，且 docker
compose 文件还是要在 deploy 目录里（因为同 stack），导致"模板在 monitoring/，复制产物在
deploy/"，分裂。

**回归**：`tests/test_monitoring.py::test_templates_under_deploy_dir` 验证模板路径。

> **不使用 `.tpl` 后缀 + 不写渲染函数**：target 固定，无需变量替换。`.tpl` + 专用
> `_render_prometheus_config()` 是死代码 + 误导命名。未来加多 tenant / 多实例时再引入
> `.tpl` 与渲染逻辑，YAGNI。

### 4. **alert rules 单源 + 两步单向同步，不反向回流** —

`hohu-admin/docs/monitoring/alerts.yml` 是 source of truth（跟 hohu-admin 代码一起演进，
经 hohu-admin 测试覆盖）。规则经两步到达客户：

1. **维护者侧（发版前手动）**：hohu-admin alerts.yml 更新后，hohu-cli 维护者把它复制到
   `hohu-cli/hohu/templates/deploy/prometheus/rules/ai-tool-gateway.yml`，bump
   `.template-version`（如 0.1.13 → 0.1.14），随 hohu-cli 一起发布。文件头注释写明 source
   path（`# source: hohu-admin/docs/monitoring/alerts.yml @ <commit>`）方便后续 diff。
2. **客户侧（`hohu monitoring init` 触发）**：`_sync_templates()` 把模板里的
   ai-tool-gateway.yml 复制到 `.hohu/deploy/prometheus/rules/`。

不做反向同步（客户改本地 rules 不回流）。hohu-cli 不在运行时联网拉 hohu-admin（airgapped
部署不能依赖网络）。

**反例**：客户改本地 rules/ai-tool-gateway.yml 后 `hohu monitoring init` → 被模板覆盖 →
客户定制丢失。规避分两层：

1. **模板版本相同**（绝大多数情况）：`_sync_templates` 走"仅补缺"分支
   （`deploy.py:135`），客户本地修改完整保留。
2. **模板版本 bump**（hohu-cli 升级，alerts.yml 有更新）：`_collect_outdated_files()` 把
   ai-tool-gateway.yml 列入 outdated，但 `_sync_templates` 在覆盖前弹
   `questionary.confirm` 让客户选（`deploy.py:154`）。客户选 skip 即保留本地，选 overwrite
   即用模板覆盖。永不静默覆盖。

**回归**：`tests/test_monitoring.py::test_init_copies_alerts_to_local` 验证 alerts.yml 从
模板复制到本地；`test_init_preserves_user_alerts_on_re_init` 验证二次 init 不覆盖本地改动
（版本相同场景）；`test_init_prompts_on_template_version_bump` 验证版本 bump 且本地有改动
时调用了 `questionary.confirm`（mock 验证交互，不实际 ask）。

### 5. **`GRAFANA_ADMIN_PASSWORD` 用占位符 + 自动生成** —

复用 `deploy.py:_generate_secrets()` 机制：`.env.example` 里写
`GRAFANA_ADMIN_PASSWORD=<YOUR_GRAFANA_ADMIN_PASSWORD>`（与 `POSTGRES_PASSWORD` /
`REDIS_PASSWORD` 占位符风格一致），`hohu deploy init` / `hohu monitoring init` 时
自动生成 16 位字母数字密码。

GRAFANA_ADMIN_PASSWORD 加入 `PASSWORD_FIELDS` 集合（与 `POSTGRES_PASSWORD` / `REDIS_PASSWORD`
同列）。`SECRET_FIELDS` 不动（Grafana 不需要 hex secret）。

**反例**：默认 `admin/admin` → 客户忘了改 → Grafana 暴露在公网被默认凭据爆破。

**回归**：`tests/test_monitoring.py::test_grafana_password_auto_generated` 验证占位符被替换。

### 6. **`PROMETHEUS_PORT` / `GRAFANA_PORT` 通过 `.env` 暴露，默认 9090 / 3000** —

复用 deploy 已有的 `_collect_port_overrides()` 模式（`deploy.py:322`），追加两条 port_mapping：
`("PROMETHEUS_PORT", "prometheus", 9090)` / `("GRAFANA_PORT", "grafana", 3000)`。

`_update_infra_override()` 把端口写进 `docker-compose.override.yml`，跟现有 nginx/pg/redis
端口同机制。

**反例**：硬编码 `9090:9090` → 客户 9090 已被占用时改不动。

**回归**：`tests/test_monitoring.py::test_port_override_for_monitoring` 验证 .env 改
`PROMETHEUS_PORT=19090` 后 override 文件含 `19090:9090`。

### 7. **Alertmanager 留 PR-3，prometheus.yml 不预留 alerting 块** —

YAGNI。当前 alerts.yml 已含 severity 标签（critical/warning）但无处路由；Prometheus
评估规则后只在 UI 显示 firing 状态，等 PR-3 加 Alertmanager 时同步改 prometheus.yml
加 `alerting.alertmanagers` 块 + 加 alertmanager service，模板版本号 bump 触发 sync。

**反例**：prometheus.yml 预留 `# alerting:` 注释块 → 客户误以为已支持 Alertmanager，
取消注释 + 改 alertmanager 地址后 prometheus 启动失败或日志持续刷 connection refused，
错误表现"配置语法对但实际不工作"，排查路径长。

**回归**：`tests/test_monitoring.py::test_prometheus_yml_no_alerting_block` 验证
`prometheus.yml` 不含 `alerting` 关键字。

### 8. **Prometheus target 用服务名 `hohu-admin-api:8000`，不走 host network** —

profile 方案下 monitoring 跟业务栈共享 `hohu-network`（现有 compose 唯一 network），DNS
解析直接通，metrics 不暴露到 host。这是 TOB 安全底线。

**反例**：`host.docker.internal:8000` → Linux 上要 `extra_hosts: host-gateway`，且 metrics
端口暴露到 host，多 tenant 部署或共享主机时数据外泄。

**回归**：`tests/test_monitoring.py::test_prometheus_target_uses_service_name` 验证
`prometheus.yml` 中 target 为 `hohu-admin-api:8000`。

### 9. **Grafana datasource url 走服务名，不用 localhost** —

跟决策 #8 同源理。Grafana 容器内 `localhost` 指向 Grafana 自己，datasource 必须用 docker
network 里的服务名 `http://prometheus:9090` 才能解析到 prometheus 容器。

**反例**：datasource url 写 `http://localhost:9090` → Grafana provisioning 文件加载成功
（语法对），但 query 永远 connection refused，dashboard 显示"No data"，排查路径长。

**回归**：`tests/test_monitoring.py::test_grafana_provisioning_datasource` 验证
`grafana/provisioning/datasources/prometheus.yml` 中 url 为 `http://prometheus:9090` 且
`isDefault: true`。

### 10. **`monitoring_init` 复用 deploy_dir，前置依赖 `hohu deploy init` 显式提示** —

`monitoring_init` 调 `_ensure_deploy_dir()`，若 `.hohu/deploy/` 不存在会裸退（exit 1）。
客户第一次跑 monitoring 不应被这个静默失败坑到——必须把前置条件说清楚。

实现侧：在 `_ensure_deploy_dir()` 抛出的 `deploy_not_initialized` 文案基础上，monitoring
命令组在 `monitoring_not_initialized` i18n key 里显式提示"请先运行 hohu deploy init"
（不修改 deploy.py 共享函数，只在 monitoring 命令的入口 try/except 包一层）。

**反例**：客户从 README 看到 `hohu monitoring init` 直接跑，未先 `hohu deploy init` →
看到 deploy.py 通用错误"未找到部署目录" → 不知道下一步。或者更糟：客户以为 monitoring
是独立栈，自己手 mkdir `.hohu/deploy/` 导致后续模板同步路径错乱。

**回归**：`tests/test_monitoring.py::test_init_without_deploy_init_shows_hint` 验证
`.hohu/deploy/` 不存在时，stdout 含 `hohu deploy init` 字样。

## 命令设计

### `hohu monitoring init`

```python
def monitoring_init(force: bool = typer.Option(False, "--force")):
    _ensure_docker()
    try:
        deploy_dir = _ensure_deploy_dir()    # 复用 deploy.py:226
    except typer.Exit:
        console.print(f"[red]{i18n.t('monitoring_not_initialized')}[/red]")
        raise
    _ensure_env(deploy_dir)                  # 复用 deploy.py:237

    # 1. 同步模板（_sync_templates 会把 prometheus/ 和 grafana/ 子目录补到本地，
    #    prometheus.yml 是固定配置直接复制，无需渲染）
    _sync_templates(deploy_dir, force=force)

    # 2. 自动生成 GRAFANA_ADMIN_PASSWORD（若仍是占位符）
    _generate_secrets(deploy_dir / ".env")

    # 3. 提示用户改 .env 里的 PROMETHEUS_PORT / GRAFANA_PORT（如需）
    console.print(i18n.t("monitoring_init_success"))
    console.print(i18n.t("monitoring_init_hint").format(deploy_dir / ".env"))
```

### `hohu monitoring up`

```python
def monitoring_up():
    _ensure_docker()
    deploy_dir = _ensure_deploy_dir()
    _ensure_env(deploy_dir)
    _update_infra_override(deploy_dir)   # 现有 + 新加 prometheus/grafana 端口
    cmd = _compose_cmd(deploy_dir) + [
        "--profile", "monitoring", "up", "-d", "prometheus", "grafana"
    ]
    run_command(cmd, cwd=deploy_dir)
```

### 其他子命令

`down` / `logs` / `ps` / `restart` 跟 `deploy.py` 同名子命令同构，仅 compose 参数差异：

| 命令 | compose 参数差异 |
|---|---|
| `down` | `--profile monitoring stop prometheus grafana` 然后 `--profile monitoring rm -f prometheus grafana`（**不**用 `down`，见上表注） |
| `logs [-f] [services...]` | `logs [--follow] [prometheus|grafana...]` |
| `ps` | `ps prometheus grafana` |
| `restart [services...]` | `restart [prometheus|grafana...]` |

## docker-compose.yml 服务定义

追加到 `templates/deploy/docker-compose.yml`（在 nginx 之后、volumes 之前）：

```yaml
  # ============================
  # Monitoring (profile: monitoring)
  # ============================
  prometheus:
    profiles: [monitoring]
    image: prom/prometheus:v2.55.1
    container_name: hohu-prometheus
    restart: unless-stopped
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./prometheus/rules:/etc/prometheus/rules:ro
      - prometheus-data:/prometheus
    ports:
      - "${PROMETHEUS_PORT:-9090}:9090"
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://127.0.0.1:9090/-/healthy"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks: [hohu-network]

  grafana:
    profiles: [monitoring]
    image: grafana/grafana:11.3.0
    container_name: hohu-grafana
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - grafana-data:/var/lib/grafana
    ports:
      - "${GRAFANA_PORT:-3000}:3000"
    depends_on:
      prometheus:
        condition: service_healthy
        required: false
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://127.0.0.1:3000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    networks: [hohu-network]

volumes:
  # 已有 pgdata / redis-data 等
  prometheus-data:
  grafana-data:
```

**镜像版本**：固定 minor patch（`v2.55.1` / `11.3.0`，2024 Q4 baseline），不用 `latest`，
避免客户不同时间 init 拿到不兼容版本。基线偏旧有意为之——稳定性优先，等社区主流版本运行
半年以上再 bump。版本升级走模板版本号 bump（`.template-version` 0.1.13 → 0.1.14），
`_sync_templates()` 自动检测变更提示覆盖。

**Healthcheck**：与现有 postgres / redis / api / web / nginx 一致，prometheus 用内置
`/-/healthy`，grafana 用 `/api/health`。grafana 的 `depends_on.prometheus` 用
`condition: service_healthy`（`required: false` 容错），prometheus 起不来时 grafana 不
盲目启动掩盖问题。

## Prometheus 配置模板

`templates/deploy/prometheus/prometheus.yml`（固定内容，由 `_sync_templates` 直接复制到
`.hohu/deploy/prometheus/prometheus.yml`）：

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/rules/ai-tool-gateway.yml

scrape_configs:
  - job_name: hohu-admin-api
    metrics_path: /metrics
    static_configs:
      - targets: ['hohu-admin-api:8000']
        labels:
          service: hohu-admin-api
```

## Grafana Provisioning

`templates/deploy/grafana/provisioning/datasources/prometheus.yml`：

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

dashboard provisioning 留 PR-2（需要先设计 dashboard JSON 规范）。

## .env.example 变量

追加到 `templates/deploy/.env.example`：

```bash
# === Monitoring (hohu monitoring up) ===
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
GRAFANA_ADMIN_PASSWORD=<YOUR_GRAFANA_ADMIN_PASSWORD>
```

`deploy.py:PASSWORD_FIELDS` 追加 `"GRAFANA_ADMIN_PASSWORD"`，让 `_generate_secrets()` 检测到
`<...>` 占位符后自动填（与 `POSTGRES_PASSWORD` / `REDIS_PASSWORD` 走同一条路径，无需特殊
分支）。

## i18n

`hohu-cli/hohu/locales/{en,zh}.json` 追加 key（按现有 deploy_* 命名风格）：

```
monitoring_help                  / monitoring 帮助
monitoring_init_help             / 初始化监控栈配置（需先 hohu deploy init）
monitoring_init_force_help       / 强制覆盖本地修改
monitoring_init_success          / monitoring 初始化完成
monitoring_init_hint             / 如需改端口或 Grafana 密码，编辑 {env_file}
monitoring_not_initialized       / 未找到 .hohu/deploy/ 目录，请先运行 hohu deploy init
monitoring_up_starting           / 正在启动监控栈...
monitoring_up_success            / 监控栈已启动
monitoring_down_success          / 监控栈已停止
monitoring_restarted             / 监控栈已重启
monitoring_alerts_copied         / 已复制告警规则
monitoring_prometheus_copied     / 已就位 prometheus.yml
```

> `deploy_docker_not_found` / `deploy_compose_not_found` / `cmd_not_found` 等通用错误 key
> 直接复用，不重复定义。

## 测试矩阵

`hohu-cli/tests/test_monitoring.py`：

| # | 测试 | 覆盖决策 |
|---|---|---|
| 1 | `test_compose_cmd_includes_profile_flag` — `monitoring_up` 调用 mock 后 compose 命令含 `--profile monitoring` | 1 |
| 2 | `test_command_group_registered` — `hohu monitoring --help` 列出 6 个子命令 | 2 |
| 3 | `test_templates_under_deploy_dir` — 模板路径在 `templates/deploy/prometheus/` 和 `grafana/` 下 | 3 |
| 4 | `test_init_copies_alerts_to_local` — init 后 `.hohu/deploy/prometheus/rules/ai-tool-gateway.yml` 存在且内容匹配；二次 init 不覆盖本地修改 | 4 |
| 5 | `test_grafana_password_auto_generated` — `.env` 里 `GRAFANA_ADMIN_PASSWORD=<YOUR_GRAFANA_ADMIN_PASSWORD>` 被 `_generate_secrets()` 替换为 16 位字母数字 | 5 |
| 6 | `test_port_override_for_monitoring` — `.env` 改 `PROMETHEUS_PORT=19090` 后 `docker-compose.override.yml` 含 `19090:9090` | 6 |
| 7 | `test_prometheus_yml_no_alerting_block` — `prometheus.yml` 不含 `alerting` 关键字 | 7 |
| 8 | `test_prometheus_target_uses_service_name` — `prometheus.yml` 中 target 是 `hohu-admin-api:8000` 而非 `host.docker.internal` | 8 |
| 9 | `test_docker_compose_yaml_has_monitoring_services` — docker-compose.yml 含 prometheus 和 grafana 两个 service 且都标 `profiles: [monitoring]` | 1 |
| 10 | `test_grafana_provisioning_datasource` — `grafana/provisioning/datasources/prometheus.yml` 指向 `http://prometheus:9090` 且 `isDefault: true` | 9 |
| 11 | `test_init_preserves_user_alerts_on_re_init` — 客户改 `.hohu/deploy/prometheus/rules/ai-tool-gateway.yml` 后再次 `hohu monitoring init`（模板版本未变），本地文件不被覆盖 | 4 |
| 12 | `test_monitoring_down_scoped_to_services` — `monitoring_down` 调用 mock 后，compose 命令序列为 `stop prometheus grafana` + `rm -f prometheus grafana`，**不含** bare `down`（防止业务栈被误停） | 2 |
| 13 | `test_init_without_deploy_init_shows_hint` — `.hohu/deploy/` 不存在时，stdout 含 `hohu deploy init` 提示 | 10 |
| 14 | `test_init_prompts_on_template_version_bump` — 模板版本 bump 且本地 ai-tool-gateway.yml 有改动时，`_sync_templates` 调用 `questionary.confirm`（mock 验证交互调用，不实际 ask） | 4 |

测试隔离：mock `subprocess.run` / `run_command`，不实际起 docker；文件操作用 `tmp_path` fixture。

## 实施步骤

1. **写失败测试** — `tests/test_monitoring.py`（14 个测试，按测试矩阵），先全部红
2. **CLI 实现** — `hohu/commands/admin/monitoring.py`（新文件，~200 行）+ 注册到 `hohu/main.py`
3. **模板追加** — `templates/deploy/docker-compose.yml` 加 prometheus/grafana service；新建
   `templates/deploy/prometheus/prometheus.yml`（固定内容，无渲染）+ `grafana/provisioning/datasources/prometheus.yml`
4. **`.env.example` 追加 3 个变量**；`deploy.py:PASSWORD_FIELDS` 加 `GRAFANA_ADMIN_PASSWORD`；
   `deploy.py:_collect_port_overrides()` 加 prometheus/grafana 两条
5. **i18n 追加 11 个 key** 到 en/zh locales（含 `monitoring_init_help` / `monitoring_init_force_help` / `monitoring_not_initialized`，通用错误复用 deploy_* key）
6. **手动验证** — 本地 `hohu deploy up && hohu monitoring init && hohu monitoring up`，按
   下列验收门逐项核对：
   - Prometheus: 访问 `http://localhost:9090/targets`，job `hohu-admin-api` 状态 = **UP**
   - Prometheus: 访问 `http://localhost:9090/api/v1/rules`，返回 **6 条 alert rule**
     （critical 2 + warning 4，对应 alerts.yml 的 2 个 group / 6 个 alert，其中
     `AIHitlTimeoutRateHigh` + `AIRedisDownAffectingAI` 为 critical）
   - Grafana: 访问 `http://localhost:3000/api/health`，返回 200 且 `database: ok`
   - Grafana: 用生成的 `GRAFANA_ADMIN_PASSWORD` 登录，进入 Connections → Data Sources，
     点 Prometheus datasource 的 "Save & test"，返回 **Data source is working**
   - Grafana: dashboards 目录暂为空（PR-2 才加 JSON），不应报 provisioning 错误
7. **`uv run ruff check . && uv run ruff format .`** + `uv run pytest` 全绿
8. **commit + PR** — 按 memory `feedback_hotfix_no_pr.md`，本次跨 6+ 文件属"大批量 ship"，
   走 PR（不用 hotfix 直推）
9. **PR merge 后回写本 spec** — 改"状态：✅ Plan 已完成（YYYY-MM-DD）"+ 加 Ship 记录块

## 未来工作

### PR-2：Grafana dashboards

- 设计 dashboard JSON 规范（按 ai-tool-gateway 6 条告警 + 8 个 metric 设计 1-2 个 dashboard）
- `templates/deploy/grafana/provisioning/dashboards/dashboard.yml` + `dashboards/*.json`
- 客户 `hohu monitoring init` 后 Grafana 自动加载

### PR-3：Alertmanager

- 加 alertmanager service 到 docker-compose.yml（profile: [monitoring]）
- prometheus.yml 加 `alerting.alertmanagers: [{static_configs: [{targets: ['alertmanager:9093']}]}]`
- 默认配置钉钉 / 邮件接收端，由客户 .env 配置 webhook

### 不在本 spec 范围

- Postgres / Redis exporter（监控 db / cache 层）
- Loki / Tempo（日志 / trace 后端）
- Prometheus federation（多实例联邦）
- Monitoring 自身高可用（HA Prometheus 双副本 + Thanos）
