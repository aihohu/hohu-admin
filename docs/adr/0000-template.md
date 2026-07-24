# ADR-NNNN: <决策标题>

> **说明**：复制本文件到 `NNNN-<kebab-case-title>.md`，替换所有 `<...>` 占位符，删除本说明段。
>
> 编号取下一个连续数字（参考 `README.md` 索引表的最大值 +1）。

- **Status**: Proposed  <!-- Proposed → Accepted → Deprecated / Superseded -->
- **Date**: YYYY-MM-DD
- **Deciders**: <姓名 / 角色，逗号分隔>
- **Tags**: <backend / frontend / database / security / marketplace / ...>

## Context（背景）

<问题描述 + 触发本次决策的契机。两到三段。

必答：
- 我们面对的是什么问题？
- 有哪些约束（技术、组织、时间、合规）？
- **不**解决会怎样？

可选：相关 spec / 历史 ADR / 业界对比。>

## Decision（决策）

<我们选择了什么。一句话开头，然后展开。

必须明确到「能被代码验证」的程度 —— 例如「采用 Snowflake ID」不够，「所有主键 `Mapped[BigInteger]` + `default=next_id`，JSON 序列化为字符串」才够。>

## Alternatives Considered（备选方案）

### 备选 A: <方案名>

<简述方案>。

- ✅ <优点>
- ❌ <缺点 / 否决理由>

### 备选 B: <方案名>

<同上>。

## Consequences（后果）

### 正面

- <带来的好处>

### 负面 / 已知 trade-off

- <代价 / 局限 / 未来要补的债>

### 后续行动

- [ ] <需要立即跟进的事项（如更新 spec、补测试、迁移数据）>
- [ ] <...>

## References（参考）

- 相关 spec: [`docs/<feature>.md`](../<feature>.md) §<章节>
- 相关 ADR: [ADR-XXX](./XXX-...)
- 外部资料: <链接 / 书籍 / 论文>

---

## 决策记录（事后追加，原文不动）

<!-- 这一段是 ADR 特有的「时间胶囊」：决策上线后的实际效果、踩过的坑、是否需要补充。
     每条记录标日期；不要修改原文 Context / Decision。 -->

- **YYYY-MM-DD**: <事后观察 / 补充说明 / 已知问题>
