# 配置与任务字段内部候选契约

本文面向后端维护者，说明配置、设置和任务的纯 Python 候选接口。`control_catalog()`、值分类、导入准备、TaskSpec 及固定域投影均未接入当前 HTTP、事务所有者或 scheduler；当前受控准备目录仍是七对象。使用这些函数不代表新增对象已取得生产激活资格，也不能改变旧 TaskRegistry 的执行行为。通用授权前提见[内核契约](FIELD-POLICY-CORE.md)，当前关系入口见[关系字段读取](RELATION-FIELD-PROJECTION.md)。

## 目录与动作

`app.modules.system.field_policy.control_catalog.control_catalog()` 在七对象目录上增加 `system.config`、`system.setting` 和 `job.definition`，共十对象、144 字段。历史目录仍独立保留。设置字段采用稳定的实际 key，不接受通配符、camelCase 别名或统一的 `setting_value` 授权项。内部 AI 设置、固定安全设置、邮件和平台控制面不进入租户角色目录；`auth:default_password` 只允许更新，没有读取、查询或导出用途。

字段许可仍须与动作功能权限相交，缺失许可即拒绝。普通配置修改没有 `is_public` 写许可；发布需独立的 `system:config:publish` 和编辑权限。普通任务修改仅覆盖名称、备注；调度、状态、运行分别使用专用动作，任务 key 创建后不可改。普通设置保存不能改账户安全、上传约束或默认密码；注册角色选择还需角色授权功能权限。候选动作声明不替代真实所有者的委派、安全上限或锁证明。

编辑读取应使用对应列表或设置分组的读取动作；专用写动作本身不额外授予表单读取。导入创建、导入精确匹配和导出分别声明用途，不从新增、修改或读取推导。`next_run_time` 不可独立授予；`project_next_run()` 仅在状态、触发类型、cron、间隔值及间隔单位全部可读时返回可信所有者计算的值与五项来源。任一来源拒绝时省略整个派生单元格，合法的空时间仍保持为空。

## 动态值与公开内容指纹

`control_values.prepare_value()` 支持 `unknown`、`private`、`secret`。只有完整、显式的原子文本替换可将未知旧值分类为 private；敏感 key 或已有 secret 均保持 secret，改名不能解除保护。`******` 仅代表保持原值，不能用作新配置的实际值。支持的类型为 text、richtext，每值最多 8192 个 UTF-8 字节；结构 JSON、文件来源、NUL、非法编码或未知类型拒绝。richtext 验证不提供 HTML 净化能力。

`project_value()` 只提供分类和类型上限，不能代替当前读取授权。`publication_digest()` 仅接受启用的 private 普通文本，将租户、配置 ID、key、值、类型、状态、分类和 schema 修订绑定到 SHA256；`publication_matches()` 还要求当前记录已公开并与观测登记完全匹配。没有登记、分类未知、secret、内容或元数据变化均不匹配。指纹和 `PublicationFact` 都不是发布凭据；真实所有者必须从受保护的数据库取得登记事实并独立核验当前动作权限。

## 严格输入与导入准备

独立的 `ControlConfigCreate` / `ControlConfigUpdate` 拒绝未知字段、内部分类/发布字段、空修改及非法 null，不替换旧 HTTP Schema。`is_public=true` 要求走发布动作，内置设置 key 不走普通配置协议；修改时最终 key、类型、旧分类及实际字段足迹仍须按数据库当前行复验。

`prepare_control_import()` 仅准备调用方提供的 XLSX bytes，不读取数据库、不提交事务。表头固定为 `config_name, config_key, config_value, config_type, config_group, status, is_public, remark`；额外列、重复或错序表头、公式、非法类型和公开请求拒绝。布尔值只接受 bool、整数 0/1、字符串 true/false/0/1，空值为 false，不使用字符串真值转换。类型和状态仅在空单元格时补默认值。

文件最多 10 MiB，并通过 ZIP/XML 预检；最多 1000 个物理数据行、单值 8 KiB、规范记录合计 1 MiB。解析使用四个 worker、队列 32、3 秒等待；超时仍占有执行中的 worker，工作簿在 finally 关闭。冻结的准备结果仅是输入，不具有授权或可信持久来源。

## TaskSpec 和固定域配置

`ClosedTaskArgs` 及所有嵌套模型必须 strict、frozen、extra=forbid；拒绝开放 dict、任意 JSON、递归 schema 及主体、租户、角色等身份字段，包括内部名称与别名。`TaskSpec` 显式登记 key、翻译 key、作用域、参数模型、async 函数及固定服务协议；函数须接受一个位置参数。默认时限 30 秒，上限 60 秒，重试最多三次。注册表不可变、最多 256 项，未知 key 或作用域不符拒绝。

`prepare_task_args()` 同时限制原始输入和默认值展开后的 JSON：8192 字节、深度 8、节点 1024。重复键、NaN、非有限数字及非法 Unicode 拒绝。`call_arguments()` 重新校验绑定到同一 TaskSpec 的准备数据，仅返回一个独立的位置 dict；None、空字符串和空对象统一为该调用形态，不依赖 args/_args 关键字。公开 schema 去除默认值和描述，不使用函数 docstring；登记后模型漂移拒绝。这些对象不创建身份上下文，不保存创建者角色，不提供执行授权或 execute 方法。

`builtin_task_contracts()` 精确绑定当前测试任务及三个导入/导出过期清理函数；租户候选仅有无业务能力的 `test_task`，其余固定为平台保留任务。新注册旧任务不会自动进入该候选。

`ai.schemas.control_config` 的候选 Provider 扩展配置为空闭集，Model 仅允许 generation 下的 max_tokens、temperature。新输入未知键拒绝，旧异常配置整值隐藏；固定配置上限 64 KiB、深度 8、节点 1024。候选函数不替换当前 `redact_config` 或已有输出 Schema。

## 错误与兼容边界

非法值使用 `CONFIG_VALUE_SCHEMA_UNREGISTERED`，不安全公开内容使用 `CONFIG_PUBLICATION_UNSAFE`；普通输入公开请求使用 `CONFIG_PUBLICATION_REQUIRED`，内置 key 使用 `BUILTIN_SETTING_PROTECTED`。导入格式使用 `CONFIG_IMPORT_INVALID_XLSX`，容量超限使用 `AUTHORIZATION_PLAN_LIMIT_EXCEEDED`；任务声明不合格或漂移使用 `JOB_ARGUMENT_SCHEMA_REQUIRED`，参数不合格使用 `JOB_ARGUMENT_INVALID`。错误不带原始值。

候选没有数据库读写、迁移或运行接线，也没有新增 HTTP 路径。现有策略摘要、激活屏障、七对象准备目录及旧输入/输出路径保持原有契约；不能仅凭这些纯函数将目录切换为十对象或重新开放生产激活。
