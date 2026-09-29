# hohu-admin 维护文档

产品使用、部署和二次开发教程统一在 [HoHu 文档站](https://hohu.org/zh/guide/user/index)维护，源码位于 [hohu-admin-docs](https://github.com/aihohu/hohu-admin-docs)。本站当前 v0.1.5 页面为开发版本，使用时核对产品版本。

## 代码贡献与维护

| 文档 | 职责 |
| --- | --- |
| [贡献指南](CONTRIBUTING.md) / [开发流程](DEV-GUIDELINES.md) | 参与方式、提交与验证要求 |
| [测试指南](TESTING-GUIDELINES.md) | 隔离环境、回归与覆盖率 |
| [架构与契约](ARCHITECTURE-GUIDELINES.md) / [ADR](adr/README.md) | 分层、可信租户边界与技术取舍 |
| [安全政策](SECURITY.md) / [AI 安全](AI-SECURITY.md) | 安全边界和漏洞报告 |
| [数据库迁移](DATABASE-MIGRATIONS.md) | 受支持升级路径、结构与数据保护 |
| [初始化内部职责](SCRIPTS-DEPLOYMENT.md) | CLI 所调用脚本的事务、幂等与权限约束 |
| [发布维护](RELEASE.md) / [AI 运维](AI-OPERATIONS.md) | 发布证据、恢复和审计工具 |
| [监控规则](monitoring/alerts.yml) | 供部署方接入的规则示例 |
| [需求与范围](PRODUCT-GUIDELINES.md) / [应用市场状态](APP-MARKETPLACE.md) | 贡献提案及当前未开放能力 |
| [许可证政策](LICENSE-POLICY.md) / [文档维护](DOCUMENTATION.md) | 授权与文档收录规则 |

## 已迁移的教程入口

[系统设置](SYSTEM-SETTINGS.md)、[多租户](MULTI-TENANCY.md)、[AI 部署](AI-DEPLOYMENT.md)、[模块开发](MODULE-DEVELOPMENT-GUIDE.md)、[按钮权限](button-permission-guide.md)、[数据范围](data-scope-guide.md)与[分页](pagination-guide.md)仅保留链接入口，正文不在两个仓库重复维护。

本仓库维护文档随代码版本保存；必要架构决策、安全契约与贡献资料继续公开。个人过程资料不作为团队唯一知识来源。
