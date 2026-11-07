# 通用字段策略存储与准备入口

本文描述 Backend 0.2.0 开发版本的内部持久化契约。目录与规则语义见[通用字段内核](FIELD-POLICY-CORE.md)。存储和候选管理已经提供；全系统业务对象、Web 页面与发布资格仍须各自接入和验收。当前准备目录只包含兼容对象 `system.user`，不能据此宣称全系统字段权限已生效。

## 事实与生效边界

`e020f1200001` 在原有授权屏障上增加 11 张表：全局不可变目录快照、对象声明，以及按租户隔离的 head、对象状态、规则修订、候选绑定、manifest、manifest 子项、审计、请求回执和迁移映射。全部新时间使用 UTC；角色、对象、修订和 manifest 通过复合外键约束租户与身份。租户 0 不代表跨租户授权。

规则修订与审计不能修改或删除。缺少绑定、空规则、新增角色均不产生允许规则。固定域对象不能保存为租户角色授予。发布或归档只修改候选绑定；运行时加载已封存的 active manifest，并重新核验目录、动作、适配器、子项与规则摘要。候选编辑不会改变当前 active。

manifest 必须先创建为 `building`，在父行锁保护下填充子项，再以精确子项数量封存为 `sealed`。封存后不能增删子项、替换摘要或修改 manifest。head 保留候选与活动的独立指针。对某对象冻结时，其受影响的来源依赖对象一同冻结；已启用事实不能退回 legacy。

在线管理复用原来的 G/B 和严格事务 owner。一次修改必须包含完整锁计划、head/绑定 CAS、一次原 B epoch 推进、审计和请求回执；Service 不提交。已有授权写者推进 B 时会清除 ready 水位。ready 使用自身写入后的 G/B，激活再次检查当前水位、资格摘要及有效期，并原子更新所有对象与用户状态镜像。G 改变也使旧 ready 无法激活。

请求以租户、当前 actor 和规范 UUID 标识。同一 ID、相同命令只返回原回执，不重复推进 epoch；不同命令返回 `FIELD_POLICY_REQUEST_CONFLICT`。读取回执仍需当前身份与权限。提交 ACK 未知时停止会话，不自动重试业务。

publish/archive 回执返回 `revisionId`，请求事实以完整 tenant/role/object/revision 外键固定该引用；封存结果同时固定 manifest 身份和摘要。候选后续变化不会改写原回执。

## 管理权限与命令

管理者需要当前启用的 `R_SUPER` 角色及对应专用功能码，二者缺一不可：

| 命令 | 功能码 |
| --- | --- |
| read、receipt | `system:role:field-policy-view` |
| publish、archive、build、ready | `system:role:field-policy-edit` |
| activate、freeze | `system:role:field-policy-activate` |

这些按钮位于现有角色管理菜单。新装种子为初始管理角色明确分配；升级不会默默扩大已有角色的菜单授权。部署方须在停写前完成必要权限配置并取得有效管理 token。自定义功能按钮不会自动获得字段许可。

写命令具有 `action`、`requestId`、`expectedVersion`、`reason`。publish/archive 另需 `roleId`、`objectId`、`bindingVersion`，publish 携带闭集 `rules`；ready/activate 另需 `expectedEpoch`；freeze 同时指定 `objectId`。ID 使用十进制字符串，版本/epoch 使用整数。规则文档是版本化协议，不能提交任意数据库列或 SQL。空规则明确表示未授予。

`read` 返回目录、head、对象状态和候选绑定；`receipt` 只接受 `requestId`。目录规范化内容的摘要以内部 snake_case 规范编码计算，HTTP 字段遵循 camelCase；不要对 HTTP 展示 JSON 直接计算持久化摘要。

## 受控准备与迁移

在[受控安装](CONTROLLED-INSTALL.md)协议内使用 `policy-prepare` 替代 `prepare`，可以在数据库 admission 关闭期间编辑候选。它只监听 `127.0.0.1`，默认端口 8765，可用 `--policy-port` 指定非特权端口；不启动普通 API lifespan、scheduler、AI 恢复、过期任务或业务路由。

| 路径 | 行为 |
| --- | --- |
| `GET /policy-prepare/catalog` | 当前身份下读取目录和候选 |
| `POST /policy-prepare/command` | 固定策略命令或本人回执查询 |
| `POST /policy-prepare/finish` | 验证当前读取权限，结束准备会话 |

全部入口要求已有 JWT Bearer。控制器串行处理命令，每次核验直接进程或 Compose 实例身份、停写记录、禁止新连接及数据库在途事务；结束后排队请求也不能重新进入。Ctrl+C、进程退出码或前端隐藏都不能代替证据。直接启动与 Compose 共用该协议，不引入新的部署平台。

迁移在当前维护 permit 内锁定原 G 和有序 B，单事务转换旧规则，保留旧 revision/audit、原 actor/时间、来源摘要及映射批次。自动生成的迁移 manifest 标记为 `creation_kind=migration`，不伪造 actor。旧 enforcing/frozen 的对象状态和 ever/active 历史保留，整体 head 仍为 preparing。相同迁移只允许对全部源/目标事实作精确只读核对；编辑后或摘要漂移时拒绝重放。

出现 generic head 后，旧用户策略 owner、旧业务字段路径以及旧策略表写者均在服务端拒绝，错误为 `FIELD_POLICY_CLIENT_UPGRADE_REQUIRED`（数据库直接写入由约束拒绝）。不能回退到旧 binding 继续运行。

当前准备入口拒绝 ready/activate，持久 `policy-prepare.json` 使所有 normal resume 入口保持关闭。完整对象与交付入口尚未接入，这一限制不能靠删除标记、替换旧回执或自行构造资格文件解除。资格协议绑定 build、schema、catalog、action、adapter、对象覆盖和全部验收证据，但测试中的模拟资格不代表部署资格。

控制器会话失败或连接丢失后保持停写；当前不提供自动重连恢复准备会话。恢复须使用已验证的数据库、文件、密钥和执行事实一致性备份。存在通用目录或策略事实时，DDL downgrade 会拒绝；不可用删表回退替代恢复。

## 预算与错误

目录规范编码限 1 MiB，单规则组限 256 KiB，旧用户原文先按 256 条/64 KiB 验证再转换；JSONB 物理大小另有保守上限。在线管理保护期 15 秒（读 8 秒）、锁 2 秒、statement 5 秒，累计读取最多 100000 行/128 MiB。迁移保护期 25 秒，permit 30 秒，最多 1024 租户；源记录分 16 条读取，累计 100000 行/128 MiB。超限整笔失败，不分批提交或开放部分结果。

输入不合法为 `FIELD_POLICY_INVALID`；身份/管理权限不足为 `FIELD_POLICY_MANAGEMENT_FORBIDDEN`；CAS 冲突为 `FIELD_POLICY_VERSION_CONFLICT`；未获资格为 `FIELD_POLICY_ACTIVATION_BLOCKED`；状态缺失或摘要漂移为 `FIELD_POLICY_UNAVAILABLE`；预算超限为 `AUTHORIZATION_PLAN_LIMIT_EXCEEDED`。所有成功响应保持 `{code: 200, msg, data}`，不返回业务字段值或秘密。
