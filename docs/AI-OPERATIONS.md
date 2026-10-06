# AI 平台维护与审计

本文面向后端维护者。产品部署与配置正文已迁到[文档站](https://hohu.org/zh/guide/operations/ai)；本文件维护独立平台身份、审计工具和代码切换的约束。

## 平台运维工具

日常部署使用 CLI；下面的后端工具仅供维护人员在明确目标环境和权限后使用。先查看对应命令帮助，再按环境执行，不能复制历史测试账号或令牌：

```bash
uv run python -m tools.ops.platform_principal --help
uv run python -m tools.ops.platform_ai --help
uv run python -m tools.ops.audit_tenant_isolation --help
uv run python -m tools.ops.audit_ai_provider_egress --help
uv run python -m tools.ops.audit_data_scope_union --help
```

`platform_principal` 管理其余运维接口使用的独立平台主体；`platform_ai` 使用 `HOHU_SYSTEM_ACCESS_TOKEN` 中的普通系统超级管理员 access token 管理 Agent、Provider/模型和租户模型授权。模型配置网页入口为「AI 管理 → 模型管理」，不创建独立模型维护账号。全局管理操作需要 reason、ticket 和 correlation；权限替换不是普通租户管理入口。

隔离审计报告需绑定准确构建 SHA，检查租户归属、引用、唯一约束和 namespace。DataScope 切换审计比较旧/新范围：先停止 writer、复核报告并确认精确 hash，切换过程持有维护锁；报告变化必须重新复核。切换失败时工具不会替部署者安全恢复 writer，须先验证旧构建再恢复流量。具体参数及校验以 [审计工具](../tools/ops/audit_data_scope_union.py) 为准，不能将报告当作无条件放行依据。

[监控规则](monitoring/alerts.yml)是部署示例，需按实际指标和接收方配置，不会自动部署监控系统。发布证据与恢复要求见[发布维护](RELEASE.md)，授权边界见[AI 安全](AI-SECURITY.md)。
