# AI 安全边界

面向部署者和 AI 工具贡献者。AI 默认启用，但登录成功不等于获得 Agent、模型或工具访问权。

## 权限与数据访问

AI 入口使用显式权限；Agent 使用 Role-Agent 绑定，模型使用租户模型策略，工具使用精确的功能权限及数据范围。`shared` 标记和超级管理员角色不是 Agent/Tool 的通用授权旁路。

Gateway 在执行前检查 tenant、owner、当前授权和工具声明。历史消息、工具结果、文件及跨轮引用也需按当前权限重新投影，不能因为曾经显示过就永久可访问。后端拒绝是授权依据，Prompt 或前端隐藏不能替代授权。

系统 Agent 管理复用默认租户的系统超级管理员会话；Provider/模型目录等平台维护 API 仍使用独立平台身份。管理操作记录真实操作者、原因、工单与关联 ID，避免将提示词正文或密钥写入审计。

## 执行与人工确认

破坏性操作、声明必须确认的工具和相应风险检测结果进入人工确认。Gateway 绑定确认对象、参数、执行身份与业务快照，并在批准后复验授权和有效状态，防止换参、重复执行和撤权后继续执行。

确认编排与具体模型生成的话术分离；两阶段工具的执行入口由 Gateway 控制。原理见 [ADR-0002](adr/0002-gateway-owned-confirmation-flow.md)。人工确认只代表批准，不承诺后台任务持久执行；相关边界见 [ADR-0001](adr/0001-ai-safety-consistency-before-deferred-execution.md)。

工具开发需区分只读、幂等和实际副作用；输出经统一脱敏，面向客户端的展示数据不应无条件塞入模型上下文。运行 `uv run python -m tools.checks.check_ai_tools` 检查内置工具接入规则。

## 文件与 Provider

私有附件及导出结果按 tenant/owner 鉴权。扩展名、MIME、解析资源预算、路径和所有权共同约束文件访问；上传成功不表示可以公开读取或被任意工具使用。

Provider 连接走统一 [出站控制](../app/modules/ai/core/provider_egress.py)，包括允许列表、DNS/IP 验证、超时、响应大小、并发与重试边界。恶意输入检测不能证明 Prompt 注入已被完全解决；权限、数据范围和执行确认始终需要独立生效。

## 紧急停用

将部署配置 `AI_MODULE_ENABLED=false` 后重启服务。关闭时 AI 业务组件不初始化，`/ai/**` 与 `/platform/ai/**` 返回 HTTP 503 和 `AI_MODULE_DISABLED`；核查非 AI 功能仍正常。

该开关是部署熔断，不是日常用户授权方式。恢复前定位问题、撤销受影响凭据或权限，并验证拒绝路径。漏洞报告统一遵循 [安全说明](SECURITY.md)。

## 核验入口

- [认证与入口权限](../app/core/auth.py)
- [Gateway](../app/modules/ai/agents/gateway)
- [静态检查](../tools/checks/check_ai_tools.py)
- [AI 回归测试](../tests/modules/ai)
- [部署指南](AI-DEPLOYMENT.md)
