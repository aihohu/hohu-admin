# 产品规范（PRODUCT-GUIDELINES）

> **主视角**：产品经理
> **受众**：PM / 开发 / 设计 / 测试
> **目的**：把「Idea → Spec → 实现 → 上线 → 反馈」的需求生命周期标准化；保证每个 spec 都能被任意角色快速读懂、被任意开发者接手实现。
>
> 标杆 spec 样本：[`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)（4400+ 行，含术语表 / 决策记录 / Plan 状态块）。

---

## 1. 产品哲学

### 1.1 hohu 是什么

**hohu** 是一个开源的全栈 AI 管理平台，目标是企业级用户能像用 VS Code Marketplace / Shopify App Store 一样，按需安装业务模块（CRM / ERP / OA / HR），并让多个模块通过事件总线协同工作。

### 1.2 不做什么（边界）

| 想法 | 决定 | 理由 |
|---|---|---|
| 自建 IM / 日历 | ❌ 不做 | 已有成熟方案（钉钉 / 飞书），集成即可 |
| 重写数据库引擎 | ❌ 不做 | 用 PostgreSQL，专注应用层 |
| 强制特定云厂商 | ❌ 不做 | 默认 self-hosted，云市场 Phase 2 才上 |
| Phase 1 上可视化编排 | ❌ 推迟 | Phase 1 只跑通事件总线基础，编排 UI 是 Phase 2 |
| Phase 1 上容器化后端组件 | ❌ 推迟 | Phase 3 才上，Phase 1 低代码引擎为主 |

新需求评估时优先问「这是不是 hohu 该做的」。

### 1.3 ToB 颜色

- **多租户**：Phase 1 单租户（`tenant_id=0`），Phase 2 多租户，schema 全预留
- **权限粒度**：User → Role → Menu（CRUD 用权限码 `<module>:<resource>:<action>`）
- **数据归属**：租户数据归租户，卸载应用可保留数据（`retained_table_names`）
- **审计**：关键操作（install / uninstall / 权限变更）必留审计日志

### 1.4 开源 vs 商业版

- **开源（MIT/Apache）**：核心 + 系统模块 + 低代码引擎 + 应用市场后端
- **商业版（远期）**：云市场托管 / SaaS 多租户 / SLA 支持

新功能默认进开源版，除非 spec 显式标注「Enterprise only」。

---

## 2. 需求生命周期

```
Idea → Brainstorm → Spec → Plan → Implementation → Release → Feedback → (回到 Idea)
```

### 2.1 各阶段 entry / exit

| 阶段 | Entry | Exit |
|---|---|---|
| **Idea** | 用户反馈 / 内部讨论 / 跨模块启发 | 一句话能说清「为谁解决什么问题」 |
| **Brainstorm** | 问题清晰，但方案未定 | spec 大纲（章节列表 + alternatives） |
| **Spec** | 大纲确定 | spec 评审通过（含决策记录） |
| **Plan** | spec 通过 | 任务列表可执行（每步 2-5 分钟） |
| **Implementation** | plan 就绪 | 全量测试绿 + spec 决策回写完 |
| **Release** | 实现完成 + CHANGELOG | 发布到生产 / PyPI / npm |
| **Feedback** | 用户开始用 | 反馈进 issue tracker，回流到 Idea |

### 2.2 何时写 spec

| 改动 | 写不写 spec |
|---|---|
| 新功能（用户能感知） | ✅ 完整 spec |
| 数据模型变更 | ✅ 完整 spec |
| 状态机变更 | ✅ 完整 spec |
| 跨模块交互 | ✅ 完整 spec |
| 新 API endpoint（同模块） | ⚠️ 简版（spec 决策 #N 即可） |
| Bug fix | ❌ spec 决策 #N（含根因 + regression test） |
| 文档 / 重构 | ❌ 不写 spec |
| 依赖升级 | ❌ 不写 spec（major 升级写 ADR） |

详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md) §1.6。

---

## 3. Spec 模板

### 3.1 文件位置与命名

```
docs/
├── specs/                              # 草稿区
│   └── 2026-06-26-<feature>.md         # 日期前缀，便于按时间排序
├── <feature>.md                        # 评审通过后挪到这里（去掉日期前缀）
└── APP-MARKETPLACE.md                  # 标杆样本
```

**命名约定**：
- 草稿：`YYYY-MM-DD-<kebab-case>.md`
- 正式：`<kebab-case>.md` 或 `<UPPER_CASE>.md`（沿用既有约定）
- 一个 feature 一个文件，不要拆 `part1.md` `part2.md`

### 3.2 章节骨架

```markdown
# <Feature 名称>

> 状态：<讨论中 / 已批准 / 实现中 / 已发布> | 创建日期：YYYY-MM-DD
>
> **相关文档**：<链接到相关 spec / ADR>

## 0. 核心定位（Why）

<2-3 段。必答：
- 为谁解决什么问题（用户故事）
- 不做会怎样（业务后果）
- 为什么现在做（优先级理由）>

### 术语表

| 术语 | 定义 | 类比 |
|------|------|------|
| **<Term>** | <定义> | <类比 / 外部参考> |

**易混点辨析**：
- **A vs B**：<一句话说清差异>

## 1. 核心抽象（What）

<这个 feature 的核心数据 / 实体 / 概念。配 ASCII 图。>

## 2. 生命周期 / 流程

<核心对象的 lifecycle / 状态机 / 关键流程图。>

## 3-N. 详细设计

<按子主题拆章节。常见子主题：
- 数据模型（含建表 SQL / SQLAlchemy Model）
- API 设计（路径 / 入参 / 出参 / 错误码）
- 权限模型
- 状态机
- 并发与一致性
- 跨模块交互>

## N. 决策记录

<N. **决策名** — 理由。**反例**: ...。**回归**: ...>

## N+1. 待办与路线图

### Phase 1
- [ ] <待办>

### Phase 2
- ⚠️ Plan 2 gap：<还没做的事>

## N+2. 参考系统借鉴

| 系统 | 模式 | 借鉴内容 | 应用位置 |
|---|---|---|---|
| Odoo | ... | ... | ... |
```

### 3.3 用户故事（User Story）

每个 spec 必含至少一个用户故事。格式：

```markdown
**作为** <角色>（who）
**我想要** <动作>（what）
**以便** <目的>（why）
```

**例**（来自 APP-MARKETPLACE）：
> **作为** 企业管理员，**我想要** 一键安装 CRM 应用，**以便** 不需要开发就能用上客户管理功能。

**反例**：
- ❌ "支持应用安装"（缺 who / why）
- ❌ "作为用户我想要按钮"（why 不清晰）

### 3.4 验收标准（Acceptance Criteria）

用 Given / When / Then：

```markdown
**Scenario**: 安装已发布的低代码应用
**Given** 一个 status=published 的 app，且有 approved 版本
**When** 管理员调用 POST /marketplace/install
**Then** 返回 200 + tenant_app 记录 status=installed
**And** app_data_<slug> 表已创建，含 manifest 中声明的字段
**And** 菜单已写入 sys_menu
```

每个 spec 至少覆盖：**正向** / **边界** / **异常** / **权限拒绝** 四类场景。

### 3.5 非功能需求（NFR）

新功能必须考虑：

| 维度 | 必答 |
|---|---|
| 性能 | 关键 API 的 P99 目标（如列表 < 200ms） |
| 可用性 | 失败模式（DB 断 / Redis 断 / 第三方超时） |
| 安全 | 权限要求、敏感字段处理、输入校验 |
| 合规 | 是否涉及 PII / 审计日志 |
| 多租户 | 是否需要 tenant_id 隔离 |
| 国际化 | UI 文案是否经 i18n |

**反例**：只写 happy path 不写 NFR → 上线后才知道夜间批处理把 slave DB 打挂。

### 3.6 决策记录（核心）

每个重要决策都要写进 spec，格式：

```markdown
N. **<决策名>** — <一句话决策>。<理由：为什么这么选>。**反例**: <如果不这么做会怎样>。**回归**: <对应的 regression 测试路径>。详见 <章节>。
```

**例**（决策 #74）：
> 74. **重装走 apply_upgrade 而非 create_table** — `InstallService._create_app_tables` 单表/多表两条路径都调 `MigrationRunner.apply_upgrade`，不再直接调 `create_table`。新装时 `apply_upgrade` 内部 introspect 返回 None 退化成 `create_table`，行为不变；重装时走 introspect + `compare_schemas` + `ALTER TABLE ADD COLUMN` / `ALTER COLUMN TYPE`，v2 manifest 新增字段与 widening 才能真正落库。**反例**：直接用 `CREATE TABLE IF NOT EXISTS`，表已存在时是 no-op，新字段被静默忽略，运行时 INSERT 缺字段报错。回归测试覆盖 add column 保数据 + varchar widening 两类场景（`tests/modules/marketplace/test_install_service_lowcode.py::TestReinstallSchemaEvolution`）。详见 6.4。

**必含四要素**：
1. **决策**：选了什么
2. **理由**：为什么这么选（不只是 what，要 why）
3. **反例**：不这么做会怎样（让未来维护者不敢乱改）
4. **回归**：测试路径（让回归可被验证）

详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md) §1 Phase 4。

### 3.7 待办块（Plan 状态）

```markdown
### Phase 1：基础（已实现）
- [x] install / uninstall
- [x] 单表模式建表

### Phase 2：低代码引擎（部分实现）
- [x] 多 model 模式
- ⚠️ Plan 2 gap：v1→v2 schema 演化（已修复，见决策 #74）
- ⚠️ Plan 2 gap：uninstall 数据保留策略（待定）

### Phase 3：生态
- 事件总线
- Outbox 防堆积
```

**块翻转流程**：
1. 未完成：`⚠️ Plan X gap：<描述>`
2. 完成后：`✅ Plan X 已完成（YYYY-MM-DD）：见决策 #N`
3. spec 顶部「状态」字段同步更新

---

## 4. MVP 切分

### 4.1 为什么要切

「一次做完」是新手最常犯的错。原因：
- 大 PR review 慢，bottleneck 在 maintainer
- 上线晚，用户等不及
- 跑偏了才发现，返工成本高

切 MVP 的目标：**2 周内能有可用的 v0.1**。

### 4.2 切分原则

| 维度 | Phase 1（MVP） | Phase 2 | Phase 3 |
|---|---|---|---|
| 用户数 | 单租户 | 多租户 | SaaS |
| 功能 | 正向路径 | 边界 + 异常 | 优化 + 生态 |
| 测试 | 关键路径 | 全覆盖 | 性能 + 压测 |
| 监控 | 基础日志 | metrics + alert | tracing + profiling |
| 文档 | README | 用户手册 | 最佳实践 |

### 4.3 Phase 切分实例（marketplace）

```
Phase 1（已完成）：
  - manifest 驱动建表
  - install / uninstall
  - 单表 + 多表模式
  - 审核（人审 + AI 风险）

Phase 2（实现中）：
  - 低代码引擎
  - schema 演化（apply_upgrade）
  - 云市场分拆（HOHU_MODE）

Phase 3（规划）：
  - 事件总线 + Outbox
  - App SDK CLI
  - 可视化编排中心
  - 容器化后端组件
```

每个 Phase 必须能独立发布、独立产生用户价值。

### 4.4 反例

- ❌ 「Phase 1 把 Phase 3 的事件总线也做了」（过度设计）
- ❌ 「Phase 1 不写测试，Phase 2 补」（欠债）
- ❌ 「Phase 1 不考虑多租户，Phase 2 重构」（schema 后期改成本爆炸）

---

## 5. 跨端协同

### 5.1 同一功能的多端覆盖

新功能定义后，**先**确定要在哪几端实现：

| 端 | 默认 | 例外 |
|---|---|---|
| 后端 API | ✅ 必有 | - |
| Web (hohu-admin-web) | ✅ 默认有 | 纯 API 工具（如 webhook 接收端）例外 |
| 移动端 (hohu-admin-app) | ⚠️ 看场景 | 数据查看默认有，复杂编辑可推迟 |
| 桌面端 (hohu-admin-desktop) | ⚠️ 看场景 | 离线场景才有 |

### 5.2 功能降级

复杂功能在弱端要降级，spec 必须写明：

```markdown
**应用市场 install**:
- Web: 完整流程（搜索 → 详情 → 权限确认 → 安装）
- 移动端: 简化（搜索 → 安装，权限默认全选）
- 桌面端: 不支持（理由：桌面端场景是工作流不是市场）
```

### 5.3 i18n

**铁律**：用户可见的所有文本必经 i18n（`$t()` / `i18n.t()`）。

```typescript
// ✅ 正确
$t('page.marketplace.review.title')

// ❌ 错误：硬编码中文
'审核详情'
```

新增 i18n key 必须**同时**更新 zh-cn 和 en-us 两个文件。详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md) §3.4。

---

## 6. 用户反馈闭环

### 6.1 Issue 分类

| 类型 | 标签 | 流转 |
|---|---|---|
| Bug | `bug` + 严重度 `P0/P1/P2/P3` | 直接进当前 sprint |
| Feature request | `feature` | 进 brainstorm，spec 通过后开发 |
| Question | `question` | 24h 内回复 |
| Docs | `docs` | 随时修 |

### 6.2 反馈进 spec

```
用户反馈 → 开 issue → PM 评估 → 进 brainstorm → spec 决策 #N → 实现 → 回 issue 关闭
```

**关键**：每个 feature request 在 close 前必须有 spec 决策记录（解释为什么这么做 / 为什么不做）。

### 6.3 标签管理

```
needs-spec       还没写 spec
needs-design     还没设计
ready-to-build   可以开发了
in-progress      开发中
blocked          被阻塞（标依赖 issue #N）
```

---

## 7. 文档对外可读性

### 7.1 README

每个子项目的 `README.md` 是**用户视角**入口，不是开发视角：

```markdown
# hohu-admin

> 开源 AI 管理平台后端。

## 特性
- ✅ 应用市场（按需安装业务模块）
- ✅ 低代码引擎（JSON Schema 驱动 CRUD）
- ✅ ...

## 快速开始

```bash
uv sync
fastapi dev app/main.py
```

打开 http://localhost:8000/docs

## 文档

- [架构设计](./docs/ARCHITECTURE.md)
- [开发规范](./docs/DEV-GUIDELINES.md)
- ...

## 贡献

见 [CONTRIBUTING.md](./docs/CONTRIBUTING.md)。

## License

MIT
```

**反例**：README 一上来就讲目录结构、依赖列表、内部架构 —— 那是开发文档，不是 README。

### 7.2 用户手册（VitePress）

`hohu-admin-docs` 是面向**最终用户**的文档站点，分类：

```
docs/
├── guide/                # 入门 / 安装 / 配置
├── features/             # 各功能使用说明（含截图）
├── admin/                # 管理员操作手册
├── developer/            # 二次开发指南
└── api/                  # API 参考（从 OpenAPI 自动生成）
```

每个 feature 文档必含：
1. 截图 / 录屏（视觉优先）
2. 操作步骤（1. 2. 3. 编号）
3. 常见问题（FAQ）

### 7.3 截图规范

- 尺寸：1440x900（标准桌面）
- 命名：`<feature>-<scene>.png`（如 `marketplace-install-success.png`）
- 存放：`docs/public/images/<feature>/`
- 标注：用红框 / 箭头标关键操作点
- 不要含敏感数据（脱敏或用 mock data）

---

## 8. 反模式（Don't）

| 反模式 | 正解 |
|---|---|
| 用「功能点列表」代替用户故事 | As a... I want... so that... |
| 不写 NFR（性能 / 可用性） | 每个功能必答 |
| 决策记录只写 what 不写 why | 必含四要素（决策 / 理由 / 反例 / 回归） |
| 不切 MVP，一次做完 | Phase 1 必须可独立发布 |
| 同步多端开发，不分主次 | 后端 → Web → 移动端 → 桌面端 |
| README 讲目录结构 | README 是用户入口，讲特性 + 快速开始 |
| 用户手册只有文字无截图 | 每个功能必有截图 |
| 硬编码中文 UI | 必经 i18n |
| Bug fix 不留决策记录 | 至少加 spec 决策 #N |
| 关闭 issue 不说明 | close 时必含「已修复 / 不修复的理由 + spec 链接」 |

---

## 9. 参考

- 标杆 spec：[`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)
- 开发流程：[`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md)
- 架构边界：[`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)
- 测试要求：[`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md)
- 外部贡献：[`CONTRIBUTING.md`](./CONTRIBUTING.md)
