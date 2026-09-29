# 贡献指南

欢迎提交可复现的问题、文档修正和代码改进。文档以中文为主，专业术语保留英文；Issue 和 PR 可使用中文或英文。讨论围绕问题与证据，尊重其他参与者。

## 提交问题

使用仓库的 [Issue 模板](../.github/ISSUE_TEMPLATE)。提供版本或 commit、运行方式、复现步骤、预期与实际结果，以及脱敏日志。安全问题按 [安全说明](SECURITY.md) 私下联系，不在公开 Issue 上传漏洞利用细节或生产数据。

较大功能先说明使用场景和范围，避免未经讨论就重写公共契约。需求检查见 [需求与范围](PRODUCT-GUIDELINES.md)。

## 准备开发环境

应用安装和本地启动使用项目 [快速开始](../README.zh_CN.md) 中的 `hohu create`、`hohu init`、`hohu dev`。在后端仓库执行检查前安装开发依赖：

```bash
uv sync --all-extras --dev
uv run pre-commit install
```

Python 版本及依赖以 [pyproject.toml](../pyproject.toml) 为准。测试必须使用独立数据库和 Redis，见 [测试指南](TESTING-GUIDELINES.md)。

## 修改与验证

1. 从当前目标分支创建自己的工作分支。
2. 新功能或重构先写设计，确认接口、权限、租户范围和失败行为；流程见 [开发指南](DEV-GUIDELINES.md)。
3. 行为变化先补失败回归，再实现；运行相关测试、静态检查和全量测试。
4. 更新当前行为对应的正式手册；不要把聊天记录、临时报告或原始设计草案一并提交。
5. 提交 PR，说明问题、最终行为、验证结果和已知限制；跨项目改动列出配套 PR 和升级顺序。

## 提交规范与授权

使用一句英文 `type(scope): description`，例如 `fix(settings): preserve custom upload limits`。逐文件暂存、检查 diff，不附加版权消息或 `Co-Authored-By`，不跳过提交钩子。

外部贡献使用 `git commit -s` 添加 DCO `Signed-off-by`，声明有权提交该贡献；维护者提交自己拥有版权的代码可不加该尾注。DCO 不转让版权。新贡献默认遵循仓库许可证，具体规则见 [许可证政策](LICENSE-POLICY.md) 和 [LICENSE](../LICENSE)。

不要修改第三方代码的原有许可声明。许可证、凭据、生产配置和用户数据不应因文档整理而被随意替换或上传。
