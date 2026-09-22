# 开源许可证政策

## 默认许可与版本边界

hohu 官方开源项目采用 Apache License 2.0 作为默认许可政策，另有声明的代码除外。具体适用范围以各仓库中的许可证和版权声明为准。

`hohu-admin` 与 `hohu-admin-web` 自 v0.1.5 起默认采用 Apache-2.0。v0.1.4 及此前已按 MIT 发布的版本继续适用原授权，历史提交、标签和发布包不改写。CHANGELOG 中的 Unreleased 条目不表示版本已经发布。

继承代码的原 MIT 文本保存在 `LICENSE-MIT`。保留该文件不表示所有新增代码都可任选 MIT 或 Apache-2.0；已经授出的 MIT 权利继续有效。

## 版权与第三方代码

贡献者保留版权。第三方代码继续适用其原许可证，原有源码中的版权及许可声明须保留。

Web 基于 SoybeanAdmin 开发。上游 MIT 原文及适用范围见 Web 仓库的 `THIRD_PARTY_NOTICES.md`，许可证参考来源为上游提交 `8c111df85c39ae691eaf8fc71c4a1d05f76dfa6f`。该来源用于固定许可原文，不表示所有继承文件都来自同一版本，也不代表上游作者额外授出了 Apache 专利许可。

依赖包按各自许可证授权。项目归属声明不替代依赖清单及第三方许可合规检查。

## 分发要求

| 文件 | 用途 |
|---|---|
| `LICENSE` | 完整 Apache-2.0 标准文本 |
| `LICENSE-MIT` | 继承代码的原 MIT 许可及版权声明 |
| `NOTICE` | 项目归属与授权范围说明 |
| Web `THIRD_PARTY_NOTICES.md` | SoybeanAdmin 许可原文、来源和适用范围 |

后端 wheel 和 sdist 必须包含 `LICENSE`、`LICENSE-MIT` 和 `NOTICE`，由 `pyproject.toml` 中的 `license-files` 指定。

Web 根目录的四个许可文件是维护来源，`public/licenses/` 中保存对应发布副本。变更时须同步副本并保持内容一致；Vite 将它们复制到 `dist/licenses/`，分发构建产物时保留该目录。

## 贡献与提交

新贡献默认按 Apache-2.0 提交，另有声明的代码除外。外部贡献使用 DCO `Signed-off-by`；维护者提交自己拥有版权的代码时可不添加该尾注。DCO 不等于版权转让。

提交消息使用一句英文，采用 `type(scope): description` 格式，不附加版权或 `Co-Authored-By` 信息。仅提交正式维护文档和必要 ADR；开发过程中的 specs、plans、reports、验收提示词及临时文件保留本地。正式文档不得依赖未提交的过程记录。

## 决策记录

1. **采用 Apache-2.0 宽松许可** — 支持企业采用、二次开发及商业扩展，并明确贡献者专利授权条款。**反例**: 将许可证误解为禁止第三方商业托管或闭源扩展。**回归**: 核对 LICENSE 标准文本、README 和包许可元数据一致。
2. **保留历史与上游 MIT 声明** — 新的默认政策不撤销既有权利，也不覆盖第三方归属。**反例**: 替换根 LICENSE 后删除 SoybeanAdmin 的版权声明。**回归**: 对照历史 MIT 文本和固定上游许可来源检查继承声明。
3. **许可随构建产物分发** — 使用者在获取源码、Python 包或 Web 构建产物时均能取得适用许可。**反例**: 仅在 README 链接上游仓库，发布包没有 MIT 原文。**回归**: 检查 wheel / sdist 及 Web dist/licenses 的文件内容与根目录一致。

## 参考

- [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
- [MIT License](https://opensource.org/license/mit)
- [SoybeanAdmin 上游许可证](https://github.com/soybeanjs/soybean-admin/blob/8c111df85c39ae691eaf8fc71c4a1d05f76dfa6f/LICENSE)
