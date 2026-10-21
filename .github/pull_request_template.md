## 📝 Description / 描述
Fixes # (issue number)

## Release Scope / 版本归属

- Target product / component version / 目标产品及组件版本：
- Target branch / 目标分支：（日常 PR 指向 `release/<version>`；版本验收及首次独立 CI 治理 PR 按主规范指向 `main`）
- Related repositories or PRs / 配套仓库或 PR：
- Compatibility, migration and recovery impact / 兼容、迁移及恢复影响：
- For a new-series merge into main: evidence that the current stable series can still receive a controlled hotfix release / 下一系列合入 main 时，当前稳定系列仍可受控发布热修复的验证记录（其他 PR 填不适用）：

Follow the [versioning policy](https://github.com/aihohu/hohu-admin/blob/main/docs/VERSIONING.md). Initial CI governance must exclude business changes and retain review, checks and authorization requirements. / 遵循版本与兼容规范；首次 CI 治理不得混入业务改动，并保留评审、检查与授权要求；纯文档修订注明适用产品系列。

## 🛠️ Type of Change / 修改类型
- [ ] 🐛 Bug fix (non-breaking change which fixes an issue) / Bug 修复
- [ ] ✨ New feature (non-breaking change which adds functionality) / 新功能
- [ ] 💥 Breaking change (fix or feature that would cause existing functionality to not work as expected) / 重大变更（破坏性修改）
- [ ] 📝 Documentation update / 文档更新
- [ ] ⚡ Performance improvement / 性能优化

## ✅ Checklist / 检查清单
- [ ] I have performed a self-review of my own code / 我已完成代码自检
- [ ] I have run `uv run ruff format .` / 我已运行 Ruff 格式化
- [ ] I have run `uv run ruff check --fix .` / 我已运行 Ruff 修复 Lint
- [ ] My changes generate no new warnings / 我的修改没有产生新的警告
- [ ] I have added tests that prove my fix is effective or that my feature works / 我已添加相关测试
- [ ] New and existing unit tests pass locally with my changes / 本地所有测试均已通过
