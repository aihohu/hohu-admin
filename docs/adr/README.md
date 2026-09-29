# 架构决策记录

ADR 记录长期影响的背景、选择、备选方案与后果，不代替当前操作手册。实现计划、任务清单和验收日志按 [文档维护规则](../DOCUMENTATION.md) 保存。

| 编号 | 决策 | 状态 | 日期 |
| --- | --- | --- | --- |
| 0001 | [AI 延迟执行前先完成安全与一致性闭环](0001-ai-safety-consistency-before-deferred-execution.md) | Accepted | 2026-08-06 |
| 0002 | [AI 操作确认编排由 Gateway 统一负责](0002-gateway-owned-confirmation-flow.md) | Accepted | 2026-08-07 |
| 0003 | [可信租户上下文与隔离边界](0003-trusted-tenant-context-and-isolation-boundaries.md) | Accepted | 2026-08-31 |

## 新增与维护

使用 [模板](0000-template.md)，文件名为 `NNNN-kebab-case-title.md`。说明为什么作出选择、被拒绝的替代方案以及成本。编号不复用，评审通过后更新索引和公开文档清单。

已接受决策的含义不能通过编辑悄悄改变；推翻时新增 ADR，并在旧记录标记后继决策。允许修正链接、拼写和移除过程附件，保留决策背景与取舍，原始历史由 Git 保存。

2026-09-28 文档整理仅调整记录的引用与过程附录，未改变已接受决策；当前能力以相应正式手册和代码为准。
