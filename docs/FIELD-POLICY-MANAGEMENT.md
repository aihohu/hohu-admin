# 字段策略候选管理

Backend/Web 0.2.0 开发版本提供通用策略候选管理。Backend 准备目录覆盖用户、字典类型、字典项、数据范围演示、角色、部门、菜单、配置、设置和任务十个对象；保存候选与封存租户清单可用，激活和正常恢复仍被服务端阻断。当前 Web 已验收的关系界面覆盖原七对象，配置、设置与任务的新协议需要配套页面接线；旧整对象表单由 Backend 拒写。这些能力不代表全系统字段权限或生产发布已经完成。存储及迁移约束见[通用字段策略存储](FIELD-POLICY-STORAGE.md)和[配置任务契约](CONTROL-FIELD-CONTRACTS.md)。

## 角色管理入口

入口位于「系统管理 → 角色管理」的行操作，字段权限紧邻菜单权限右侧，两者采用相同按钮样式。管理者必须具有启用的 `R_SUPER` 和显式 `system:role:field-policy-view`，通配按钮权限不能代替专用权限。编辑候选另需 `system:role:field-policy-edit`；冻结另需 `system:role:field-policy-activate`。每次请求重新检查当前身份、角色和功能码，按钮隐藏不是服务端鉴权。

抽屉显示当前租户、角色、数据范围、对象和候选版本。菜单权限决定能否调用功能，数据范围决定可访问记录，字段规则决定对应用途下的字段许可；三者同时成立才允许业务操作。平台固定边界、结构字段和不支持的用途不能由角色规则扩大。自定义按钮不会自动取得字段许可，需要服务端注册的动作与字段用途映射。

规则分为「读取与查询」「新增与修改」「导入与导出」。导入内部区分新增、修改和匹配；查询分别展示服务端支持的筛选、关联、分组等用途。未配置表示未授予；`deny` 是显式拒绝，清空配置不等同于允许。

「默认推荐配置」只补当前对象尚未配置的安全读取/查询，保留显式拒绝及写入设置。「全部允许」「全部拒绝」位于其右侧，作用于当前对象、当前页签的完整目录，与搜索过滤无关；确认框显示修改、跳过和脱敏数量。结构字段、密码等禁止批量授予的字段被跳过；敏感读取默认选择脱敏。支持本地撤销，草稿不写入浏览器持久存储。

预览只展示抽象规则差异，最多20条并标记截断，不返回业务记录原值。填写原因后才能保存候选。候选保存会重新读取服务端版本，当前 active 不变。封存租户候选使用服务端完整的角色/对象绑定集合，不以当前抽屉或搜索结果代替完整清单。当前页面明确显示激活阻断原因。

关闭、切换对象、刷新和普通路由离开时保护未保存草稿；切换登录身份会清除草稿。版本冲突要求重新加载。网络结果未知时保留 requestId 并锁定编辑，只允许核对回执，不自动重发修改；没有查到回执仍属未知。退出准备会话会主动销毁本地凭据。

## 管理 API

普通 API 与准备服务共用 `/system/field-policy` 管理协议。普通 API 使用原有 JWT；准备服务的这些路径只接受本进程签发的准备会话。租户与 actor 均由可信身份取得，不接受客户端传入的权限、资格或租户声明。

| 方法与相对路径 | 内容 | 专用权限后缀 |
| --- | --- | --- |
| GET `catalog` | 对象/字段/Access/硬上限/脱敏/推荐元数据 | view |
| GET `roles` | 当前租户角色的名称、代码、数据范围与状态 | view |
| GET `roles/{roleId}/objects/{objectId}` | 角色对象候选规则、绑定版本与能力 | view |
| POST `roles/{roleId}/objects/{objectId}/preview` | 绑定当前事实的签名预览 | view |
| POST `roles/{roleId}/objects/{objectId}/revisions` | 保存不可变候选修订 | edit |
| GET `manifests/current` | 全部候选引用、对象状态、active/candidate摘要 | view |
| POST `manifests/preview` | 完整租户候选预览 | view |
| POST `manifests` | 封存完整租户候选 | edit |
| POST `manifests/{manifestId}/ready`、`activate` | 当前均返回资格阻断 | edit、activate |
| POST `freeze` | 冻结对象及其依赖闭包 | activate |
| GET `requests/{requestId}` | 当前 actor 的持久回执或 null | view |

成功包装为 `{code: 200, msg, data}`。新管理协议的所有 ID、version、epoch 均为十进制字符串，包括 `expectedVersion` 和 `expectedBindingVersion`；不要转换为 JS number。catalog/roles 使用 `cursor`、`limit`（1–100）和 `nextCursor`，属于有界控制面游标协议；catalog另有 `complete`，每个对象包含完整字段目录。目录摘要、租户、head或epoch不一致时客户端拒绝使用拼接结果。目录上限沿用内核64对象、每对象256字段；不会把截断结果标记为完整。

对象预览请求包含 `catalogDigest`、`expectedVersion`、`expectedBindingVersion`、`rules`。每条规则仅允许 `fieldId`、`access: {operation, purpose, use}`、`effect`、`level`、`maskId`。保存额外包含 `previewDigest`、规范小写 UUID `requestId`、1–500字符的可打印 `reason`。manifest预览只需要 catalog/head；封存额外需要签名、请求ID、原因。冻结包含 `expectedVersion`、`expectedEpoch`、`objectId`、请求ID和原因。

请求体上限512KiB，重复JSON键、未知属性、越界版本和任意动作均拒绝。普通API的OpenAPI公布相同闭集请求 schema。预览签名有效5分钟，绑定当前用户/租户安全版本、G/B epoch、head、active、目录/动作摘要及完整目标规则或清单；保存时在同一事务重新校验。签名上下文上限4MiB，签名不包含业务值。原 `/policy-prepare/command` 的版本整数协议保持不变，不与新协议混用。

`FIELD_POLICY_PREVIEW_STALE` 表示预览过期或事实已变化；`FIELD_POLICY_VERSION_CONFLICT`、`FIELD_POLICY_CATALOG_CONFLICT` 要求重载；`FIELD_POLICY_MANAGEMENT_FORBIDDEN` 表示当前授权不足；`FIELD_POLICY_ACTIVATION_BLOCKED` 表示完整接线资格尚未满足。提交/回滚 ACK 未知按原持久回执协议处理。

## 受控 Web 与身份交接

准备服务仍由宿主执行，复用直接启动和 Docker Compose 的既有维护 inventory；不启动普通 API、scheduler、AI恢复或过期任务。使用与目标 Backend 配套的完整 Web 源码 SHA，在该 Web checkout 的锁定环境构建：

```bash
pnpm install --frozen-lockfile
pnpm build:policy --outDir /protected/policy-web
```

构建产生独立 `policy-prepare.html` 与 `policy-preparation.json`，后者包含协议、组件版本、源提交和构建文件SHA256。部署交接必须使用干净、已验收的源码提交及固定依赖，不能将有本地修改的构建声明为该提交的发布产物。运行时显式选择目录和完整 Web SHA：

```bash
python -m tools.ops.controlled_install policy-prepare \
  --inventory /protected/upgrade.json --control-dir /protected/control \
  --policy-web-directory /protected/policy-web \
  --policy-web-commit <40-character-web-sha> --policy-port 8765
```

新装另加 `--bootstrap`，其余配置遵循[受控安装](CONTROLLED-INSTALL.md)。目录和commit参数必须同时提供。控制器验证协议、0.2.0版本、源码SHA和每个文件摘要，只服务已验证的内存字节；未登记路径404，无普通业务路由fallback。清单≤256KiB、文件≤8MiB、合计≤64MiB，不提供任意文件读取。

打开本机 `http://127.0.0.1:8765/`，输入租户代码、已有用户名和密码。升级前必须在每个启用租户通过现有菜单/角色管理显式配置上述三个专用码，绑定给一个启用的 `R_SUPER` 管理身份；旧用户字段权限码不能代替。可先创建对应权限按钮再授权，升级种子按权限码复用，禁止直接改SQL授权。非新装在停写成立后、DDL前预检；新装在种子后预检。失败返回 `FIELD_POLICY_ADMIN_PREFLIGHT_REQUIRED`，不创建通用策略head或准备标记，且保持admission关闭，按维护恢复协议处理。

`POST /policy-prepare/login` 接受 `tenantCode`、`userName`、`password`，返回15分钟随机会话及tenant/actor字符串ID。密码校验在SQL事务和G/B锁之外；校验后重新检查当前身份和专用读取权限。不签发普通access/refresh token，服务端只存token摘要，客户端仅内存持有，不放URL/localStorage/sessionStorage。登录体≤8KiB，10次/分钟、最多16会话；同源与loopback Host校验阻断跨站请求和域名重绑定。登录沿用部署已有的租户登录模式及运行资格限制。

`POST /policy-prepare/logout` 撤销会话；`POST /policy-prepare/finish` 在当前授权与停写核验后结束服务。证据失效、未决事务或致命错误销毁会话并禁止排队请求重新进入。旧 catalog/command 入口仍只使用已有管理JWT；该JWT不能调用新准备管理API。准备页面共用产品角色抽屉，其他菜单/角色业务操作保持禁用。

准备控制器把最小登录/拒绝/结束事件写入受保护控制目录的 `policy-events.jsonl`，仅含时间、固定操作、稳定代码与已验证身份；单条≤1KiB、总≤8MiB，落盘失败会关闭会话。密码、令牌、草稿、原因和原异常文本不写入事件。候选成功的审计与回执仍在数据库事务内原子提交；普通API操作日志也不读取或记录管理请求体。

准备完成仍不开放正常admission。旧Web及旧用户策略路径的服务端拒绝继续生效，不提供绕回旧表的适配器；完整对象接线、实际镜像组合和恢复验收完成前不得解除阻断。

## 普通对象契约

三个普通对象的列表、字典类型 all、字典项 type/options 和写入回执都在当前授权事务中投影。无读取资格的字段省略 JSON 键；锚点 ID 保留并序列化为字符串。列表附带 `fieldPolicy: {read, query, create, canCreate}`，记录附带 `fieldPolicy: {update}`，字段名称保持目录 snake_case。它们只用于展示，不能作为后续请求的授权证明。旧普通表的 naive 时间输出为 ISO 8601，不把未迁移的时区来源伪装为 UTC。

Web 列、搜索和表单使用这些能力；修改仅发送用户显式改动的允许字段，只写字段允许重新输入而不回显旧值。新增包含服务端默认业务字段的完整足迹；省略参数不能绕过字段权限。`deptId`、`createBy`、租户和记录 ID 不能通过普通业务请求修改，未知字段拒绝。字典类型 ID 在 Web 中使用字符串。

查询资格检查覆盖筛选前的完整记录范围；未获资格的筛选和字典排序会拒绝整次请求，包括 total。字典下拉必须同时允许标签和选值，并检查 `system:dict-type:list` 或 `system:dict-data:list`；仅登录不能读取下拉内容。非分页选项上限 1000，超限拒绝而不静默截断。页大小上限 100，批量 ID 上限 256。

演示数据的记录范围按每个启用角色独立求值：一个角色的字段允许不能借用另一角色的记录范围。字典编码变更必须同时具备父字段修改资格及所有受影响字典项的 `dict_type` 修改资格和功能码；全部行先规划锁定，再原子更新。相同租户、类型最多一个默认项；冲突返回 `DICT_DEFAULT_CONFLICT`，不自动取消其他项的默认状态。

当前三个对象没有导入/导出业务入口，相关用途显示不可配置；新增入口必须重新登记动作与来源，不能借用手工新增/修改授权。数据库存在通用 preparing/frozen 或目录不匹配时，普通业务拒绝，不能通过这些适配器启动未获全系统资格的安装。

字典项新增或更换类型还需当前 `system:dict-type:list` 功能权限与类型编码的精确查询资格，校验覆盖原始租户范围，再检查目标是否存在。类型改名涉及的每个字典项均需修改类型编码的字段许可与 `system:dict-data:edit`；普通名称、备注修改不要求这项关联修改权限。写字段授权先于唯一性冲突检查。页面表头按本页实际投影字段生成，空页不提供业务列；能力提示不替代提交时的完整关联校验。

## 关系对象与现有 Web 入口

角色管理的操作区继续在“菜单权限”右侧提供“字段权限”，沿用原有抽屉和页面规范。当前目录提供七对象的中英文名称、字段和用途；“默认推荐配置”“全部允许”“全部拒绝”操作候选草稿，预览只显示规则差异，不读取业务值。读取、查询、新增、修改、导入、导出按对象实际登记的用途配置；未实现的用途不能借自定义功能按钮获得字段授权。

角色、部门和菜单的业务表单从当前受保护快照加载，支持稀疏修改、只写字段和脱敏替换；菜单、Agent、部门成员编辑器先证明完整集合再开放保存。角色状态/数据范围、菜单注册等专用动作仍沿用原授权协议。接口、来源与限制见[关系对象读取](RELATION-FIELD-PROJECTION.md)。候选管理和受控七对象准备不代表具备全系统生产激活资格。
