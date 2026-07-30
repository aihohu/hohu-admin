# Grafana Dashboard Provisioning（PR-2）

> 状态：✅ Plan 已完成（2026-07-30） | 创建日期：2026-07-30
>
> 配套 [monitoring CLI 集成方案](./2026-07-29-monitoring-cli-design.md) PR-1，补齐"Grafana
> 启动后客户第一眼能看到什么"。PR-1 已 provisioning datasource，本 spec 在此基础上加
> dashboard JSON，让客户 `hohu monitoring up` 后**零配置**看到 AI Tool Gateway 全貌。
>
> **相关文档**：hohu-admin `app/modules/ai/metrics.py`（8 个 metric 定义）；
> [`monitoring/alerts.yml`](../monitoring/alerts.yml)（6 条告警规则）。

## Ship 记录

| 项 | 值 |
|---|---|
| 实施 commit | `hohu-cli@da2a9d4`（与 PR-1 同 commit，按 `feedback_no_bump_before_release.md` 偏好合并发布）|
| spec commit | `hohu-admin@1b914d1` |
| 实施日期 | 2026-07-30 |
| 决策数 | 7 |
| 测试数 | 8（`tests/test_monitoring.py::test_dashboard_*`，编号 15-22）|
| 新增 dashboard | 2（`ai-tool-gateway-overview.json` 8 panel + `hitl-health.json` 6 panel）|
| datasource 补丁 | `datasources/prometheus.yml` 加 `uid: hohu-prometheus`（让 dashboard 稳定引用）|
| 验证 | `uv run pytest` 22/22 全绿 |
| 待客户验证 | 真机 `hohu monitoring up` 后 Grafana UI Dashboards 列表能看到 2 个 dashboard；panel 在 metric 有数据时显示曲线 |

### Ship-time 决策补充

- **未单独 bump 版本**：PR-2 与 PR-1 共用 0.1.14，未发布前不 bump（按 `feedback_no_bump_before_release.md`）。
- **dashboard JSON 内联**：未使用 library panel，2 个 dashboard 各自独立 ~200-300 行 JSON。
- **schemaVersion 39**：对齐 Grafana 11.3.0（PR-1 pinned 版本），不依赖 11.4+ 字段。
- **客户升级路径**：dashboard `editable: false`，客户想改 UI 上 Duplicate 副本；hohu-cli 模板版本 bump 时 `_sync_templates` 触发 confirm。

## 背景

PR-1 ship 后客户反馈：datasource 配好了但 dashboard 是空的，要自己研究 PromQL + Grafana
panel 编辑器才能看到曲线，门槛高。本 spec 直接 ship 2 个开箱即用的 dashboard JSON，对应
hohu-admin v1.5+ 暴露的 8 个 metric 与 6 条告警规则。

## 参考借鉴

| 项目 | 是否借鉴 | 说明 |
|---|---|---|
| Grafana official dashboard schema（v1，Grafana 11.x 兼容） | ✅ | schema 字段对齐 Grafana 11.3.0（PR-1 pinned 版本），不依赖 11.x 新特性 |
| hohu-admin `app/modules/ai/metrics.py` | ✅ | 8 个 metric 名 / label / type 是 dashboard PromQL 的唯一来源 |
| `monitoring/alerts.yml`（6 条告警规则） | ✅ | dashboard 阈值线与告警阈值对齐（失败率 30%、超时率 10%、pubsub 丢失率 1%） |
| grafana.com 社区 dashboard | ❌ | hohu-admin 的 `ai_*` 是自定义 metric，社区没有对应模板 |
| Grafana auto-query（LLM 生成 panel） | ❌ | 需配置外部 LLM，airgapped 客户不可用 |

## 决策记录

### 1. **2 个 dashboard，而非 1 个或 3+** —

按"告警聚类 + 客户心智"维度切：

| Dashboard | Panel 数 | 覆盖告警 | 受众 |
|---|---|---|---|
| **AI Tool Gateway Overview** | 8 | 全部 6 条（全景） | 运维 / 业务负责人 |
| **HITL Health** | 6 | 2 条 critical + 1 条 warning（HITL 聚类） | 开发 / 紧急排障 |

**反例**：
- 合并成 1 个 dashboard（14 panel）→ 一屏看不下，客户滚动疲劳，定位慢。
- 拆 3+ 个（按 metric 类别切）→ HITL 4 个 metric 跨 dashboard 看，紧急排障要切窗口。

**回归**：`tests/test_monitoring.py::test_dashboards_provisioned` 验证
`provisioning/dashboards/*.json` 文件存在且 dashboard.yml 指向同目录。

### 2. **JSON schema 用 Grafana 11.x 兼容字段，不依赖新特性** —

PR-1 pin 的 grafana 镜像是 `11.3.0`，dashboard JSON 不使用 11.4+ 字段（如 `panels[].options.asyncQuery`）。
版本升级走 PR-1 已有的 `.template-version` bump 流程，与本 spec 解耦。

schema 字段最小集：
- `title` / `uid`（uid 固定，让客户跨环境导入不冲突）
- `schemaVersion: 39`（Grafana 11.x stable schema）
- `panels[]`：`type` / `title` / `datasource` / `targets[]` / `gridPos`
- `templating.list`：变量（`$tool` / `$execution_mode`）让客户能筛
- `time`：默认 `now-1h` → `now`
- `refresh`: `30s`（与 scrape_interval 15s 对齐，避免空查询）

**反例**：用 schemaVersion 50（Grafana 13.x 字段）→ Grafana 11.3 加载报 schema 不兼容，dashboard 不显示，且日志刷 error。

**回归**：`tests/test_monitoring.py::test_dashboard_json_schema_version` 验证每个 JSON
的 `schemaVersion` 字段 ≤ 39。

### 3. **panel 类型与数据形态匹配** —

| 数据形态 | panel 类型 | 用法 |
|---|---|---|
| 单值比率（失败率/超时率） | `stat` | 加阈值颜色（绿/黄/红），客户一眼看出告警是否触发 |
| 时间序列 QPS / latency | `timeseries` | 多 series 堆叠或重叠 |
| 离散类别分布 | `piechart` 或 `bargauge` | status / risk / event_type |
| Histogram 分位数 | `timeseries` + `histogram_quantile` | P95 / P99 |

**反例**：所有 panel 都用 `timeseries` → 失败率这种单值指标画成线，客户看不出当前是否触发阈值；状态分布画成线，category 颜色乱。

**回归**：`tests/test_monitoring.py::test_dashboard_panel_types_match_data` 验证每个
stat panel 的 query 含 `clamp_min` / 比率计算（即真的在算百分比，不是 raw counter）。

### 4. **阈值线对齐 alerts.yml，让客户在 dashboard 上"看见告警"** —

每个 `stat` panel 加 `thresholds` 步进：

| Panel | threshold steps | 数据源 |
|---|---|---|
| 失败率 | `<30 green, <50 yellow, else red` | `AIToolFailureRateHigh` 30% |
| HITL 超时率 | `<10 green, <20 yellow, else red` | `AIHitlTimeoutRateHigh` 10% |
| Pub/Sub 丢失率 | `<1 green, <5 yellow, else red` | `AIPubSubMessageLossRateHigh` 1% |
| 当前 HITL 挂起数 | `<5 green, <20 yellow, else red` | 无对应告警，经验值 |

**反例**：阈值与 alerts.yml 不一致 → 客户在 dashboard 看到绿色以为没问题，但 Prometheus 已经 firing。

**回归**：`tests/test_monitoring.py::test_dashboard_thresholds_match_alerts` 验证
stat panel 阈值与 alerts.yml 表达式数字一致（30 / 10 / 1）。

### 5. **provisioning `editable: false`，客户改 dashboard 走"duplicate → 保存"路径** —

JSON 由 hohu-cli 模板版本管理，客户改了之后 `hohu monitoring init` 升级版本时会被覆盖
（与 ai-tool-gateway.yml 同步规则一致，spec PR-1 决策 #4）。客户想长期保留定制 →
在 Grafana UI 里 Duplicate 当前 dashboard，复制版独立于 provisioning，不受 sync 影响。

provider yml 配置：
```yaml
disableDeletion: false      # 客户可删（删后下次 init 又补回）
editable: false             # 客户改 JSON 改不动（保护模板）
updateIntervalSeconds: 30   # 改 JSON 文件后 30s 内热加载
```

**反例**：`editable: true` → 客户在 UI 改了 panel → 下次 hohu monitoring init 模板
版本 bump 时被静默覆盖 → 客户困惑"我的改动哪去了"。

**回归**：`tests/test_monitoring.py::test_dashboard_provider_not_editable` 验证
`dashboard.yml` 中 `editable: false`。

### 6. **dashboard JSON 单文件，不拆 row 文件 / library panel** —

每个 dashboard 一个 JSON 文件（约 200-300 行），所有 panel 内联。Grafana 支持 library
panel（多 dashboard 共享），但当前 2 个 dashboard 的 panel 不重合，无共享需求。

**反例**：用 library panel → 需要 `library_panels.json` 单独 provisioning，模板结构复杂；
当前 2 个 dashboard 完全不重合，library 收益为零。

**回归**：`tests/test_monitoring.py::test_dashboard_no_library_panels` 验证 JSON 不含
`libraryPanel` 字段。

### 7. **uid 固定，跨环境导入不冲突** —

每个 dashboard JSON 写死 `uid`（如 `hohu-ai-tool-gateway-overview`）。客户 import 自己
环境时 Grafana 用 uid 判重，避免 provisioning + 手动 import 重复。

**反例**：JSON 不写 uid → Grafana 自动生成随机 uid → 客户 import 同名 JSON 后变成两个 dashboard，provisioning 那个又被下一次 sync 覆盖回去，状态分裂。

**回归**：`tests/test_monitoring.py::test_dashboard_uid_stable` 验证每个 JSON 含 `uid`
字段且匹配 `^hohu-[a-z-]+$`。

## Dashboard 设计

### Dashboard 1: AI Tool Gateway Overview

`uid: hohu-ai-tool-gateway-overview`，folder: `hohu`，refresh: 30s

| # | Panel | PromQL | Type |
|---|---|---|---|
| 1 | 工具 QPS（按 tool，top 10） | `topk(10, sum by (tool) (rate(ai_tool_calls_total[5m])))` | timeseries |
| 2 | 失败率 % | `100 * sum(rate(ai_tool_calls_total{status=~"failed\|internal_error\|repeated_failure"}[5m])) / clamp_min(sum(rate(ai_tool_calls_total[5m])), 0.001)` | stat (阈值 30/50) |
| 3 | 状态分布 | `sum by (status) (rate(ai_tool_calls_total[5m]))` | piechart |
| 4 | 风险等级分布 | `sum by (risk) (rate(ai_tool_calls_total[5m]))` | piechart |
| 5 | 执行模式（autonomous/hitl） | `sum by (execution_mode) (rate(ai_tool_calls_total[5m]))` | timeseries (stacked) |
| 6 | 调用延迟 P95（按 tool） | `histogram_quantile(0.95, sum by (le, tool) (rate(ai_tool_call_duration_seconds_bucket[5m])))` | timeseries |
| 7 | 配额拒绝（按 level） | `sum by (level) (rate(ai_quota_rejected_total[5m]))` | bargauge |
| 8 | 安全事件（按 type） | `sum by (event_type) (rate(ai_security_events_total[5m]))` | bargauge |

变量：`$tool`（label_values(ai_tool_calls_total, tool)）、`$execution_mode`

### Dashboard 2: HITL Health

`uid: hohu-hitl-health`，folder: `hohu`，refresh: 30s

| # | Panel | PromQL | Type |
|---|---|---|---|
| 1 | 当前挂起数 | `sum(ai_hitl_pending_count)` | stat (阈值 5/20) |
| 2 | HITL 超时率 % | `100 * sum(rate(ai_hitl_timeout_total[5m])) / clamp_min(sum(rate(ai_tool_calls_total{execution_mode="hitl"}[5m])), 0.001)` | stat (阈值 10/20) |
| 3 | 超时趋势（按 mode） | `sum by (mode) (rate(ai_hitl_timeout_total[5m]))` | timeseries |
| 4 | Pub/Sub 丢失率 % | `100 * sum(rate(ai_hitl_pubsub_lost_total[5m])) / clamp_min(sum(rate(ai_hitl_wake_total{mode="redis_pubsub"}[5m])), 0.001)` | stat (阈值 1/5) |
| 5 | Wake 结果分布 | `sum by (result) (rate(ai_hitl_wake_total[5m]))` | piechart |
| 6 | Redis 故障影响 | `sum(rate(ai_tool_calls_total{status="redis_down"}[5m]))` | timeseries |

变量：`$mode`（label_values(ai_hitl_timeout_total, mode)）

## 模板路径

```
hohu-cli/hohu/templates/deploy/grafana/provisioning/
├── dashboards/
│   ├── dashboard.yml                    # provider 配置（已存在，PR-1 已 ship）
│   ├── ai-tool-gateway-overview.json    # 新增
│   ├── hitl-health.json                 # 新增
│   └── .gitkeep                         # PR-1 已有
└── datasources/
    └── prometheus.yml                   # PR-1 已 ship
```

`dashboard.yml` 已在 PR-1 ship，本 spec 不改其结构（`options.path` 指向同目录）。

## 测试矩阵

`hohu-cli/tests/test_monitoring.py` 追加：

| # | 测试 | 覆盖决策 |
|---|---|---|
| 15 | `test_dashboards_provisioned` — `provisioning/dashboards/*.json` 存在 ≥ 2 个文件 | 1 |
| 16 | `test_dashboard_json_schema_version` — 每个 JSON `schemaVersion` ≤ 39 | 2 |
| 17 | `test_dashboard_panel_types_match_data` — stat panel 的 query 含 `clamp_min` 或 `/`（比率计算） | 3 |
| 18 | `test_dashboard_thresholds_match_alerts` — 失败率 panel threshold 含 30，超时率含 10，pubsub 含 1 | 4 |
| 19 | `test_dashboard_provider_not_editable` — `dashboard.yml` 含 `editable: false` | 5 |
| 20 | `test_dashboard_no_library_panels` — JSON 不含 `libraryPanel` 字段 | 6 |
| 21 | `test_dashboard_uid_stable` — 每个 JSON `uid` 匹配 `^hohu-[a-z-]+$` | 7 |
| 22 | `test_dashboard_targets_prometheus_datasource` — panel 的 `datasource.type == "prometheus"`，不依赖具体 name（用 UID 或 type） | 2 |

## 实施步骤

1. **写失败测试** — `tests/test_monitoring.py` 追加 8 个测试（#15-#22）
2. **实现 2 个 dashboard JSON** — `templates/deploy/grafana/provisioning/dashboards/{ai-tool-gateway-overview,hitl-health}.json`
3. **`uv run ruff format && ruff check && pytest`** 全绿
4. **手动验证** — 本地 `hohu monitoring up`，浏览器打开 Grafana → Dashboards，应看到 `hohu` folder 下 2 个 dashboard；点开各自 panel 有数据（先跑几次 AI 工具调用产生 metric）
5. **commit + PR** — 与 PR-1 同走 PR 流程，**不单独 bump `.template-version` / `pyproject.toml` 版本号**（PR-1 已 bump 到 0.1.14 且尚未发布，本次 PR-2 与 PR-1 合并发布）
6. **PR merge 后回写 spec** — 改"✅ Plan 已完成（YYYY-MM-DD）"

## 未来工作

### PR-3：Alertmanager + 告警接收端

- `alertmanager` service 加入 monitoring profile
- prometheus.yml 加 `alerting.alertmanagers` 块
- 默认 webhook 接收端（钉钉 / 飞书 / 邮件），客户在 .env 配
- Grafana dashboard 加 "Active Alerts" panel 显示 firing 告警

### PR-4：业务层 metric 扩展

- hohu-admin 增加 `auth_login_total` / `business_kv_*` 等业务侧 metric
- 配套 dashboard：User Activity / Business Metrics

### 不在本 spec 范围

- 自定义 panel plugin
- Grafana reporting（定时 PDF 邮件）
- Grafana alerting（用 Grafana 自己的告警，与 Prometheus alerts.yml 重复，不引入）
