# 应用市场与低代码能力状态

本文说明当前源码边界，供评估扩展能力的使用者和贡献者阅读。

**当前主应用未注册 Marketplace、Contributes 或 Lowcode 数据路由。** 仓库中保留相关模型、服务和测试，不能据此认为发布部署已开放应用市场或支持安装第三方业务插件。

可核对 [主应用入口](../app/main.py)、[模块代码](../app/modules/marketplace) 和 [能力守卫](../app/modules/marketplace/capability.py)。能力守卫仅允许 single 模式下默认租户进入旧能力；它本身不会注册路由，hosted 模式仍拒绝。

## 不应当作为当前部署能力使用的设计

- 云端市场与本地执行分离，或 `HOHU_MODE=cloud/local/hybrid` 部署方式。
- Python 插件包自动发现、热加载、上传即执行，以及跨模块事件总线协议。
- hosted 租户的 Marketplace/Lowcode 安装、运行和升级流程。

这些需要独立设计、实现、安全审查与发布说明。历史方案中的 manifest、目录和命令不是当前公开稳定接口。

需要扩展现有系统时，按 [后端模块开发](MODULE-DEVELOPMENT-GUIDE.md) 在源码中开发和测试，通过配套版本发布。将来开放应用市场时，应重新交付可复现的安装、升级、权限、隔离及故障恢复手册，而不是直接发布原始设计稿。
