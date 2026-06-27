# 架构决策记录（ADR）

> **ADR（Architecture Decision Records）** 用于记录项目中具有长期影响的技术决策：为什么选 A 不选 B、当时的约束、可预见的后果。
>
> ADR **一旦合入不再修改**（包括错别字也尽量保留）；若决策被推翻或废弃，新增一条 ADR 标记 `Status: Superseded by ADR-XXX`，原文件保持不动。这是 ADR 与 spec 内「决策 #N」最大的不同 —— spec 内的决策会随版本演进翻转，ADR 是时间胶囊。

## 何时写 ADR

| 场景 | 写不写 ADR |
|---|---|
| 选 SQLAlchemy 2.0 而非 Tortoise ORM | ✅ 写 |
| 新增一个 API endpoint | ❌ 不写（spec 内决策 #N 即可） |
| 用 Snowflake ID 而非 UUID | ✅ 写 |
| 把 `created_at` 改为 `timestamptz` | ✅ 写（已有 ADR-0003） |
| 选 NaiveUI 而非 Element Plus | ✅ 写 |
| 给某个字段加索引 | ❌ 不写 |
| 跨项目契约（响应格式、ID 序列化） | ✅ 写 |
| 修一个 bug | ❌ 不写 |

**判断标准**：决策的影响时间 > 6 个月、跨多个子项目、推翻成本高 → 写 ADR。否则写 spec 决策 #N 足够。

## ADR 与 spec 决策 #N 的关系

| 维度 | ADR | spec 决策 #N |
|---|---|---|
| 范围 | 全项目 / 跨子项目 | 单个 feature spec 内 |
| 生命周期 | 不可变（superseded 标记） | 可演进（修复后翻转 warning 块） |
| 受众 | 全员 / 未来维护者 | 实现该 feature 的人 |
| 编号 | 全局连续（ADR-0001、ADR-0002...） | 每个 spec 独立从 #1 开始 |
| 触发 | 架构选型 / 跨项目契约 | 实现细节决策 |

**例**：
- 「应用市场采用 manifest 驱动而非代码驱动」→ ADR（影响整个 marketplace 架构）
- 「重装走 apply_upgrade 而非 create_table」→ spec 决策 #74（marketplace 内部实现细节）

## 目录

<!-- ADR 索引按编号升序列。新增 ADR 时在此追加一行。 -->

| 编号 | 标题 | 状态 | 日期 |
|---|---|---|---|
| _待第一条 ADR_ | | | |

## 模板

新建 ADR 直接复制 [`0000-template.md`](./0000-template.md)。

## 命名规则

```
docs/adr/
├── README.md           # 本文件（索引）
├── 0000-template.md    # 模板
├── 0001-snowflake-id-not-uuid.md
├── 0002-jwt-hs256-not-rs256.md
└── ...
```

- 文件名：`NNNN-kebab-case-title.md`（4 位编号 + 短横线标题）
- 编号连续不跳号；废弃也占编号
- 标题用英文 kebab-case（便于排序与跨语言协作）

## 写作风格

- **中文为主，专业术语保留英文**（与所有规范文档一致）
- 单条 ADR 控制在 200 行以内；超出说明范围太广，应拆成多条
- 写「为什么」远重于「是什么」—— 实现细节看代码，ADR 存意义在于存「当时的取舍」
- 必填字段：`Status` / `Context` / `Decision` / `Consequences`
- 选填字段：`Alternatives`（被否决的方案 + 否决理由）

## 评审流程

1. 提 PR 时新增 ADR 文件
2. 至少 1 名 maintainer review
3. 合入后追加到本 README 索引表
4. 后续若推翻，**不改原文件**，新 ADR 标 `Status: Superseded by ADR-XXX`，并在原 ADR 顶部加一行 `> Superseded by [ADR-XXX](./XXX-...)（YYYY-MM-DD）`
