# 贡献指南 (Contributing Guide)

感谢你考虑为本项目做出贡献！

### 贡献许可

自 v0.1.5 起，新贡献默认按 [Apache License 2.0](./LICENSE) 授权，贡献者保留版权。v0.1.4 及此前版本曾以 MIT 发布，该授权对已分发副本继续有效。请保留第三方代码的版权及许可声明。

外部贡献使用 `git commit -s` 添加 DCO `Signed-off-by`；维护者提交自己拥有版权的代码时可不添加该尾注。DCO 不等于版权转让；完整规范见 [License 与 DCO](./docs/CONTRIBUTING.md#3-license-与-dco) 和[许可证切换政策](./docs/LICENSE-POLICY.md)。

### 开发环境配置
本项目使用 [uv](https://github.com/astral-sh/uv) 进行管理：
1. Fork 并克隆仓库。
2. 安装依赖：`uv sync`。
3. 所有的开发工具（Ruff, Pytest）都已包含在内。

### 代码标准
我们使用 **Ruff** 来保持代码整洁。在提交 PR 之前，请务必运行：
- 检查逻辑：`uv run ruff check --fix .`
- 格式化：`uv run ruff format .`

### 提交 PR 流程
1. 创建功能分支 (`git checkout -b feature/amazing-feature`)。
2. 提交更改 (`git commit -s -m 'Add some amazing feature'`)。
3. 推送到分支 (`git push origin feature/amazing-feature`)。
4. 开启一个 Pull Request。
