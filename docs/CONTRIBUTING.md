# 贡献指南（CONTRIBUTING）

> 欢迎为 hohu 贡献代码、文档、想法。本文档适用于所有贡献者 —— 无论你是修一个 typo，还是新增一个完整模块。
>
> **Chinese-first project**：本仓库的文档以中文为主、专业术语保留英文。Issue / PR / commit message 用英文（便于国际贡献者参与）。

---

## 1. 项目愿景

**hohu** 是一个开源的全栈 AI 管理平台：核心是一个通用的 FastAPI + Vue 3 管理后台框架，用户通过应用市场按需安装业务模块（CRM / ERP / OA / HR），并支持多模块通过事件总线协同工作。

详见 [`ARCHITECTURE.md`](./ARCHITECTURE.md) 与 [`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)。

## 2. 行为准则

我们承诺为每个人提供友好、安全、欢迎的社区环境 —— 不分经验水平、性别、性别认同、性取向、残疾、外貌、体型、种族、民族、宗教信仰、国籍。

- ✅ 对事不对人
- ✅ 接受建设性批评
- ✅ 关注社区利益
- ❌ 不使用色情/暴力内容
- ❌ 不人身攻击
- ❌ 不公开他人私密信息

违反者 maintainer 有权屏蔽 / 封禁。完整准则见 [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md)（如暂无，参照 [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/)）。

---

## 3. License 与 DCO

### 3.1 License

本仓库代码默认采用 **MIT License** 发布（各子项目 `LICENSE` 文件为准）。

贡献的代码默认在 MIT 下发布，你保留版权。

### 3.2 DCO（Developer Certificate of Origin）

每个 commit 必须包含 `Signed-off-by` 行，表示你确认你有权以本仓库 License 贡献这段代码：

```bash
git commit -s -m "..."
# 自动加：Signed-off-by: Your Name <your.email@example.com>
```

DCO 是 Linux 内核、Git 等大型开源项目采用的标准轻量贡献者协议。它不是 CLA —— 你不需要签任何文件，只需在每个 commit 加一行。

** certify 内容**（同 [developercertificate.org](https://developercertificate.org/)）：
> The contributor certifies that they have the right to submit the patch under the project's license.

### 3.3 PR 检查

- PR 缺 `Signed-off-by` → CI fail，阻塞合并
- 用 `git rebase --signoff` 批量补 sign（仅 PR 未合并前）

---

## 4. 找一个贡献入口

### 4.1 新手友好

- 标签 [`good first issue`](https://github.com/aihohu/hohu-admin/labels/good+first+issue)：小改动，2 小时内可完成
- 标签 [`help wanted`](https://github.com/aihohu/hohu-admin/labels/help+wanted)：社区帮助 wanted，欢迎认领
- 标签 [`documentation`](https://github.com/aihohu/hohu-admin/labels/documentation)：文档相关

### 4.2 进阶

- 标签 [`bug`](https://github.com/aihohu/hohu-admin/labels/bug)：修 bug
- 标签 [`feature`](https://github.com/aihohu/hohu-admin/labels/feature)：实现新功能
- 标签 [`performance`](https://github.com/aihohu/hohu-admin/labels/performance)：性能优化

### 4.3 非代码贡献

- 文档（VitePress 站点、API 参考）
- 翻译（i18n，详见 §8）
- 截图 / 录屏
- 复现 issue 帮忙定位
- 在社区答疑

---

## 5. 开发流程

### 5.1 准备环境

```bash
# Fork 仓库到你自己的 GitHub 账号，然后：
git clone https://github.com/<your-username>/hohu-admin.git
cd hohu-admin

# 添加上游
git remote add upstream https://github.com/aihohu/hohu-admin.git

# 安装依赖
uv sync

# 启动开发服务器
fastapi dev app/main.py
```

### 5.2 创建分支

```bash
git checkout -b feature/<short-name>
# 或: fix/<short-name> / chore/<short-name>
```

不要在 `main` 上直接改 —— 你的 PR 会被拒。

### 5.3 写代码

详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md)：

- 后端必跑 `ruff check . && ruff format .`
- 前端必跑 `pnpm lint && pnpm typecheck`
- 写测试（覆盖率门禁 70%）
- 跑全量测试

### 5.4 提交

```bash
git add <specific-files>      # 不要 git add -A
git commit -s -m "feat(marketplace): add uninstall data export"  # 一句话英文 + Signed-off-by
git push origin feature/<short-name>
```

### 5.5 开 PR

GitHub 上开 PR 到 `aihohu/hohu-admin:main`，模板见 §6。

### 5.6 评审

- 至少 1 名 maintainer review（小改动）
- 2 名 maintainer review（跨模块 / 数据模型变更）
- CI 必绿（含 ruff / pytest / coverage / lint / typecheck）
- 至少 1 个 approver → squash merge

---

## 6. PR 模板

```markdown
## Summary

<1-3 句话说明这个 PR 做了什么、为什么。>

## Related Issue

Closes #<issue-number>
<!-- 或: Refs #<issue-number> -->

## Type of Change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactor / chore

## Spec / Design

<如果是新功能或重构，链接到 spec：>
- Spec: [docs/<feature>.md](./docs/<feature>.md) §<章节>
- 决策记录: #<N>

## Test Plan

- [ ] 新增测试: `<test-path>`
- [ ] 全量测试通过: `pytest` / `pnpm test`
- [ ] 手动验证: <步骤>
- [ ] 覆盖率 ≥ 70%

## Checklist

- [ ] 代码遵循 [DEV-GUIDELINES.md](./docs/DEV-GUIDELINES.md)
- [ ] 测试遵循 [TESTING-GUIDELINES.md](./docs/TESTING-GUIDELINES.md)
- [ ] 如适用，spec 已回写决策记录
- [ ] commit msg 英文一句话
- [ ] 每个 commit 含 `Signed-off-by`（DCO）
- [ ] 不含敏感数据（.env / 密钥）
```

---

## 7. Issue 模板

### 7.1 Bug Report

```markdown
**Describe the bug**

<清晰简短地描述 bug 是什么。>

**To Reproduce**

1. 进入 '...'
2. 点击 '...'
3. 看到 '...'

**Expected behavior**

<应该发生什么。>

**Actual behavior**

<实际发生了什么。>

**Environment**

- OS: [e.g. Ubuntu 22.04]
- Browser: [e.g. Chrome 120]
- hohu-admin version: [e.g. 0.1.0]
- Node / Python version: [...]

**Logs / Screenshots**

<相关日志、截图、stack trace。>
```

### 7.2 Feature Request

```markdown
**Is your feature request related to a problem?**

<描述痛点。如：安装应用时无法预览权限...>

**Proposed solution**

<你希望怎么做。>

**Alternatives considered**

<其他方案 + 否决理由。>

**Additional context**

<截图、参考实现、相关 issue。>
```

### 7.3 标签约定

| 标签 | 含义 |
|---|---|
| `bug` / `feature` / `documentation` / `question` | 类型 |
| `P0` / `P1` / `P2` / `P3` | 严重度（仅 bug） |
| `good first issue` | 适合新手 |
| `help wanted` | 欢迎社区认领 |
| `needs-spec` / `needs-design` | 还没准备好开发 |
| `ready-to-build` / `in-progress` | 状态 |

---

## 8. 文档贡献

### 8.1 VitePress 站点（`hohu-admin-docs`）

面向最终用户的文档，目录：

```
docs/
├── guide/            # 入门
├── features/         # 功能使用
├── admin/            # 管理员手册
├── developer/        # 二次开发
└── api/              # API 参考
```

修改后跑：
```bash
cd hohu-admin-docs
pnpm dev
```

### 8.2 翻译

i18n 文件位于各子项目的 `locales/` 或 `src/locales/`：

- `hohu-admin-web/src/locales/langs/{zh-cn,en-us}.json`
- `hohu-cli/hohu/locales/{zh,en}.json`

新增 key 必须**同时**更新两个文件。详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md) §3.4。

### 8.3 API 文档

FastAPI 自动从代码生成 OpenAPI（`/docs`）。改 API 时**不要**手动改文档站点的 API 参考 —— 它会自动同步。

---

## 9. 模块作者指南

如果你开发的是可分发的应用市场模块（而不是核心代码贡献），详见 [`MODULE-DEVELOPMENT-GUIDE.md`](./MODULE-DEVELOPMENT-GUIDE.md)。

模块开发的关键步骤：
1. `hohu create-module <name>` 脚手架
2. 写 manifest（`app.json`）
3. 后端 Model / Schema / Service / API
4. 前端 views / api / store
5. `hohu dev` 本地联调
6. `hohu publish` 发布到应用市场

详见上述文档。

---

## 10. 评审流程（maintainer 视角）

### 10.1 评审优先级

1. **P0 bug fix**：立即 review
2. **good first issue PR**：24h 内 review（鼓励新手）
3. **feature PR**：2-3 天内 review
4. **文档 / 重构**：1 周内 review

### 10.2 评审 checklist

详见 [`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md) §8。重点：
- 功能正确性
- 代码质量
- 性能
- 安全
- 文档同步

### 10.3 合并策略

- **默认 squash merge**：PR 多 commit 合为一个，commit msg 用 PR 标题
- **里程碑 PR**：merge commit 保留完整历史
- **不允许**：直接 push 到 main

---

## 11. 社区

- **Issue**：[github.com/aihohu/hohu-admin/issues](https://github.com/aihohu/hohu-admin/issues)
- **Discussion**：[github.com/aihohu/hohu-admin/discussions](https://github.com/aihohu/hohu-admin/discussions)（想法、问答）
- **邮件**：maintainers@aihohu.com（私有 / 安全问题）

### 11.1 安全漏洞

**不要**通过公开 issue 报告安全漏洞。邮件 security@aihohu.com，SLA 见 [`SECURITY.md`](./SECURITY.md)。

---

## 12. 致谢

感谢所有贡献者。每次发布会在 release notes 列出本次贡献者 GitHub 用户名。

---

## 13. 参考

- 开发规范：[`DEV-GUIDELINES.md`](./DEV-GUIDELINES.md)
- 测试规范：[`TESTING-GUIDELINES.md`](./TESTING-GUIDELINES.md)
- 架构边界：[`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)
- 发布流程：[`RELEASE.md`](./RELEASE.md)
- 行为准则：[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md)（如无，参照 Contributor Covenant 2.1）
