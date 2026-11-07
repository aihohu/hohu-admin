# 字段策略候选管理

Backend/Web 0.2.0 开发版本提供通用策略候选管理。目前服务端目录仅注册 `system.user` 兼容对象。保存候选与封存租户清单可用，激活和正常恢复仍被服务端阻断；这些操作不代表全系统字段权限或生产发布已经完成。存储及迁移约束见[通用字段策略存储](FIELD-POLICY-STORAGE.md)。

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
