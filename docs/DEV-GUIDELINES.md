# 后端开发流程

本文适用于 hohu-admin 的代码和文档贡献；模块实现见 [模块开发](MODULE-DEVELOPMENT-GUIDE.md)。

## 先确定设计与验收

新功能、重构、数据模型及跨模块改动先写设计。工作草案放在 `.local/docs/specs/YYYY-MM-DD-feature.md`，分阶段计划放在 `.local/docs/plans/`；两者都不默认公开。设计需说明场景、非目标、接口、权限、租户范围、状态变化、迁移/回滚和可验证的完成条件。

完成后将当前行为整理进 `docs/` 的对应手册，必要的长期取舍放入 [ADR](adr/README.md)。不能仅更新本地草案而漏掉对外契约。文档收录规则见 [文档维护](DOCUMENTATION.md)。

决策记录格式：`N. **决策名** — 理由。**反例**: ...。**回归**: tests/path/to/test_x.py。`

## 实现与验证

1. 为行为变化添加失败回归，确认失败原因。
2. 实现最小改动，遵守 [架构与契约](ARCHITECTURE-GUIDELINES.md)。
3. 执行 `uv run ruff check .` 和 `uv run ruff format --check .`，必要时先格式化。
4. 在隔离环境执行相关测试及全量 `uv run pytest --cov=app --cov-fail-under=70`。
5. 检查迁移、权限拒绝、并发冲突和敏感数据处理；运行 `uv run python -m tools.checks.check_docs`。
6. 回写正式手册，在 PR 中记录实际验证范围；未运行的浏览器、真实 Provider 或部署验证不得写成已通过。

拼写、链接等纯文档修改检查文档和差异即可，不以重复运行数据库测试代替内容评审。

## 提交前

- 使用 `git diff` 和 `git diff --check` 检查改动，按文件名暂存。
- 提交标题使用英文 Conventional Commits；DCO 与署名规则见 [贡献指南](CONTRIBUTING.md)。
- 不提交 `.env`、数据库转储、本机路径、测试账号、运行日志和开发过程资料。
- 不覆盖他人工作区改动，不修改已经发布的迁移，不 amend 已推送的提交。
- 只在明确授权后提交或发布；普通代码修改不自动表示授权 push。
