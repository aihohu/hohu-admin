# 系统设置、内置文案与运行配置

状态：✅ Plan settings 已完成（2026-09-24）；✅ Plan E 已完成（2026-09-26）。

## 2026-09-26：菜单、权限与存储拆分

✅ Plan E 已完成（2026-09-26）：系统设置与自定义参数独立菜单、API、权限和数据表；系统设置使用分组 Tabs。

- `sys_setting` 保存内置设置及保留命名空间的内部策略；`sys_config` 只保存自定义参数。设置值保留文本编码，类型仍由代码目录校验，避免迁移时改变已有值和内部 JSON 策略。
- 新表使用 Snowflake setting_id、强制 tenant_id、唯一 `(tenant_id, setting_key)`、setting_value、status 和 UTC 时间；不开放任意设置键的 CRUD。
- Alembic 将既有内置键及原来已经保留的 ai:/security:/upload: 命名空间逐租户搬迁，保留值和启停状态；普通自定义参数不动。迁移不依赖运行时目录导入。回滚恢复配置值。
- `/system/setting` 与 `system:setting:list/edit` 独立；`/system/config` 与原权限继续管理自定义参数。新权限不自动授予原自定义参数管理员，系统超级管理员仍可访问；租户管理员遵循既有租户边界。
- `/system/setting/public`、`/runtime` 提供内置公开值与认证能力；原 `/system/config/public` 只返回公开自定义参数。所有内置消费者、初始化及缓存同步切换。
- Web 新增 `/system/setting`；原 `/system/config` 展示自定义参数。六个分组使用 Tabs，每组独立保存；切换保留草稿，路由离开/关闭窗口提示未保存内容，URL query.tab 保存当前分组。
- CLI 继续通过 Alembic + 幂等初始化自动部署，不增加手工搬迁步骤。

7. **设置与自定义参数独立存储** — 两者的所有权、权限和生命周期不同。**反例**: 删除业务参数影响系统登录或上传策略。**回归**: 存储隔离、迁移值保留、权限拒绝测试。
8. **Tabs 保留分组草稿** — 切换设置类别不应丢失未保存输入；仅保存当前分组。**反例**: 修改品牌后查看上传限制，返回时修改消失。**回归**: 设置页 Tab/URL/离开保护组件测试。

### 拆分验证记录（2026-09-26）

- 后端 Ruff check/format、diff check 通过；隔离测试库全量 **2,890 passed**，覆盖率 **79.77%**，保留既有 33 条测试警告。
- 新迁移通过空库安装、升级/降级往返、跨租户值与启停状态保留、自定义参数不迁移、拆分后的新值在回滚时保留等验证。
- Web lint/format/typecheck、diff check、生产构建通过；**72 个测试文件 / 297 passed**，语句 **81.21%**、分支 **70.64%**、函数 **71.51%**、行 **84.63%**。
- 组件覆盖分组草稿保留、只保存当前分组、URL 恢复、离开/刷新提示、只读权限、保存冲突及首次加载失败重试。
- CLI 部署契约 **4 passed**：迁移后运行统一初始化，失败阻断启动，密码只生成一次且不进入日志。本轮未修改 CLI 命令或部署入口。
- 本轮未迁移开发数据库，未执行真实 Docker 部署或浏览器端到端验收。使用开发环境手测前需通过 CLI 初始化/升级执行新迁移和菜单同步，再重新登录。

回归入口：`tests/modules/system/test_setting_separation.py`、`tests/test_release_migration_roundtrip.py`、`tests/scripts/test_deployment_seed.py`、`tests/core/test_tenant_inventory.py`；Web `src/views/system/setting/modules/__tests__/settings-form.spec.ts`。

## 2026-09-26：扩展名全集单源下发

- `UPLOAD_EXTENSION_UNIVERSE` 常量是可选扩展名全集的唯一来源：`upload:allowed_extensions` 的 default 由常量 join 生成，`GET /system/setting/{group}` 的 `fields[].options` 向客户端下发全集；`ALLOWED_UPLOAD_EXTENSIONS` 改为正向派生。
- Web 扩展名多选改为渲染 `field.options` 元数据（与 select kind 同机制），删除前端硬编码的 14 项数组。新增扩展名只需修改后端常量一处。
- 全集由 14 项扩至 28 项（新增 .bmp/.ico/.ppt/.pptx/.md/.json/.xml/.7z/.tar/.gz/.mp3/.wav/.mp4/.mov）；html/svg/js 等主动内容与可执行文件永久排除。存量租户已保存的白名单**不被改写**——设置页出现新选项后由管理员手动勾选；未自定义过的新租户直接落入新默认值。
- `ai_file` 场景白名单扩为 csv/xlsx/txt/md/json：聊天附件 md/json 走 `ai-chat-private` 私有根，上传时对空/octet-stream MIME 按扩展名补 canonical 类型（浏览器对 .md 常发空 MIME）；读取侧 `_looks_like_text` 内容校验与 `TextParser`（rows=行数、preview 前 3 行、单行截断 2000 字符）覆盖。`.txt` 仍走 CsvParser（既有行为不变）。
- image/import 场景白名单与公开挂载的 JPEG/PNG 内容校验（7a3b4be 收紧）不变：通用场景（general）非图片文件仍会被公开上传校验拒绝，非图片通用文件的存储与下发通道（私有根 + 鉴权下载，或公开安全静态类型）仍是未决遗留点。

9. **扩展名全集单源下发** — 全集是代码级安全边界，由后端目录统一定义，前端只消费元数据，避免两侧数组漂移。**反例**: 前端另维护一份选项数组，新增扩展名漏改一侧导致可勾选但保存被 `SETTING_VALUE_INVALID` 拒绝。**回归**: `test_upload_extension_field_serves_option_universe` 断言 options 含新增项且新租户默认值等于全集；Web 组件测试断言多选选项来自 `field.options`。
10. **场景能力跟代码解析器走，租户只能收窄** — ai_file 扩 md/json 的前提是 TextParser 能安全摘要它们；上传路由与 MIME 补齐让私有链路端到端可用，不触碰公开挂载。**反例**: 只扩全集不动场景/路由，设置页可勾选但上传被拒；或为图省事把 md/json 放进公开挂载。**回归**: `test_ai_file_scenario_accepts_markdown_and_json`、`test_server_keeps_chat_markdown_json_attachments_private`、`tests/modules/ai/test_file_parser.py::TestTextParser`。

11. **按字段类型验证选项** — select 校验单个选项，extensions 对逗号分隔的多选集合做白名单校验、去重和规范化。**反例**: 扩展名字段下发 options 后，将 `.md,.png` 当作一个单选项拒绝，导致整个上传设置分组无法保存。**回归**: `test_upload_extensions_save_multiple_choices_and_preserve_invalid_write`，同时验证非法扩展名拒绝后原值和版本保持不变。

### 提交前复核（2026-09-26）

- 修复扩展名元数据新增 options 后，多选白名单被单选校验拒绝的问题；保留页面去重标题及后端元数据驱动选项。
- 隔离数据库后端全量 **2,907 passed**，覆盖率 **79.81%**；Web 全量 **299 passed**，语句 **81.26%**、分支 **70.71%**、函数 **71.66%**、行 **84.66%**；CLI 全量 **45 passed**。
- 后端/CLI Ruff 与格式检查、Web 生产构建通过。既有通用非图片文件上传通道限制仍按上文边界保留，未扩大公开文件挂载。

## 已确认范围

- 系统内置设置采用分组表单，自定义参数保留表格；固定标签与说明使用语言包，用户内容原样保存。
- 菜单、权限、内置角色及助手采用稳定翻译键，明确记录自定义覆盖；不根据部署语言重写已有内容。
- 默认语言按用户选择、租户默认、部署默认回退；缺失翻译回退英文及原文。新增语言不需要初始化第二份数据。
- 环境变量保留启动、存储路径、网络安全边界、进程拓扑和紧急熔断；业务设置移至数据库。
- 模型输出 token 数与 temperature 使用每模型配置，未配置不强制传参；删除无效全局 AI 参数。
- 通用请求限流使用 Redis 原子计数；登录/注册与已认证 API 分开计数，多实例共享。AI 工具执行限流维持独立语义。
- 文件策略统一覆盖前端提示、上传、导入、AI 读取与代理配置。有效单文件上限为部署、租户、场景上限的最小值；请求体开销、解压大小、行数和像素限制独立保留。降低上传上限不影响已有文件下载。

## 契约与边界

- 使用独立 sys_setting 租户键值存储，自定义参数保存在 sys_config；内置键的类型、默认值、公开/敏感属性在代码统一定义，通用增删改和导入不得绕过。
- 设置分组读取/保存使用 system:setting:list/edit 权限；平台安全设置额外限定系统管理员（默认租户 R_SUPER）。所有业务设置显式 tenant scope。
- 分组写入先完整校验再原子保存；敏感值不回显；提交事务后清理租户设置缓存。Service 不 commit。
- API 返回原始内容与可选翻译元数据，编辑接口不接收翻译后的展示值。用户修改字段后停止应用其默认翻译。
- CLI 与后端同步更新，无旧版本兼容包装；重跑初始化只补缺，保留已有设置。

## 决策记录

1. **设置定义与配置值分离** — 固定设置是产品能力，键和安全属性不能被参数 CRUD 任意改写。**反例**: 将默认密码设为公开配置。**回归**: 设置接口与通用参数保护测试。
2. **动态展示内置文案** — 多语言用户及租户共享稳定业务标识。**反例**: 初始化英文后无法切换内置角色显示语言。**回归**: 翻译回退、用户覆盖与重复种子测试。
3. **文件策略统一来源、按场景收紧** — 上传与消费能力必须一致，保留解析器资源边界。**反例**: 前端允许上传但代理/解析器拒绝，或提高文件上限同时放松解压限制。**回归**: 文件边界、租户隔离、代理模板测试。
4. **配置必须连接真实消费者** — 不保留仅可保存而不生效的模型/限流字段。**反例**: 修改 RATE_LIMIT_LOGIN 后中间件继续使用写死值。**回归**: 实际模型参数传递与跨实例限流测试。
5. **并发保存携带版本** — 分组返回 HMAC revision，保存时锁定租户行并比较版本，防止后写覆盖先写。密码不参与明文回显，空密码表示保持不变。**反例**: 两个管理员打开相同表单，第二人无意覆盖第一人的调整。**回归**: `tests/modules/system/test_settings_service.py`。
6. **请求体限制先于审计读取** — ASGI 外层同时检查 Content-Length 和流式接收字节；设置密码在嵌套 values 中也必须脱敏。**反例**: 审计提前读取超大 JSON，或将导入默认密码记录到操作日志。**回归**: `tests/modules/system/test_request_body_limit.py`、`test_settings_audit.py`。

## 实际配置归属

| 配置 | 入口 | 生效方式 |
| --- | --- | --- |
| 品牌名称、Logo、描述、页脚、协议 | 系统设置的品牌/协议分组 | 租户存储；公开配置供登录页及客户端读取；名称/Logo/页脚接入 Web 展示 |
| 开放注册、默认头像、主部门约束、导入/重置默认密码 | 账号分组 | 注册端点、头像显示及原有用户服务消费；密码永不回显 |
| 默认语言 | 语言分组 | 已保存的用户语言优先，其次租户设置，缺失时使用 DEFAULT_LOCALE；无翻译时回退英文/原文 |
| 文件大小、图片/导入大小、扩展名 | 文件分组 | 前端能力提示、上传、配置/用户导入、AI 文件读取共用后端策略 |
| 登录/注册/API 每分钟额度 | 访问保护分组 | 默认租户 R_SUPER 管理；Redis 原子计数、多实例共享，缓存最长 30 秒，保存后失效 |
| max_tokens / temperature | AI Provider 页面中的每个模型 | 存入模型 config.generation，创建真实模型和测试连接均传入；留空省略参数 |
| 数据库、Redis、JWT 密钥、存储根目录、网络与进程边界 | 环境变量 | 部署配置，更新后重启 |
| 文件部署硬上限 | UPLOAD_HARD_MAX_BYTES | 默认 100 MiB；CLI 派生代理请求体上限并透传到后端、Web 和网关 |

删除原来无实际消费者的 `AI_MAX_TOKENS`、`AI_TEMPERATURE`、`RATE_LIMIT_LOGIN/REGISTER/API` 及旧的 `UPLOAD_MAX_SIZE/UPLOAD_ALLOWED_EXTENSIONS` 配置项。模型连接配置以数据库 Provider/Model 为准，移除未使用的全局默认模型/API key 设置。模型参数校验只覆盖通用类型及范围；具体模型支持的参数仍以该 Provider 为准，不为所有模型强制设置 temperature。

API 时间格式保持现有契约；它不属于可以随意修改的显示偏好。AI 工具的写入/全局配额与 HTTP 请求限流是不同边界，仍由各自的安全模块管理。

## 设置与能力接口

所有响应继续使用 `{code, msg, data}`；系统设置路径以 `/system/setting` 为前缀，自定义参数继续使用 `/system/config`。

- `GET /`：可访问分组名称，要求 `system:setting:list`。
- `GET /{group}`：`group / values / fields / revision / secretKeys / configuredSecrets`。values 使用稳定配置键，布尔/整数具有真实 JSON 类型。敏感值返回空字符串。
- `PUT /{group}`：`{values, revision}`，要求 `system:setting:edit`；不接收任意键或修改公开属性。值非法为 `SETTING_VALUE_INVALID`，并发冲突为 HTTP 409 / `SETTINGS_CONFLICT`。
- `GET /runtime`：认证用户读取当前租户的语言、品牌、默认头像、主部门要求、各场景 `maxBytes / extensions`；不需要配置管理权限，不返回密码或全局安全阈值。
- `GET /public`：延续现有可信公共租户定位边界，补齐代码定义的公开默认值。登录前读取默认租户；认证后使用实际租户能力。
- 原参数列表、导出只返回自定义参数；原创建/修改/删除/批量删除/导入不能操作内置键。AI、安全和上传保留前缀也不能用于绕过定义。

安全分组额外验证默认租户与系统管理员身份；租户管理员不能借接口路径访问。设置保存由 API commit，随后清除本租户的 setting 缓存；自定义参数使用 config:custom 缓存，避免读取拆分前的混合缓存。列表页的语言切换只改变展示，不将翻译内容提交到编辑接口。

## 上传与部署契约

`effective_max_bytes = min(UPLOAD_HARD_MAX_BYTES, tenant_max, scenario_max)`。

- 图片场景再取图片偏好，处理硬上限 20 MiB；用户/配置导入与 AI 文本文件再取导入偏好，处理硬上限 10 MiB。
- 支持的扩展名是租户白名单与场景白名单的交集。原有 MIME、图片解码、XLSX 防炸弹、行数和路径/所有权验证继续生效。
- 已有图片读取保留独立处理上限，降低上传额度不使历史图片失效。
- 单次请求体上限为部署硬上限加 1 MiB；它约束整个 multipart 请求，批量上传也受此上限约束。超限返回 HTTP 413。
- `hohu-cli` 自动生成 `UPLOAD_REQUEST_MAX_BYTES` 并向两层 Nginx 传递相同值；外部代理示例片段同步更新，保留其他自定义指令。用户无需重复填写三个大小。
- 修改数据库偏好对后端后续请求生效；Web 登录和保存设置后刷新能力，其他已打开客户端可能暂时显示旧额度，后端仍按新额度校验。
- 请求限流的 Redis 故障返回 503，超额返回 429 和 Retry-After；不在故障时无保护地放行登录。健康检查和 OPTIONS 不计数。

## 内置数据国际化与升级

迁移 `b728ae092401` 为 `sys_role`、`ai_agent` 增加可空 JSONB `i18n_keys`。菜单继续复用 `i18n_key`。API 附带 `i18nKeys`，用户列表附带与 roleNames 对应的 roleNameKeys；业务 ID、权限码与原始文本不变。

首次同步旧数据时，仅为仍等于内置默认文案的字段补翻译键。之后修改名称/描述会删除对应键；空映射或菜单空键是明确的自定义覆盖标记，重跑初始化不能将它重新绑定到内置翻译。Agent 同步也不再覆盖自定义名称/描述。

Web 的 `builtin-{zh-cn,en-us}.json` 提供内置角色、助手、按钮权限翻译，`settings-{zh-cn,en-us}.json` 提供设置标签与说明。新增语言的步骤：

1. 增加对应语言资源，并在 locale 注册表及语言选择列表中注册。
2. 补齐 builtin/settings 的相同键；语言资源完整性测试应包含新语言。
3. 在后端设置目录的 default_locale 选项中注册语言代码，按需调整部署 DEFAULT_LOCALE。
4. 验证切换、缺失翻译回退、自定义名称保留；无需复制数据库初始化数据或新增语言字段。

CLI 原有迁移与幂等初始化流程自动执行本次迁移和元数据同步；没有旧 CLI 兼容层。升级不会按语言重写已有业务内容，设置值（品牌名称、协议正文等）也保持管理员输入原文。

## 实施与验收

- ✅ Plan A 已完成（2026-09-24）：设置目录、分组接口、参数隔离与设置页面。
- ✅ Plan B 已完成（2026-09-24）：统一文件策略及 CLI 部署上限。
- ✅ Plan C 已完成（2026-09-24）：模型参数、Redis 限流及环境模板整理。
- ✅ Plan D 已完成（2026-09-24）：内置数据翻译、自定义保护、默认语言及扩展指引。
- 后端/CLI Ruff 与全量测试，Web lint/typecheck/全量测试；覆盖率门禁 70%。

## 验证记录（2026-09-24）

- 后端 Ruff check/format 通过；独立测试数据库全量 **2,886 passed**，覆盖率 **79.77%**。现有 33 条测试警告未作为通过结果隐藏。
- 新迁移已通过空库升级、升级/降级往返和保留发布边界数据测试；幂等种子和自定义覆盖回归通过。
- Web lint、format、typecheck 和生产构建通过；**72 个测试文件 / 293 tests passed**，语句覆盖率 **81.12%**、分支 **70.63%**、函数 **71.12%**、行 **84.45%**。
- CLI Ruff check/format 与全量 **45 tests passed**；验证请求体上限派生、幂等写入、错误值拒绝、保留自定义代理指令和两层代理一致性。
- 验证使用独立数据库，未迁移开发数据库。未执行 Docker 实际部署或生产 Provider 调用。

主要新增回归：`tests/modules/system/test_settings_service.py`、`test_runtime_policies.py`、`test_builtin_translations.py`、`test_shared_rate_limit.py`、`test_request_body_limit.py`、`test_settings_audit.py`；Web 的 `settings-form.spec.ts`、`runtime-settings.spec.ts`、`builtin-translations.spec.ts`；CLI 的 `tests/test_upload_limits.py`。
