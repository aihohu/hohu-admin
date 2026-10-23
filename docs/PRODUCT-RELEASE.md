# 配套发布清单

`release.json` v1 描述一组同号 Backend/Web 的源码、镜像、支持平台、升级路径和验收证据。组件 Release 成功不等于产品组合已通过验收；只有最终产物、安装、升级、恢复及 AI 写一致性证据齐备，才发布产品清单。

字段定义以 [JSON Schema](../tools/ops/schemas/product-release-v1.schema.json) 为准。[校验器](../tools/ops/check_product_release.py) 补充跨字段规则，[来源核验器](../tools/ops/product_release_sources.py) 读取官方来源。Web 使用 Backend Release 上的同一文件，不维护第二份契约。

## 文件与支持范围

- 最终清单使用 UTF-8，无 BOM；`schema_version` 为整数 `1`，`status` 为 `released`。重复键、未知字段、空白占位、非有限数字、类型转换均拒绝。文件最多 1 MiB。
- 尚未完成的资料保存为 `release.draft.json`；它不因改名而取得发布资格。最终 `published_at` 是产品完成时间（UTC、以 `Z` 结束），重试时必须保留原字节。
- Backend/Web 与 `product_version` 完全同号；每端恰好一个官方 GHCR primary。完整版本 tag、index digest、各平台 manifest digest 必须一致；mirror 使用同一构建的完整映射和 qualification。
- 两端 `verified` 平台集合必须相同且非空。构建出来但未完整实测的平台标为 `unverified`；构建支持 arm64 不代表产品已承诺 arm64。
- 每个支持平台都必须有 `pair_install`、`upgrade`、`recovery`、`ai_write_consistency` 四类配对证据；两端各有 `component_ci`。全部证据绑定两个源码 SHA；配对证据同时绑定两个 primary index digest 和平台。源码 CI 的 digest/platform 为 null；产物 CI 仅填写被测组件的 index digest 和已构建平台。
- 0.1.6 的升级来源只允许实际验证的 0.1.5。CLI 最低候选为 0.1.16，minimum 必须在 tested_versions 中；不承诺未列出的未来 CLI。方法为 `manual_digest_override`，不代表 CLI 自动解析清单。App/Desktop 均为 `not_released`，verified 数组为空。
- Docs series 匹配产品 major.minor，snapshot_url 固定已审阅的 Docs commit。限制说明不能豁免必需门禁，也不能包含凭据或业务数据。

`tests/fixtures/product_release/` 中的数据是合成的规则测试样本，其 SHA、报告和版本组合不是已发布产品的证据。

## 离线与在线校验

在 Backend checkout 中安装锁定的开发依赖（`uv sync --locked --dev`），执行：

```bash
uv run --locked python -m tools.ops.check_product_release .local/artifacts/release.draft.json
```

离线校验成功输出 `offline: passed`、`online: not_run`。未完成草稿应失败；离线通过也不能证明镜像真实存在、报告确实通过或业务可以部署。

在线核验额外需要已登录官方 GitHub 的 `gh`、可用的 Skopeo、网络和本地临时目录。只读核验不需要业务数据库或业务凭据：

```bash
uv run --locked python -m tools.ops.check_product_release .local/artifacts/release.json \
  --online --work-dir .local/tmp/product-verification
```

若原构建启用了 ACR，必须为该清单的每个 ACR 镜像显式增加 `--allow-mirror 完整仓库名`。只接受已有同步工具允许的 Alibaba Cloud 域名及匹配组件名；清单不能自行扩充来源白名单。

核验包括：

1. 官方仓库附注 tag 的 commit 和原 Actions run；main 上 Release workflow 的原 attempt 1 成功构建及适用发布资格任务。组件公开 Release 的 `component-build.json`、`component-delivery.json` 必须匹配原 run、提交、镜像和摘要。
2. 固定源码的版本、CHANGELOG 和锁文件。Backend 检查 uv.lock 根包版本；pnpm lockfile 没有应用自身版本字段，Web 检查根 importer 存在，并依赖同 SHA 的完整前端 CI 校验依赖/构建。
3. Backend 实际 Alembic 源码链有唯一目标 head，无缺失父节点、环或断开的分支。使用 AST 读取元数据，不运行迁移文件。
4. 每个主镜像及 mirror 的完整 tag、index、运行平台 manifest 和匿名完整镜像下载；下载后再次核对 tag。失败、冲突、漂移、权限或来源未知都停止。
5. Docs 快照和操作说明可读取；证据报告的原始字节 SHA256 匹配清单。官方永久来源限定为 `aihohu/hohu-admin`、`hohu-admin-web`、`hohu-admin-docs`、`hohu-cli` 的固定 SHA blob 或稳定 Release 附件。不接受任意外站、临时 Actions 日志/过期下载链接或带凭据/查询参数的 URL。

在线成功输出 `online: passed`；失败返回非零。工具只核对来源和字节，不执行报告中的命令，也不自动判断报告正文的测试质量。发布负责人须保留工具版本、实际命令、来源版本/产物、观察结果和范围，另一位复核者在正式发布前检查报告内容、支持承诺及脱敏。散列与同处一份 Release 的附件不是独立密码学签名。

## 不可覆盖上传与恢复

[上传工具](../tools/ops/upload_product_release.py) 只接受命名为 `release.json` 的完整清单，每次都先进行上述在线核验，再上传至 Backend 对应的已公开正式组件 Release：

```bash
uv run --locked python -m tools.ops.upload_product_release .local/artifacts/release.json \
  --work-dir .local/tmp/product-upload
```

同样用 `--allow-mirror` 显式批准实际 ACR 仓库。此命令属于正式发布操作，需按[发布规范](RELEASE.md)授权；校验命令本身不会上传。Web Release 和用户文档由维护者链接 Backend 同号 `release.json`。

- 同名附件不存在才写入一次；完整列表不可读取或达到分页上限时停止。
- 已存在且下载后字节完全相同，视为成功；内容不同或同名重复则停止。无 clobber、删除、覆盖或自动改写 Release 正文。
- 上传成功和回执丢失都必须重新读回。读回同字节才确认；不存在、不同或未知时本次失败，不自动再发一次。
- 使用固定临时快照上传，并检查输入文件在核验期间未被更改。故障后保留原始文件及 `published_at`，重新调用时先核验/查询，不能重新生成一份时间不同的清单。
- 上传器不创建 Tag/Release、不触发构建、不推进系列/latest、不补造缺失证据。

已公开清单出现错误，保留原始记录，通过公开纠错/撤回记录和新产品版本处理；不能以覆盖历史文件替代纠错。
