# 配置、设置与任务字段控制

本文说明 Backend 0.2.0 开发版本的配置、设置与任务接口。HTTP 请求释放原读取事务后，交由独立所有者重新验证身份、字段策略和租户范围。受控准备目录现为十对象、144 字段、69 动作；历史四对象、七对象目录保留原摘要。准备目录不等于生产激活资格，完整交付与独立验收门仍然关闭。通用授权前提见[内核契约](FIELD-POLICY-CORE.md)，关系入口见[关系字段读取](RELATION-FIELD-PROJECTION.md)。

## 目录与动作

`app.modules.system.field_policy.control_catalog.control_catalog()` 在七对象目录上增加 `system.config`、`system.setting` 和 `job.definition`，共十对象、144 字段。历史目录仍独立保留。设置字段采用稳定的实际 key，不接受通配符、camelCase 别名或统一的 `setting_value` 授权项。内部 AI 设置、固定安全设置、邮件和平台控制面不进入租户角色目录；`auth:default_password` 只允许更新，没有读取、查询或导出用途。

字段许可仍须与动作功能权限相交，缺失许可即拒绝。普通配置修改没有 `is_public` 写许可；发布需独立的 `system:config:publish` 和编辑权限。普通任务修改仅覆盖名称、备注；调度、状态、运行分别使用专用动作，任务 key 创建后不可改。普通设置保存不能改账户安全、上传约束或默认密码；注册角色选择还需角色授权功能权限。候选动作声明不替代真实所有者的委派、安全上限或锁证明。

编辑读取应使用对应列表或设置分组的读取动作；专用写动作本身不额外授予表单读取。导入创建、导入精确匹配和导出分别声明用途，不从新增、修改或读取推导。`next_run_time` 不可独立授予；`project_next_run()` 仅在状态、触发类型、cron、间隔值及间隔单位全部可读时返回可信所有者计算的值与五项来源。任一来源拒绝时省略整个派生单元格，合法的空时间仍保持为空。

## 动态值与公开内容指纹

`control_values.prepare_value()` 支持 `unknown`、`private`、`secret`。只有完整、显式的原子文本替换可将未知旧值分类为 private；敏感 key 或已有 secret 均保持 secret，改名不能解除保护。`******` 仅代表保持原值，不能用作新配置的实际值。支持的类型为 text、richtext，每值最多 8192 个 UTF-8 字节；结构 JSON、文件来源、NUL、非法编码或未知类型拒绝。richtext 验证不提供 HTML 净化能力。

`project_value()` 只提供分类和类型上限，不能代替当前读取授权。`publication_digest()` 仅接受启用的 private 普通文本，将租户、配置 ID、key、值、类型、状态、分类和 schema 修订绑定到 SHA256；`publication_matches()` 还要求当前记录已公开并与观测登记完全匹配。没有登记、分类未知、secret、内容或元数据变化均不匹配。指纹和 `PublicationFact` 都不是发布凭据；真实所有者必须从受保护的数据库取得登记事实并独立核验当前动作权限。

## 严格输入与导入准备

HTTP 使用 `ControlConfigCreate` / `ControlConfigUpdate` 的闭集契约，拒绝未知字段、内部分类/发布字段、空修改及非法 null。`is_public=true` 要求走发布动作，内置设置 key 不走普通配置协议；修改时最终 key、类型、旧分类及实际字段足迹仍按数据库当前行复验。

`prepare_control_import()` 仅准备调用方提供的 XLSX bytes，不读取数据库、不提交事务。表头固定为 `config_name, config_key, config_value, config_type, config_group, status, is_public, remark`；额外列、重复或错序表头、公式、非法类型和公开请求拒绝。布尔值只接受 bool、整数 0/1、字符串 true/false/0/1，空值为 false，不使用字符串真值转换。类型和状态仅在空单元格时补默认值。

文件最多 10 MiB，并通过 ZIP/XML 预检；最多 1000 个物理数据行、单值 8 KiB、规范记录合计 1 MiB。解析不信任工作表声明尺寸，按真实单元格检查列数和行数，避免静默截断。解析使用四个 worker、队列 32、3 秒等待；超时仍占有执行中的 worker，工作簿在 finally 关闭。冻结的准备结果仅是输入，不具有授权或可信持久来源。

## TaskSpec 和固定域配置

`ClosedTaskArgs` 及所有嵌套模型必须 strict、frozen、extra=forbid；拒绝开放 dict、任意 JSON、递归 schema 及主体、租户、角色等身份字段，包括内部名称与别名。`TaskSpec` 显式登记 key、翻译 key、作用域、参数模型、async 函数及固定服务协议；函数须接受一个位置参数。默认时限 30 秒，上限 60 秒，重试最多三次。注册表不可变、最多 256 项，未知 key 或作用域不符拒绝。

`prepare_task_args()` 同时限制原始输入和默认值展开后的 JSON：8192 字节、深度 8、节点 1024。重复键、NaN、非有限数字及非法 Unicode 拒绝。`call_arguments()` 重新校验绑定到同一 TaskSpec 的准备数据，仅返回一个独立的位置 dict；None、空字符串和空对象统一为该调用形态，不依赖 args/_args 关键字。公开 schema 去除默认值和描述，不使用函数 docstring；登记后模型漂移拒绝。这些对象不创建身份上下文，不保存创建者角色，不提供执行授权或 execute 方法。

`builtin_task_contracts()` 精确绑定当前测试任务及三个导入/导出过期清理函数；租户任务仅有无业务能力的 `test_task`，其余固定为平台保留任务。普通 TaskRegistry 注册不会取得排程或执行资格。历史未知任务保留原参数，允许修改名称、备注和停用、删除，不能启用、运行或修改调度。定时执行按固定服务能力验证当前租户与任务，不继承创建者角色。

`ai.schemas.control_config` 已接入平台写入、探测和实际出站检查。Provider 扩展配置为空闭集，Model 仅允许 generation 下的 max_tokens、temperature。新输入未知键拒绝，历史未知配置整值隐藏；仅修改元数据不会重写原配置，未知扩展不能取得执行能力。读取使用 SQL 闭集投影，api_key 只有 configured 布尔，未知 JSON 与密钥不进入管理响应。固定配置上限 64 KiB、深度 8、节点 1024。邮件与平台保留各自真实身份、功能权限和审计协议，不进入租户字段目录。

## HTTP 与旧客户端

Config、Job 列表保留 `records/total/current/size`，附页级和行级 `fieldPolicy`；无权字段缺键，合法可见 null 保留。查询用途在原租户范围上证明，先于过滤、计数与分页。Job 不返回旧 create_by/update_by 自由文本。

`GET /system/config/form` 与 `GET /system/job/form` 接受 `mode=create|update`；update 必须有字符串 `recordId`，create 禁止目标。响应包含稀疏 record、fieldPolicy 和最长五分钟的 formReceipt。Config/Job 新增、普通修改及 Settings 分组保存必须携带当前 receipt，缺失、过期或上下文变化由服务端拒绝；伪造版本请求头不能代替。receipt 只证明表单协议，所有者仍按实际提交字段重新授权。普通修改只发送 dirty 字段，写而不可读的字段仅在明确替换时提交。

Settings values 的 key 保持原拼写，不递归 camelCase。保存只接受当前分组的 dirty 键，revision 绑定完整原事实及默认定义，采用数据库侧指纹与 HMAC，不向响应暴露秘密。注册角色选择器必须证明完整候选集合和角色名称来源；不完整时关闭选择器，保存另验注册资格与委派上界。默认密码不读取，空输入保持原值；秘密元数据也按命令能力过滤。

Config 的 `PUT /system/config/{config_id}/publication` 接受 confirmed=true 与 expectedRevision，后者来自有完整发布权限及读取、修改足迹的表单 publicationRevision。`DELETE` 同路径只需 expectedRevision，使用表单独立的 revocationRevision，按 unpublish 锚点动作撤回，无需读取隐藏内容。两种证明绑定当前身份、G/B、目录、目标与登记，最长五分钟，不能互换。值、key、类型、状态或分类改变会在同一事务撤销旧登记；恢复旧值不会自动恢复公开资格。

公开自定义配置每次新鲜验证租户、分类、发布登记和精确摘要，不复用旧 public 缓存。历史 is_public=true 不构成新发布资格；历史未知分类值继续保存但不读取、导出或公开。只有显式合法完整替换才能按新协议分类，secret 不因改名降级。

Job 普通 update 只收名称、备注；`PUT /system/job/schedule` 使用专用调度命令，status/run 保留专用入口。registered 只返回真实 TaskSpec 的安全标签和闭集参数 schema。AI cron 修改也验证调度字段写权限，预览与结果中的旧 cron、名称须有当前读取来源。

Job 的 `specialUpdate` 汇总当前可用的调度、状态字段，普通 `update` 仍仅表示名称、备注；这些提示不替代实际命令校验。表单查询拒绝未知参数，任务 ID 接受规范的十进制字符串，拒绝布尔值、空白及前导零。

SMTP 测试采用准备、锁外发送、完成三个阶段。两个数据库阶段各限八秒和 4096 次 SELECT；发送最多十五秒。完成段重新验证当前身份和配置，在 G-S/B-S 与 Mail 行锁下按配置版本写测试时间、状态和审计，不推进授权 epoch。

## Dispatch 与维护

手动运行的 Redis 消息为服务端签名封套，最多 2048 字节、最长 30 秒，绑定租户、任务、当前用户安全版本、G/B、目录、配置摘要及单次 dispatch_id。监听端拒绝旧 tenant/job 裸消息；验签只用于定位，claim 在新事务内重新验证当前身份、功能和字段许可，再持久化 nonce 与原过期时间。明确收到 claim 提交 ACK 后才调用任务，任务函数运行时不持数据库会话或 G/B。

同一 nonce 至多一次认领，日志在原期限内或运行状态下不能删除。claim、完成或通知 ACK 未知不会自动重做任务。完成与孤儿回收仅收口原运行事实。直接合并运行与独立 scheduler 共用这些所有者；reload 按租户获取有界快照，释放事务后才修改 APScheduler。

独立 scheduler 与合并模式均启动孤儿日志恢复监控，维护停写及正常退出会等待其停止；API 单独模式不承担 scheduler 的恢复身份。跨租户恢复扫描整体限六十秒，各租户使用独立短事务；超时不会重执业务，已经完成的租户收口事实保留。

运行入口的 `notificationStatus=notified` 只表示 Redis 确认存在订阅者，不能证明任务已执行；零订阅者、连接失败或超时返回 `unconfirmed`。响应不暴露封套，也不自动重发数据库命令或任务。

数据库迁移增加值分类、发布登记和 dispatch 防重放事实，不推断旧数据来源。一般 downgrade 固定拒绝，恢复须停写并还原整套一致性备份。受控目录迁入只接受精确历史来源、完整租户集合、从未生效且无 active 的准备状态；旧规则以新修订保留，新增对象不自动授予，重复执行不推进版本。直接启动与 Docker Compose 均须沿[受控安装](CONTROLLED-INSTALL.md)验证实际进程、后台任务、防重入和数据库在途事务；Ctrl+C 不能代替维护证据。

受控安装接受本系列已知的 `e020f2a00001`、`e020f2b00001` 前置 head 升级到 `e020f2c00001`；未知或分叉 head 继续拒绝，不能用 `stamp` 代替迁移。

## 错误与兼容边界

非法值使用 `CONFIG_VALUE_SCHEMA_UNREGISTERED`，不安全公开内容使用 `CONFIG_PUBLICATION_UNSAFE`；普通输入公开请求使用 `CONFIG_PUBLICATION_REQUIRED`，内置 key 使用 `BUILTIN_SETTING_PROTECTED`。导入格式使用 `CONFIG_IMPORT_INVALID_XLSX`，容量超限使用 `AUTHORIZATION_PLAN_LIMIT_EXCEEDED`；任务声明不合格或漂移使用 `JOB_ARGUMENT_SCHEMA_REQUIRED`，参数不合格使用 `JOB_ARGUMENT_INVALID`。错误不带原始值。

普通表单升级使用 `FIELD_POLICY_PROTOCOL_REQUIRED`，发布证明变化或过期使用 `CONFIG_PUBLICATION_CONFLICT`；dispatch 的签名、期限或当前事实不匹配分别拒绝，错误不带原参数。新客户端须与 Backend 同组合安装，旧整对象表单不作为写兼容客户端。日志与 AI 全文、文件来源及 Web 完整交付另有验收要求，不能据此文档重新开放生产激活。
