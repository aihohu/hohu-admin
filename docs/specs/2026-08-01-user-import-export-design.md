# 用户管理导入导出设计（页面 + AI 双轨对齐）

> 状态：⚠️ Plan v2.2（页面层 + AI 层，含 v2 + v2.1 + v2.2 P0/P1/P2 增强，**Ready for Implementation**） | 创建日期：2026-08-01 | v2 修订：2026-08-01 | v2.1 修订：2026-08-01 | v2.2 修订：2026-08-03（含 P1 二次细化）
> 作者：Jack
> 影响项目：`hohu-admin`（后端）、`hohu-admin-web`（前端）
> 关联文档：
> - [`2026-07-02-ai-tool-gateway-design.md`](./2026-07-02-ai-tool-gateway-design.md) §5.2 `user.create` 示例 / §10 异步通道 / §16.2 export 设计
> - [`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md)（标杆 spec 样本）
> - 待写：`2026-XX-cli-module-generator-design.md`（Phase B 模板提炼，依赖本 spec 落地）
>
> **v2 修订增量**（7 条）：
> - #2.19 ImportPreviewSession + preview_token（Redis 10min TTL）
> - #2.20 行级事务 savepoint + chunk 100 rows
> - #2.21 overwrite 字段白名单
> - #2.22 ImportBatchContext 持久化表
> - #2.23 Export 改 POST
> - #2.24 employee_no 字段
> - #2.25 并发导入兜底
>
> **v2.1 修订增量**（基于二次审查反馈，6 条细化）：
> - #2.19 增 `file_storage_key`（execute 不依赖前端重传文件，P0）
> - #2.20 增 `RECOVERABLE_ERROR_CODES` 白名单（致命错误 abort 整批，P1）
> - #2.22 batch 在 dry_run 阶段创建（审计完整覆盖 dry_run 动作，P0）
> - #2.22.1 failed_rows 文件 retention = batch retention（90 天同删，P1）
> - #2.24 employee_no 空值规范化（`"" → NULL`，P1）
> - #2.26 集中状态机定义（CREATED/RUNNING/SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED，P2）
>
> **v2.2 修订增量**（CTO 架构评审 P0/P1/P2，12 条，2026-08-03，定位调整：「安全、小规模同步导入 + 为未来异步大规模导入预留接口」）：
> - #2.2 修订 UserService facade + 子模块拆分（P1）：保留 `user_service` facade 接口，内部拆 `import_service` / `export_service` / `import_parser` / `import_validator` / `import_state`
> - #2.6 修订 sync/async 边界统一（P0）：导入 `USER_IMPORT_SYNC_THRESHOLD=2000`，导出 `USER_EXPORT_ASYNC_THRESHOLD=5000`，Phase 1/2 全同步 ≤2000
> - #2.10 修订行数 50000 → 2000（P0）：用户导入非 ETL 入口；> 2000 走 Phase 3 异步 ImportJob
> - #2.14 修订 AI tool 拆 `import_preview` + `import_execute`（P0）：原 `user.batch_create` 拆两个 tool，强制 HITL 确认防 AI 直接 execute
> - #2.19 修订 Redis cache-only（P0）：业务数据全部进 PostgreSQL `sys_user_import_batch`，Redis 只缓存 `preview_token → batch_id`；execute 凭 token 反查 DB
> - #2.24 修订 employee_no `sync_mode`（P1）：CREATE_ONLY（默认）/UPDATE_PROFILE/FULL_SYNC 三模式，CREATE_ONLY 防误覆盖
> - #2.26 修订 ImportBatchStatus Enum + CHECK 约束（P0）：Python `Enum` + SQLAlchemy `Enum` 类型 / DB CHECK
> - #2.27 新增 execute 幂等保护（P0）：CAS `UPDATE ... WHERE status='CREATED'`，0 rows 命中 → `AI_IMPORT_ALREADY_EXECUTED`
> - #2.28 新增 ImportBatch 业务日志表 `sys_user_import_batch_log`（P1）：CREATED/PREVIEW_DONE/EXECUTE_START/EXECUTE_FINISH/EXPIRED 节点留痕
> - #2.29 新增 取消能力（P2）：状态机加 `CANCELLED`，API `POST /import/{batch_id}/cancel`
> - §3/§5 修订 records/failed_rows 限流（P1）：API 响应不返回 `failed_rows` 数组，只返回 `failed_count + failed_rows_file`；preview records 限 `MAX_PREVIEW_RECORDS=2000`
> - §5 新增 GET `/import/{batch_id}` 状态查询 + POST `/import/{batch_id}/cancel`（P2，为 Phase 3 异步预留）
>
> **v2.2 P1 二次细化**（5 条，2026-08-03，进开发前一致性 / 健壮性修补）：
> - P1-1 全局 `PARTIAL → PARTIAL_SUCCESS` 统一（状态机 / 代码 / 文档 / 测试名）
> - P1-2 合并 `ImportPreviewSession` + `UserImportBatch` 为单一 aggregate root；状态机加 `PREVIEW_DONE` 中间态（CREATED → PREVIEW_DONE → RUNNING）
> - #2.30 批量操作强制 `reason` 审计参数（P1-3）：preview / execute / cancel / export 全部必填 `reason`，写入 batch.reason + batch_log.detail.reason
> - #3.9 `FileStorage` Protocol 抽象（P1-4）：save/read/delete/exists/public_url；LocalFileStorage 默认 + Phase 3 S3FileStorage 切换零改业务
> - #2.31 ExportTask 审计表（P1-5）：与 ImportBatch 对称设计；所有导出（同步/异步/AI）强制建任务 + filter_snapshot 冻结 + 30 天 TTL

---

## 0. 术语表

| 术语 | 含义 |
|---|---|
| **导入（import）** | Excel/CSV 文件 → 解析 → 批量新增用户，含冲突处理 |
| **导出（export）** | 按 filter 查询 → 生成 Excel 文件 → 下载 |
| **dry_run（预检）** | 不落库，仅返回"将影响哪些行"的预览，三端共用（HTTP `?dry_run=true` / AI HITL 抽屉 / 前端弹窗） |
| **on_conflict** | 已存在用户的处理策略：`skip`（默认）/ `overwrite`（覆盖更新）/ `fail_fast`（首个冲突即终止） |
| **半成功策略** | 部分行失败时，成功行正常落库，失败行收集返回；不回滚成功行 |
| **异步阈值** | 行数 > 阈值（5000）时切换异步通道（broadcast_to_user），本 spec 仅留接口位，**Phase 3 推迟实现** |
| **三端对齐** | 同一业务能力在 HTTP API / AI tool / 前端页面 三处都有，且共享同一 service 函数 |

---

## 1. 背景

### 1.1 现状盘点

`user_service` 现有 9 方法 / 275 行（CRUD + batch_delete + profile/password）。AI tool 现有 7 个（`count` / `stats` / `distinct` / `create` / `batch_delete` + `role.count/list` + `dept.count/list`）。

**用户管理「CRUD + 导入导出」完整度对照**：

| 操作 | 前端页面 | HTTP API | AI tool | gap |
|---|---|---|---|---|
| 列表查询 | ✅ | ✅ `/list` | ❌ 缺 `user.list` | Phase 2 |
| 单条详情 | ✅ | ✅ `/{id}` | ❌ 缺 `user.lookup` | Phase 2 |
| 新增 | ✅ | ✅ `POST /` | ✅ `user.create` | — |
| 修改 | ✅ | ✅ `PUT /{id}` | ❌ 缺 `user.update` | Phase 2 |
| 删除（单/批） | ✅ | ✅ `DELETE /{ids}` | ✅ `user.batch_delete` | — |
| **批量导入** | ❌ 无按钮 | ❌ 无 endpoint | ❌ 缺 `user.batch_create` | **Phase 1 + 2** |
| **批量导出** | ❌ 无按钮 | ❌ 无 endpoint | ❌ 缺 `user.export` | **Phase 1 + 2** |
| 聚合统计 | — | — | ✅ count/stats/distinct | — |

### 1.2 目标

**Phase 1（页面层，2-3 周）**：
- 后端：`user_service` 扩展 4 方法 + 3 HTTP endpoint
- 前端：列表页工具栏加「导入」/「导出」/「下载模板」按钮 + 导入弹窗（上传 → 预检 → 确认）
- Excel 解析在后端（`openpyxl`），HTTP API 自包含

**Phase 2（AI 层，2-3 周）**：
- AI tool：`user.batch_create`（接 Excel 文件路径，与 SR-24 `file.parse` 一致）
- AI tool：`user.export`（同步版，> 阈值走 Phase 3 异步通道）
- 复用 `user_service` 同一方法（dry_run / batch_create / export 零拷贝复用）

**Phase 3（推迟到独立 spec）**：
- 异步通道 `broadcast_to_user`（arq + WebSocket/SSE 双通道）
- 批量 HITL 协议扩展（行级勾选，spec §8 当前只支持单 tool_call 确认）
- `role` / `dept` / `job` 等模块同款改造

### 1.3 范围外

- ❌ **跨设备导入进度推送**（Phase 3 异步通道）
- ❌ **批量 HITL 行级勾选**（Phase 3 协议扩展，本期用单次确认 + summary 表达）
- ❌ **PDF / Word 导入**（业务场景 < 10%，独立 spec 处理）
- ❌ **导入实时进度条**（同步无进度，异步通道落地后再加）
- ❌ **role / dept 模块同款改造**（等 CLI generator 模板抽象，参考待写 spec `2026-XX-cli-module-generator-design.md`）

---

## 2. 关键决策记录

> 格式遵循 CLAUDE.md：`N. **决策名** — 理由。**反例**: ...。**回归**: ...`

### 2.1 **后端解析 Excel，HTTP API 自包含** — 开源项目的 HTTP API 必须能被任意客户端（curl / Python SDK / Go SDK / Postman）直接调用，前端只是薄壳。

**反例**: 前端解析 Excel → JSON records 发后端 → 客户必须用我们的前端才能导入，HTTP API 不通用；且与 AI tool 的 SR-24 `file.parse` 路径分裂，dry_run 逻辑要在两套入口分别实现。

**回归**: HTTP `POST /system/user/import` 接 multipart/form-data（file: UploadFile）；AI tool `user.batch_create` 接 `file_path: str`（Gateway 走 `file.parse` 已建立的文件解析通道），两端最终调同一个 `user_service.parse_import_excel(file_bytes)`。

### 2.2 **`user_service` facade 不变，内部拆子模块（v2.2 P1 修订）** — 项目惯例是「模块级单例」（`user_service = UserService()`），import/export 是用户模块的核心业务能力，不是独立子域。但 v2.1 把全部逻辑塞进 `user_service.py` 单文件，加上 v2.2 P0/P1 决策（preview token + 状态机 + 幂等 + sync_mode + 业务日志）会让文件膨胀到 1000+ 行。**v2.2 P1 拆为 facade + 子模块**：`user_service` 对外接口不变（HTTP / AI 调用方零改动），内部按职责拆 5 个子模块。

**目录结构（v2.2 P1）**：

```
app/modules/system/user/                 # 新建 user/ 子包（迁移自 service/user_service.py）
├── __init__.py                          # 重导出 user_service facade
├── service.py                           # UserService facade 类（薄层，<200 行）
├── constants.py                         # USER_IMPORT_MAX_ROWS / ImportBatchStatus / OVERWRITE_* 常量
├── schemas.py                           # UserImportRecord / ImportResult / FailedRow 等 Pydantic
├── models.py                            # UserImportBatch ORM（也可保留在 service/user.py 旁）
├── import_service.py                    # 导入主流程（parse / dry_run / batch_create）
├── import_parser.py                     # Excel 解析 + 字段校验（openpyxl / csv）
├── import_validator.py                  # 反查校验（resolve_dept / resolve_role_input / check_permission_boundary / check_dept_data_scope）
├── import_state.py                      # 状态机（_transition_batch_status / ImportBatchStatus / LEGAL_TRANSITIONS / cleanup crons）
└── export_service.py                    # 导出（export_users_to_excel + EXPORT_ALLOWED_FIELDS）
```

**facade 模式（接口零改动）**：

```python
# app/modules/system/user/service.py
from app.modules.system.user.import_service import ImportService
from app.modules.system.user.export_service import ExportService

class UserService:
    """UserService facade（v2.2 P1：仅薄层委派，业务在子模块）

    外部调用方零改动：HTTP / AI 仍用 user_service.parse_import_excel(...)
    """
    def __init__(self):
        self._import_svc = ImportService()
        self._export_svc = ExportService()
        # 现有 9 方法（CRUD + batch_delete + profile/password）保留在本文件

    # ========== 导入导出（薄层委派）==========
    async def parse_import_excel(self, db, file_bytes, mime_type):
        return await self._import_svc.parse(db, file_bytes, mime_type)

    async def dry_run_import_users(self, db, records, current_user, file_bytes, filename):
        return await self._import_svc.dry_run(db, records, current_user, file_bytes, filename)

    async def batch_create_users_from_records(self, db, records, *, preview_token, current_user, on_conflict="skip"):
        return await self._import_svc.execute(db, records, preview_token=preview_token,
                                              current_user=current_user, on_conflict=on_conflict)

    async def export_users_to_excel(self, db, filter, current_user):
        return await self._export_svc.export(db, filter, current_user)

    # ========== 现有 9 方法（CRUD 等）保留 ==========
    async def create_user(self, db, ...): ...
    async def update_user(self, db, ...): ...
    # ...

user_service = UserService()
```

**子模块依赖图**：

```
UserService(facade)
   ├─→ ImportService
   │     ├─→ ImportParser        (Excel 解析)
   │     ├─→ ImportValidator     (resolve_dept/role, permission_boundary, data_scope)
   │     └─→ ImportState         (_transition_batch_status, cleanup_expired_*)
   └─→ ExportService
         └─→ ExportValidator     (EXPORT_ALLOWED_FIELDS, data_scope filters)
```

**迁移策略**（PR 切片）：
- Task A：建 `user/` 子包，先把 `parse_import_excel` 委派到 `import_service.py`（HTTP 调用方不改）
- Task B：依次迁 `dry_run` / `batch_create` / `export_users_to_excel`
- Task C：迁完后 `user_service.py` 改为 `from app.modules.system.user.service import user_service`（重导出兼容旧 import 路径）
- Task D：删除旧 `service/user_service.py`，全量改 import 路径

**反例**: (1) **保持单文件 1000+ 行 → 阅读困难，AI tool 实现时容易遗漏决策**（v2.2 P1 修订核心）。(2) **新建 `user_import_export_service.py` 独立 service → 调用方需先理解「找哪个 service」；与现有 `batch_delete_users` 已挂在 `user_service` 上的惯例不一致**（v2.1 已拒绝）。(3) 子模块对外暴露 → 调用方直接 import `ImportService` 绕过 facade → 强耦合子模块实现，未来重构受限（**子模块对外不导出，只通过 `user_service` facade 暴露**）。(4) facade 接管 CRUD + 导入导出全部 → facade 自己膨胀（CRUD 保留在 facade.py，导入导出才委派）。

**回归**: `app/modules/system/user/` 子包；`UserService` facade 仅委派 + 保留现有 9 方法（< 200 行）；`ImportService` / `ExportService` 子模块对外不导出，只通过 `user_service.<method>` 暴露；HTTP / AI 调用方零改动；旧 `service/user_service.py` 改为 `from app.modules.system.user.service import user_service` 重导出，分多个 PR 渐进迁移。

### 2.3 **dry_run 三端共用：HTTP `?dry_run=true` + AI HITL + 前端预检弹窗 跑同一个 `user_service.dry_run_import_users`** — 三处都需要"将影响哪些行"的预检，逻辑必须单一来源，避免漂移。

**反例**: (1) HTTP 自己写一份预检逻辑，AI 写一份 → 字段冲突判断规则不一致，客户从 HTTP 切到 AI 看到不同结果。(2) dry_run 函数复用 `batch_create_users_from_records` 加 `commit=False` 参数 → 函数职责混淆，签名复杂。

**回归**: `dry_run_import_users` 独立函数，返回 `ImportDryRunResult(new=N, exists=N, conflict=N, examples=[...])`；`batch_create_users_from_records` 内部第一步就调 dry_run 把冲突行剔除（不重复实现）。

### 2.4 **on_conflict 默认 `skip`，AI tool / HTTP / 前端 三处都可切** — 「已存在的跳过」是用户最自然的表达（参考 Excel 导入常规语义）；但批量场景需要 overwrite（HR 月度同步），所以三种策略都支持。

**反例**: (1) 强制 `skip` → 月度同步场景用户必须先删除再导入，体验差。(2) 默认 `overwrite` → 误导入覆盖生产数据，风险高。(3) 默认 `fail_fast` → 一个冲突就终止，Excel 一行有问题全批失败。

**回归**: 默认 `skip`（最安全）；HTTP `?on_conflict=overwrite` query param；AI tool 函数签名 `on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip"`；前端导入弹窗 radio 切换（默认 skip）。

### 2.5 **password 列不入 Excel 模板；导入用全局默认密码（`sys_config.auth:default_password`）** — `password` 是敏感字段（spec §2.4 二分法），导入时所有新用户用配置项中的默认密码哈希入库；管理员线下告知用户默认密码值。部门 / 角色字段用业务语义（name/code）而非 ID，详见 #2.17 / #2.18。

**反例**: (1) 模板含 password 列 → 用户在 Excel 里写明文密码，文件流传过程中泄露面扩大；与 §2.4「sensitive_input 不在签名」原则冲突。(2) 每用户随机生成 + 返回明文密码清单 → 管理员分发过程中邮件 / IM / U盘 都是泄露点，离职员工仍持有。(3) 默认密码硬编码 → 部署方无法改，违反「配置驱动」。

**回归**: 模板列固定（`user_name` / `nickname` / `user_email` / `user_phone` / `dept_input` / `role_input` / `user_gender` / `status`，详见 #2.17/#2.18）；导入逻辑读 `sys_config.auth:default_password`（部署方在 admin UI 可改 + 定期轮换）→ 哈希入库；`ImportResult` 不返回密码字段（管理员从 sys_config 拿默认值，线下告知用户）。

### 2.5.1 **V1 不强制首次改密（known trade-off，未来补）** — 简化首版实现，承担「用户长期用默认密码」风险。强制改密机制需要：`sys_user.must_change_password` 字段 + alembic migration + 登录响应加 `mustChangePassword` 标记 + 前端登录后强制跳改密页 + middleware 拦截「未改密用户禁写」—— 半天工作量但 Phase 1 范围外。

**理由**: 首版聚焦导入导出主流程；强制改密是独立横切能力（未来不仅 AI 导入的用户需要，admin 手动创建、LDAP 首登都需要），单独 spec 推进更合适。

**反例**: (1) 一上来就做完整强制改密 → Phase 1 范围膨胀，周期拉长。(2) 把 `must_change_password` 字段加上但 middleware 不拦截 → 字段永远 True，假承诺。

**回归**: V1 部署文档明确标注「默认密码长期有效，建议部署方在 admin UI 改强密码 + 定期轮换 + 用户自行改密」；未来加 `must_change_password` 时导入逻辑只改 1 行（`User(must_change_password=True, ...)`），无破坏性；新增用户来源（admin 手动 / LDAP 首登）一并受益。

### 2.6 **同步/异步边界统一（v2.2 P0 修订）：导入 ≤ 2000 同步 / 导出 ≤ 5000 同步；超限走 Phase 3 异步通道** — 导入与导出的同步阈值**分开定义**（导入更小，因为含写库 + 权限校验 + savepoint；导出只读 + streaming，可承受更大）。两者都明确「同步路径上限 + 异步通道预留」，避免导入/导出阈值冲突。

**两套阈值**（v2.2 P0）：

```python
# app/modules/system/user/constants.py
USER_IMPORT_MAX_ROWS = 2000          # 单次导入同步上限（#2.10）；> 2000 拒绝并引导 Phase 3
USER_IMPORT_SYNC_THRESHOLD = 2000    # 同义词，给 Phase 3 异步切换逻辑用（保留接口位）
USER_EXPORT_ASYNC_THRESHOLD = 5000   # 单次导出同步上限；> 5000 抛 AI_EXPORT_ASYNC_REQUIRED
```

**导入路径**：
- `0 ≤ rows ≤ 2000`：Phase 1/2 同步路径（HTTP `POST /import` + AI `import_preview` / `import_execute`）
- `rows > 2000`：拒绝（`AI_IMPORT_TOO_MANY_ROWS`），引导用户「分批导入或等待 Phase 3 异步 ImportJob」
- Phase 3 异步通道上线后：阈值不变，但 > 2000 时不再拒绝而是入队（`status='QUEUED'` → `RUNNING`）

**导出路径**（保持原设计）：
- `0 ≤ rows ≤ 5000`：同步 StreamingResponse xlsx
- `rows > 5000`：抛 `AI_EXPORT_ASYNC_REQUIRED`，Phase 3 实现后自动切换异步

**反例**: (1) 导入导出共用一个阈值 → 导入 5000 行同步阻塞 worker 60s，破坏系统稳定。(2) 阈值都在 settings → 部署方误改成 50000，绕过 v2.2 P0 决策。(3) 导入超阈值自动异步 → Phase 1/2 没异步通道，行为未定义。

**回归**: 两套常量在 `user/constants.py` 模块级（**非 settings，避免误改**）；service 层先 `count(*)` 再分支；导入超限拒绝（不异步），导出超限拒绝（Phase 3 后改异步）；测试 `test_import_rejects_over_2000_rows` + `test_export_rejects_over_5000_rows`。

### 2.7 **半成功策略：成功行落库 + 失败行收集返回，不回滚** — Excel 1000 行里 5 行字段冲突，应允许 995 行成功 + 返回 5 行错误清单。回滚会让用户每次都得修完全部错误才能导入，体验极差。

**反例**: (1) 全有全无（事务回滚） → 1000 行有 1 行错就全失败，用户修一行重传一次。(2) 失败即终止（fail_fast）→ 用户不知道后面还有多少错。

**回归**: `batch_create_users_from_records` 内独立 session + 每 N 行（默认 100）flush 一次；失败行进 `failed_rows: list[FailedRow]`（含 `row_num` / `record` / `reason`）；返回 `ImportResult(success_count, failed_rows)`；API 响应里同时返回成功数 + 失败清单 + 失败行 Excel 下载链接（让用户下载错误清单修了再传）。

### 2.8 **审计双轨：HTTP import 走 `sys_operation_log`（AuditLogMiddleware），AI import 走 `ai_operation_log`（Gateway executor）** — 与 spec §2.7 AI 维度审计独立于 HTTP 审计的现有设计一致。HTTP 走中间件自动审计，AI 走 Gateway 显式审计。

**反例**: (1) HTTP import 也走 `ai_operation_log` → 概念混淆（HTTP 不是 AI 维度）。(2) AI import 复用 `sys_operation_log` → spec §2.7 明确反对，AI 调用是高频低密，与 HTTP 噪音混在一起难排查。

**回归**: HTTP import / export 自动被 `AuditLogMiddleware` 记录（path=`/system/user/import`，无需特殊处理）；AI import 在 Gateway executor 内按 tool_call 落 `ai_operation_log`（spec §4.4 既有逻辑），`tool_name="user.batch_create"` / `args_summary="file=xxx.xlsx, rows=245"`。

### 2.9 **导出字段白名单：`hashed_password` / `api_key` / `hashed_password` 永不导出** — 即使 SQL 直查也不能让敏感字段出库，从 service 层兜底。

**反例**: (1) 导出全字段 → Excel 流转过程中 hashed_password 泄露。(2) schema 层用 `exclude` → 业务方新加字段忘记 exclude 就泄露。

**回归**: `export_users_to_excel` 内部用 `EXPORT_ALLOWED_FIELDS = {"user_name", "nickname", "user_email", "user_phone", "dept_id", "role_codes", "user_gender", "status", "create_time"}` 白名单，未列入的字段不进 Excel；新增敏感字段时白名单不变（默认安全）。

### 2.10 **导入文件限制：≤ 10MB / MIME 白名单（xlsx, xls, csv）/ 行数 ≤ 2000**（v2.2 P0 修订，原 50000 → 2000）— 用户导入属于 admin 管理操作，**不作为 ETL 数据同步入口**。2000 行以内保证 Excel parser 内存可控、dry_run 同步返回、前端 preview 可展示、单次事务时间可控、AI Tool 操作安全。> 2000 行的场景（HR 系统批量同步、ERP 集成）走 Phase 3 异步 ImportJob（独立 spec）。

**为什么 2000 而不是 50000**（v2.2 调整理由）：
1. **同步路径上限**：50000 行 Excel 解析 + Pydantic 验证 + chunk 100 落库 ≈ 30-60s 同步阻塞，FastAPI worker 占用影响其他请求
2. **dry_run 响应体积**：50000 行 records 序列化进 JSON response（即使只返回 summary，越界/冲突 failed_rows 可能上千条）→ HTTP body 几 MB，前端表格渲染卡顿
3. **preview 文件存储**：50000 行 Excel ≈ 5-10MB，redis cache / `/tmp/import/` 短期堆积压力
4. **错误回溯成本**：50000 行一半失败（25000 行错误清单）用户根本修不动，违反「批量导入的反馈必须 actionable」原则
5. **业务实际**：admin 用户管理 ≠ 大规模身份同步，> 2000 行的真实场景应该走 IAM/LDAP/SCIM 集成，不是 Excel

**回归**: 复用 `settings.UPLOAD_MAX_SIZE = 10MB`；MIME 白名单 `{"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel", "text/csv"}`；行数硬上限 `USER_IMPORT_MAX_ROWS = 2000`（常量在 `app/modules/system/user/constants.py`，可由部署方下调但不可上调，避免运维误改回 50000）；超限抛 `BusinessRuleException(error_code="AI_IMPORT_TOO_MANY_ROWS")`，message 引导用户「分批导入或等待 Phase 3 异步通道」。

### 2.11 **Data Scope 强制应用：操作人管辖范围校验（部门越界防御）** — 与现有 RBAC 完全一致：HR（DATA_SCOPE_DEPT）不能导入/导出他不可见部门的用户；部门管理员（DATA_SCOPE_DEPT_AND_SUB）可管理本部门 + 所有子部门；超管 / DATA_SCOPE_ALL 不受限。

**校验链**：
1. 解析 Excel 中的 `dept_input` → 反查得到 `requested_dept_id`（详见 #2.17）
2. 从 `current_user` 构建 `DataScopeContext`：`accessible_dept_ids: set[int] | None`（None = 全部可见）
3. 校验 `requested_dept_id ∈ accessible_dept_ids`？不在 → 整行 FailedRow + `AI_IMPORT_DEPT_OUT_OF_SCOPE`
4. 导出 `export_users_to_excel` 内自动加 `data_scope.filters` 到 WHERE（HR 只能导出他可见的）

**反例**: (1) 导入不校验 dept 可见性 → HR 把用户塞到超管部门绕过权限（权限提升攻击）。(2) 导出全量 → HR 一键导出全公司通讯录。(3) 只校验当前部门忘记子部门 → 部门管理员导入子部门用户被拒，体验断裂。

**回归**: 复用 spec §6.2 `ensure_targets_in_scope(ctx, dept_ids=[...])` + `build_data_scope_context(db, current_user)`；DATA_SCOPE_DEPT_AND_SUB 模式下子部门自动纳入（已有 `get_dept_and_sub_ids` helper）；超管 / DATA_SCOPE_ALL → `accessible_dept_ids=None` 跳过校验；DATA_SCOPE_SELF → 只能导入自己（实际无意义，会返回 `AI_IMPORT_DEPT_OUT_OF_SCOPE`）；FailedRow 收集所有越界行，不阻断其他成功行（与 #2.7 半成功策略一致）。

### 2.12 **错误信息结构化：行号 + 字段 + 原因** — Excel 导入错误必须可定位（用户能找到具体哪一行哪一字段错）。

**反例**: (1) `{"error": "数据格式错误"}` → 用户不知道哪行。(2) `{"row": 5, "error": "..."}` → 不知道哪个字段。(3) 抛异常终止 → 一次只看到第一个错误。

**回归**: `FailedRow(row_num: int, field: str, value: str, reason: str, error_code: str)`；前端表格渲染失败清单，按行号排序；可下载"错误清单.xlsx"（用户改完再传）。

### 2.13 **模板下载 endpoint：固定列顺序 + 示例行 + 字段说明 sheet** — 用户拿到模板就能填，不需要看文档。

**反例**: (1) 不提供模板 → 用户自己拼 Excel 列顺序，90% 第一次传错。(2) 只给空表头 → 用户不知道哪些必填。

**回归**: `GET /system/user/import/template` 返回 xlsx，含 2 sheet：「数据」（列顺序固定 + 2 行示例）+「说明」（每列字段说明 + 必填标记 + 取值范围）。

### 2.14 **AI tool 接 `file_path` 而非 file upload multipart（v2.2 P0：拆 `import_preview` + `import_execute` 两个 tool）** — 与 SR-24 `file.parse` 一致：用户先上传文件到 `/file/upload` 拿 file_path，再把 path 传给 AI tool。Gateway 不直接处理 multipart。**v2.2 P0 修订**：原 `user.batch_create` 单一 tool 让 AI 可以跳过预检直接 execute，违反 HITL 原则（批量写入必须人在回路确认）。**v2.2 拆为两个 tool**：`user.import_preview`（只读，跑 dry_run + 生成 batch）+ `user.import_execute`（写入，必须 HITL 确认 + preview_token）。

**两 tool 契约**：

```python
@ai_tool(AiToolMeta(
    name="user.import_preview",
    agent="user_mgmt",
    summary=(
        "Parse Excel + dry-run import → returns {batch_id, preview_token, summary}. "
        "Read-only, does NOT write users. Call user.import_execute next."
    ),
    required_perms=("system:user:add",),
    risk="low",                          # 只读，不写库
    readonly=True,
    accepts_file=("text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    produces_file=False,
    result_view="detail_card",           # 展示 batch_id + preview_token + summary
))
async def user_import_preview(
    ctx: AiToolContext,
    *,
    file_path: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
) -> ToolResult:
    """file_path 来自 /file/upload。

    流程：
    1. file_path → file_bytes（Gateway 内部解析）
    2. user_service.parse_import_excel → records
    3. user_service.dry_run_import_users(records, current_user, file_bytes, filename)
       → INSERT sys_user_import_batch (status=CREATED)
       → 写 Redis cache (preview_token → batch_id)
    4. ToolResult.success(data={batch_id, preview_token, summary}, ui=detail_card)
    """

@ai_tool(AiToolMeta(
    name="user.import_execute",
    agent="user_mgmt",
    summary=(
        "Execute previously previewed batch import. REQUIRES HITL confirmation. "
        "Pass preview_token from user.import_preview."
    ),
    required_perms=("system:user:add",),
    risk="high",                         # 写入，批量创建用户
    hitl_always=True,                    # 强制 HITL（批量写入永远人在回路）
    dry_run_supported=False,             # execute 本身不是 dry_run
    accepts_file=(),                     # 不接文件，凭 preview_token 取
    produces_file=True,                  # 输出 failed_rows.xlsx
    result_view="rows_affected",
))
async def user_import_execute(
    ctx: AiToolContext,
    *,
    preview_token: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
) -> ToolResult:
    """凭 preview_token 反查 batch → CAS CREATED→RUNNING → chunk + savepoint。

    幂等保护（#2.27）：重复 execute 已 SUCCESS 的 batch 返回原结果。
    """
```

**两个 tool 而不是一个的根本理由**：
1. **HITL 强制**：`import_execute` 必须 `hitl_always=True`，AI 不能跳过；`import_preview` 只读无需 HITL（用户体验：连续两个 HITL 太烦）
2. **风险评级不同**：preview 是 low risk（只读），execute 是 high risk（写库）；分开后权限审计粒度更细
3. **结果视图不同**：preview 用 `detail_card`（展示 batch_id + summary），execute 用 `rows_affected`（展示成功 N 行 / 跳过 M 行）
4. **错误恢复路径不同**：preview 失败用户改 Excel 重传即可（生成新 batch_id）；execute 失败需要查 batch_id 审计反查
5. **未来异步通道扩展**：Phase 3 实现 `import_execute` 改异步时只影响 execute tool，preview 仍同步

**LLM Prompt 引导**（在 tool description 内嵌）：
> "用户给一份用户列表 Excel 想批量导入：先调 user.import_preview 拿 preview_token 展示给用户确认，再调 user.import_execute（用户确认后）。**禁止跳过 preview 直接 execute**。"

**反例**: (1) **单 tool `batch_create` 让 AI 自行决定是否 dry_run → AI 可能为了「快」跳过预检直接 execute，违反 HITL 原则**（v2.2 P0 修订核心）。(2) AI tool 直接接 multipart → 与 SR-24 路径分裂，Gateway 要单独实现文件接收逻辑。(3) 拆 tool 但不强制 HITL → AI 仍可自行调 execute，绕过预检。(4) 用 `dry_run_supported=True` 单 tool 标志位区分 → AI 可主动设 `dry_run=false` 跳过预检，与拆 tool 等价但更脆弱。

**回归**: AI tool `user.import_preview(file_path, on_conflict) -> {batch_id, preview_token, summary}` + `user.import_execute(preview_token, on_conflict)`（强制 HITL）；前端 chat 上传文件 → `/file/upload` → 拿 path → AI 调 `import_preview` → 展示 summary → 用户确认 → AI 调 `import_execute`；Excel 解析复用 SR-24 `file.parse` 的解析通道；HITL 抽屉展示 preview summary + execute 按钮；测试 `test_ai_cannot_skip_preview`（验证 LLM tool 调用历史中 execute 必须在 preview 之后）。

### 2.15 **Permission Boundary：操作人不能分配自己不拥有的角色（权限提升防御）** — 批量导入是权限提升攻击的高风险入口（HR 给被导入用户分配 R_SUPER），后端必须在 service 层强制校验：`Excel 中请求的角色 ⊆ 操作人自己拥有的角色`，否则整行 FailedRow。

**校验链**：
1. 操作人自己拥有的 role_ids：`operator_role_ids = {r.role_id for r in current_user.roles if r.status == STATUS_ENABLED}`
2. Excel 中 `role_input` 反查得到 `requested_role_ids`（详见 #2.18）
3. 越界集合：`out_of_scope = requested_role_ids - operator_role_ids`
4. `out_of_scope` 非空 → 收集越界角色名 → 整行 FailedRow + `AI_IMPORT_ROLE_OUT_OF_SCOPE`
5. 超管豁免：`is_super_admin(current_user)` 直接放行（与 `require_permissions` 超管 bypass 一致）

**反例**: (1) 不校验角色越界 → HR 批量导入用户时给某些人偷偷塞 R_SUPER，造成权限提升攻击。(2) 在 controller 层校验而非 service 层 → AI tool 也走 service 但绕过 controller，校验失效（service 是单一入口，必须下沉）。(3) 失败即终止 → 一行越界全批失败，与 #2.7 半成功策略冲突；正确做法是越界行进 `failed_rows`，其他行正常落库。(4) 不收集角色名只报 ID → 用户看到 "无权分配角色 [1001, 1002]" 不知道是哪个角色，体验差。

**回归**: `user_service.batch_create_users_from_records` 内部第一步对每行做 Permission Boundary 校验；越界行进 `failed_rows`（field="role_input"），不阻断其他成功行；超管 / 拥有 R_SUPER 的用户豁免；错误提示含角色名：`"无权分配角色 [系统超级管理员, 财务管理员]"`。

### 2.16 **Excel Data Validation（下拉）：模板部门/角色列加下拉，减少拼写错误** — Excel 用户手动敲部门名 / 角色 code 时易拼错（"研发部" vs "研发部门"），用 openpyxl `DataValidation` 引用字典 sheet 实现下拉。

**实现要点**：
```python
from openpyxl.worksheet.datavalidation import DataValidation

# 部门列：引用「部门字典」sheet 的 full_path 列（绕过 255 字符限制）
dept_dv = DataValidation(
    type="list",
    formula1="=部门字典!$A$2:$A$1000",   # sheet 引用而非字面列表
    allow_blank=False,
    showErrorMessage=True,
)
dept_dv.error = "请从下拉选择部门（或到「部门字典」sheet 复制 full_path）"
dept_dv.errorTitle = "部门无效"
ws.add_data_validation(dept_dv)
dept_dv.add("E2:E10000")   # 部门列所有数据行

# 角色列同理（引用「角色字典」sheet 的 role_code 列；allow_blank=True 因 role 可选）
```

**关键 trade-off**：
- Excel 数据有效性**只在手动输入时触发**，复制粘贴不强制 → 后端校验仍兜底（不能省 #2.17 / #2.18 / #2.15 / #2.11）
- 大量部门时下拉体验差（1000+ 项滚动难找）→ 字典 sheet 让用户直接 Ctrl+F 查 + 复制 `full_path` 粘贴
- `.xls` 老版本数据有效性样式不同 → 模板只支持 `.xlsx`，`.csv` 用户自己保证字段值正确
- 修改字典 sheet 后下拉选项不自动刷新 → 模板每次下载重新生成（含最新字典数据）

**反例**: (1) 用字面列表 `formula1='"研发部,市场部,..."'` → 超 255 字符报错。(2) 不加 Data Validation → 用户瞎填拼写错，dry_run 阶段大量"部门不存在"错误。(3) 加了 Validation 不做后端校验 → 复制粘贴绕过，安全漏洞。

**回归**: 模板生成时给「数据」sheet 的部门列（E）+ 角色列（F）加 `DataValidation`，引用字典 sheet；用户复制粘贴时 Excel 会显示警告但不阻止（仍允许 paste）；后端 #2.17/#2.18/#2.15/#2.11 全套校验兜底。

### 2.17 **部门字段用 `dept_input`（部门名 or 完整路径），不用 `dept_id`** — 用户在 Excel 里看不到 Snowflake ID，要求填 `dept_id` 等于要求用户先去数据库查 → 体验断裂。改用业务语义：简单场景填部门名"前端部"，重名场景填路径"总公司/研发中心/前端部"。

**实现要点**：
```python
async def _resolve_dept(db: AsyncSession, dept_input: str) -> int:
    """dept_input 可能是 '前端部'（名称模式）或 '总公司/研发中心/前端部'（路径模式）"""
    if "/" in dept_input:
        # 路径模式：逐级查找，解决重名
        parts = [p.strip() for p in dept_input.split("/")]
        parent_id = None
        for name in parts:
            stmt = select(Dept).where(Dept.dept_name == name)
            stmt = stmt.where(
                Dept.parent_id.is_(None) if parent_id is None
                else Dept.parent_id == parent_id
            )
            dept = (await db.execute(stmt)).scalar_one_or_none()
            if not dept:
                raise BusinessRuleException(
                    f"部门路径不存在: {dept_input}",
                    error_code="AI_IMPORT_DEPT_PATH_NOT_FOUND",
                )
            parent_id = dept.dept_id
        return parent_id
    else:
        # 名称模式：唯一性校验
        depts = (await db.execute(
            select(Dept).where(Dept.dept_name == dept_input)
        )).scalars().all()
        if not depts:
            raise BusinessRuleException(
                f"部门不存在: {dept_input}",
                error_code="AI_IMPORT_DEPT_NOT_FOUND",
            )
        if len(depts) > 1:
            raise BusinessRuleException(
                f"找到 {len(depts)} 个同名部门 '{dept_input}'，请用完整路径 '父部门/子部门'",
                error_code="AI_IMPORT_DEPT_DUPLICATE",
            )
        return depts[0].dept_id
```

**为什么 dept 走路径，role 走 code（不对称）**：dept_name 不 unique（树形重名），role_code/role_name 都 unique；dept 业务用户日常叫法是名称，role 配置时建的 code 是稳定锚点。

**反例**: (1) 强制 dept_id → 用户必须查 ID，门槛爆炸。(2) 强制完整路径（不让简单填名）→ 90% 无重名场景被复杂化。(3) 名称模糊匹配（LIKE）→ "研发" 误命中"研发部" + "研发中心"。(4) 不提供路径兜底 → 重名部门直接报错，无法导入。

**回归**: 模板「部门字典」sheet 提供 `dept_name` / `full_path` / `dept_id` 三列，用户复制 `full_path` 粘贴到「数据」sheet；service `_resolve_dept` 按 `/` 判断路径 or 名称模式；重名错误提示引导用户改用路径。

### 2.18 **角色字段用 `role_input`（支持 code 或 name 混合）** — 业务用户日常用 role_name（"开发者"），技术用户用 role_code（"R_DEV"），两者都 unique，后端两轮匹配支持混合输入。

**实现要点**：
```python
async def _resolve_role_input(db: AsyncSession, role_input_str: str) -> list[int]:
    """支持 'R_DEV,开发者,R_FRONTEND' 混合输入"""
    parts = [p.strip() for p in role_input_str.split(",") if p.strip()]

    # Pass 1: role_code 精确匹配
    by_code = (await db.execute(
        select(Role).where(Role.role_code.in_(parts))
    )).scalars().all()
    matched_values = {r.role_code for r in by_code}
    role_ids = [r.role_id for r in by_code]

    # Pass 2: 剩余未匹配的按 role_name 精确匹配
    remaining = [p for p in parts if p not in matched_values]
    if remaining:
        by_name = (await db.execute(
            select(Role).where(Role.role_name.in_(remaining))
        )).scalars().all()
        role_ids.extend(r.role_id for r in by_name)
        matched_values |= {r.role_name for r in by_name}
        remaining = [p for p in remaining if p not in matched_values]

    if remaining:
        raise BusinessRuleException(
            f"角色不存在（code/name 都未匹配）: {','.join(remaining)}",
            error_code="AI_IMPORT_ROLE_NOT_FOUND",
        )

    return list(set(role_ids))   # 去重（防 'R_DEV,开发者' 同一角色写两次）
```

**注意**：`role.role_code` 和 `role.role_name` 都 `unique=True`（`app/modules/system/models/role.py:23,26`），所以两者都能精确匹配，不会一对多。

**反例**: (1) 强制 role_code → 业务用户日常用 name，强迫记 code 心智负担大。(2) 强制 role_name → 角色改名后旧 Excel 失效（HR 月度模板要重做）。(3) 模糊匹配（LIKE）→ "开发" 误命中"开发者" + "开发组长"。(4) 不去重 → 'R_DEV,开发者' 同一角色 ID 出现两次，落库失败。

**回归**: 字段名 `role_input`（不叫 `role_codes` 因可能含 name）；模板「角色字典」sheet 提供 `role_code` + `role_name` 双列，用户复制任一列粘贴；service `_resolve_role_input` Pass 1 code / Pass 2 name 两轮匹配；最后 `set()` 去重；匹配后立即跑 #2.15 Permission Boundary 校验。

### 2.19 **ImportPreviewSession + preview_token：dry_run 与 execute 之间绑定（v2.2 P0：业务数据落 PostgreSQL，Redis 仅 cache）** — 用户 dry_run 后可能改 Excel 重传 / 切换账号 / 多 tab 操作 → 实际执行的数据不是预检过的数据。preview_token 强制绑定预检结果 + 文件存储路径，execute 时校验 `file_sha256 + records_hash + operator_id` 三重一致性，任一不匹配 → 拒绝执行。

**v2.2 P0 关键修订（Redis cache-only）**：原 v2.1 把 `preview_token → { file_storage_key, file_sha256, records_hash, summary, operator_id, expires_at }` 全部存 Redis，存在「Redis 丢失但 batch 表还在」的审计断裂问题（Redis 重启 / eviction / 容量满 → preview_token 失效但 DB 中 batch.status='CREATED' 永远停在那）。**v2.2 改为**：业务数据（hash / file_storage_key / summary / operator_id）全部存在 PostgreSQL `sys_user_import_batch` 表，Redis 仅缓存 `preview_token → batch_id` 映射（10min TTL，纯加速用）。

**两份存储职责**：

```python
# PostgreSQL sys_user_import_batch（v2.2 P0：业务数据 SoT）
{
    "batch_id": "uuid-xxx",
    "preview_token": "preview-xxx",
    "operator_id": 12345,
    "file_storage_key": "import-preview/abc-uuid.xlsx",
    "file_sha256": "abc...",
    "records_hash": "def...",
    "summary_new": 100, "summary_exists": 20,
    "summary_conflict": 5, "summary_out_of_scope": 3,
    "status": "CREATED",
    "created_at": "2026-08-01T14:00:00",
    "expires_at": "2026-08-01T14:10:00"   # 软过期标记，cron 扫描转 EXPIRED
}

# Redis user_import:preview:{preview_token}（v2.2 P0：cache only）
{
    "batch_id": "uuid-xxx"   # 仅此一个字段，TTL 600s
}
```

**execute 阶段（凭 token 反查 DB，Redis 仅做加速）**：

```python
# POST /import with { preview_token }（不带 file）
# 1. Redis 先查（hot path，加速）
cached = redis.get(f"user_import:preview:{preview_token}")
batch_id = cached["batch_id"] if cached else None

# 2. Redis miss → DB 反查（cold path，不丢业务数据）
if not batch_id:
    batch = await db.execute(
        select(UserImportBatch).where(UserImportBatch.preview_token == preview_token)
    ).scalar_one_or_none()
    if not batch:
        raise BusinessRuleException(error_code="AI_IMPORT_PREVIEW_INVALID")
    batch_id = batch.batch_id
    # 回填 Redis cache（TTL 重新计）
    redis.setex(f"user_import:preview:{preview_token}", 600, json.dumps({"batch_id": batch_id}))

# 3. DB 读完整 batch 行（业务数据 SoT）
batch = await db.get(UserImportBatch, batch_id)

# 4. 状态校验：必须是 CREATED（v2.2 #2.27 幂等保护）
if batch.status != "CREATED":
    raise BusinessRuleException(error_code="AI_IMPORT_ALREADY_EXECUTED")

# 5. TTL 软过期校验
if batch.created_at < datetime.now() - timedelta(minutes=10):
    # cron 没扫到之前先防御性拒绝
    raise BusinessRuleException(error_code="AI_IMPORT_PREVIEW_INVALID")

# 6. 凭 file_storage_key 取文件
file_bytes = await file_storage.read(batch.file_storage_key)

# 7. 三重校验：file_sha256 + records_hash + operator_id
if sha256(file_bytes) != batch.file_sha256: raise ...
records = await user_service.parse_import_excel(...)
if hash(records) != batch.records_hash: raise ...
if current_user.user_id != batch.operator_id: raise ...

# 8. CAS 状态机转换 CREATED → RUNNING（v2.2 #2.27）
```

**storage 实现**：
- **复用现有 `/file/upload` 通道**（推荐）：与 SR-24 `file.parse` 一致，file_storage_key 是 `/file/` 下的路径，统一管理 + TTL 清理复用现有逻辑
- **独立 `import-preview/` 目录**：与一般上传文件隔离，便于单独清理（10min 过期 vs 永久存储）
- **本 spec 选「独立目录」**：导入预览文件生命周期短（10min），不应混入永久文件存储；cron 清理 `/tmp/import/` 超过 1h 的文件

**反例**: (1) **preview_token 携带完整 records → 5000 行 Excel 序列化进 Redis 撑爆内存**（v2.1 已修）。(2) **业务数据存 Redis → Redis 重启 / eviction → preview_token 失效但 batch.status='CREATED' 在 DB 永远停着**（v2.2 P0 修订：业务数据落 PostgreSQL，Redis 仅 cache `batch_id`）。(3) execute 时让前端重传 file → preview_token 的"绑定"语义被削弱；用户删了临时文件 / 关 tab 后无法 execute。(4) 不绑定 operator_id → A 账号 dry_run 后 B 账号 execute，预检失效。(5) 不绑定 file_sha256 → 用户改 Excel 重传同一 token，权限校验失效。(6) Redis 命中后不再查 DB → Redis 数据被篡改 / TTL 错乱，绕过 DB 状态校验。

**回归**: `dry_run_import_users` 接 `file_bytes` + 持久化文件 + **INSERT sys_user_import_batch 行（含所有业务字段）+ 写 Redis cache（仅 batch_id）**；`POST /import` 不带 file，只带 `preview_token`；execute 先查 Redis 加速、miss 时反查 DB（**不丢业务数据**），然后状态校验 + 三重 hash 校验 + CAS 转 RUNNING；`/tmp/import/` cron 清理超过 1h 的孤儿文件；Redis 全量丢失 → execute 仍可从 DB 反查（性能降级但功能不丢）；测试 `test_preview_cache_missing_falls_back_to_db`。

### 2.20 **行级事务（savepoint）+ chunk 100 rows：失败行 ROLLBACK 当前行，不污染外层事务** — 「半成功」（#2.7）只说"失败行不阻断成功行"，但**没明确单行内的多步操作原子性**：user 创建成功但 user_role 失败 → 残留无角色用户脏数据。每行用 savepoint 包裹多步操作（create_user + create_roles + update_relation），失败 ROLLBACK 当前行；外层 chunk 100 rows 一个 transaction 控制 undo segment 大小。

**实现要点**：

```python
CHUNK_SIZE = 100   # 外层 transaction 每 100 行 commit 一次

for chunk in chunks(records, CHUNK_SIZE):
    async with db.begin():                          # chunk 级 transaction
        for row in chunk:
            try:
                async with db.begin_nested():       # 行级 savepoint
                    user = await user_service.create_user(db, ...)
                    await user_service.bind_roles(db, user.user_id, role_ids)
                    await user_service.bind_dept(db, user.user_id, dept_id)
                    # savepoint 退出时自动 RELEASE（成功）
            except Exception as e:
                # savepoint 自动 ROLLBACK 当前行（外层 chunk transaction 不受影响）
                failed_rows.append(FailedRow(
                    row_num=row.row_num,
                    field="...",
                    reason=str(e),
                    error_code=extract_error_code(e),
                ))
                continue   # 继续下一行
    # chunk 100 行 commit 一次（控制 undo segment + 锁持有时间）
```

**反例**: (1) 每行独立 session → 1000 行 = 1000 个 connection，连接池爆炸（FastAPI async + SQLAlchemy async 下尤其严重）。(2) 全批单 transaction（50000 行 BEGIN ... COMMIT）→ undo segment 几百 MB + 长锁 + 死锁风险。(3) 不用 savepoint 用裸 try/except → user 创建成功但 role 失败时 user 已 commit，无法 ROLLBACK → 脏数据。(4) **`except Exception` 太宽 → DB connection lost / deadlock / database unavailable 这种致命错误也被吞掉，50000 行继续跑，浪费资源 + 日志刷错**。

**回归**: chunk size 在 `settings.USER_IMPORT_CHUNK_SIZE`（默认 100）配置；行级 `db.begin_nested()`；外层 chunk commit；失败行 ROLLBACK savepoint 但 chunk transaction 继续；测试覆盖 `test_row_transaction_no_orphan_user`（user 创建成功后强制 role 绑定失败 → 验证 user 也回滚）。

#### 可恢复错误白名单（RECOVERABLE_ERROR_CODES）

`except Exception` 太宽，必须区分**可恢复错误**（继续下一行）vs **致命错误**（abort 整批）：

```python
# app/modules/system/service/user_service.py 顶部

RECOVERABLE_ERROR_CODES = frozenset({
    # 业务校验类（单行问题，不影响其他行）
    "AI_IMPORT_USERNAME_DUPLICATE",       # #2.25 并发冲突
    "AI_IMPORT_EMPLOYEE_NO_DUPLICATE",    # employee_no 并发冲突
    "AI_IMPORT_DEPT_NOT_FOUND",           # 部门反查失败
    "AI_IMPORT_DEPT_PATH_NOT_FOUND",
    "AI_IMPORT_DEPT_DUPLICATE",
    "AI_IMPORT_ROLE_NOT_FOUND",           # 角色反查失败
    "AI_IMPORT_DEPT_OUT_OF_SCOPE",        # data_scope 越界
    "AI_IMPORT_ROLE_OUT_OF_SCOPE",
    "AI_IMPORT_USERNAME_INVALID",         # 字段格式
    "AI_IMPORT_EMAIL_INVALID",
    "AI_IMPORT_PHONE_INVALID",
    "VALIDATION_ERROR",                   # Pydantic 通用校验失败
    "BusinessRuleException",              # 业务规则（catch-all 业务异常）
})
```

**实现**：

```python
async with db.begin_nested():
    try:
        await user_service.create_user(...)
        await user_service.bind_roles(...)
    except (BusinessException, IntegrityError) as e:
        # 业务/约束类异常 → 提取 error_code 判断是否可恢复
        code = extract_error_code(e)
        if code not in RECOVERABLE_ERROR_CODES:
            raise   # 不可恢复 → 让外层 chunk transaction 也 rollback + abort 整批
        # 可恢复 → savepoint 自动 ROLLBACK，进 failed_rows，继续下一行
        failed_rows.append(FailedRow(..., error_code=code))
    # 不接 Exception → DB connection lost / deadlock 等直接冒泡，外层 chunk rollback + 中断
```

**致命错误列表**（不接，直接 abort）：
- `OperationalError`（DB 连接断 / 数据库不可用 / deadlock detected）
- `InterfaceError`（连接池枯竭）
- `MemoryError` / `TimeoutError`（系统级）
- 任何未在 `RECOVERABLE_ERROR_CODES` 中的 `BusinessException`

**abort 行为**：致命错误 → chunk transaction ROLLBACK → 已 commit 的前 N-1 个 chunk 不受影响（部分提交）→ batch.status = `PARTIAL_SUCCESS`（部分成功）+ finished_at + failed_count 包含未跑的剩余行（reason="批量执行中断：{致命错误}"）→ HTTP 响应 200 + 提示用户"批次中断，已成功 X 行，请重试剩余 Y 行"。

**测试**：
- `test_recoverable_error_continues`：第 5 行 ROLE_NOT_FOUND → 后续行继续落库
- `test_unrecoverable_error_aborts`：模拟 OperationalError → chunk rollback + batch.status=PARTIAL_SUCCESS + finished_at 设置

### 2.21 **overwrite 字段白名单：永不覆盖 vs 可覆盖两组** — on_conflict=overwrite 不允许全字段覆盖，否则会把已存在用户的 `hashed_password` 也覆盖（破坏登录）、`user_name` 改了导致审计锚点丢失。

**两组字段**：

```python
OVERWRITE_NEVER = {
    "user_id",           # 主键，身份锚点
    "user_name",          # 登录账号，改了破坏审计 + 外部系统关联
    "hashed_password",     # 密码，导入用默认密码不能覆盖用户已改的密码
    "create_time",       # 创建时间，审计锚点
}

OVERWRITE_ALLOWED = {
    "nickname",
    "user_email",
    "user_phone",
    "dept_id",
    "role_ids",
    "user_gender",
    "status",
}
```

**AI tool / HTTP / 前端统一使用**：on_conflict=overwrite 时，service 层只更新 `OVERWRITE_ALLOWED` 中的字段，Excel 中的 user_name 列仅用于"识别已存在"（与已存在 user_name 不一致 → 跳过/报错，不强行覆盖 user_name）。

**反例**: (1) 全字段覆盖 → hashed_password 被默认密码覆盖，用户已改的密码失效（安全 + 体验双重灾难）。(2) allow user_name 覆盖 → user_id=1001 的 user_name 从 "zhang" 改 "li"，外部系统（HR / LDAP）的关联断裂。(3) 每个模块各自定义白名单 → 行为漂移，CLI generator 抽象时无法统一。

**回归**: `OVERWRITE_NEVER` / `OVERWRITE_ALLOWED` 常量在 `user_service.py` 顶部；`update_user` 内部用 `model_dump(exclude=OVERWRITE_NEVER)` 过滤；测试 `test_overwrite_password_not_changed`（传 hashed_password 进 overwrite payload → 验证 DB 中 hashed_password 未变）+ `test_overwrite_allowed_fields_only`（nickname/user_email 更新生效）。

### 2.22 **ImportBatchContext 在 dry_run 阶段创建（不只是 execute）** — 审计场景需要追踪"谁在什么时候上传了什么文件，预检结果如何，最终执行结果如何"——即使**用户 dry_run 后关闭浏览器没 execute**，这个上传 + 预检动作也应该有审计记录。所以 batch 记录在 dry_run 阶段就 INSERT，execute 阶段只 UPDATE 状态。

**生命周期**（详见 §2.26 状态机）：

```
dry_run 阶段：
  INSERT sys_user_import_batch (
    batch_id=uuid, status='CREATED',
    operator_id, filename, file_sha256, total_rows, on_conflict,
    preview_token, summary_new, summary_exists, summary_conflict, summary_out_of_scope,
    created_at=now()
  )
  + Redis 写 preview_token → file_storage_key 绑定（#2.19）

execute 阶段：
  UPDATE sys_user_import_batch SET status='RUNNING', started_at=now() WHERE batch_id=?
  ... chunk + savepoint 执行 ...
  UPDATE sys_user_import_batch SET
    status='SUCCESS' / 'PARTIAL_SUCCESS' / 'FAILED',
    success_count=?, skipped_count=?, overwritten_count=?, failed_count=?,
    failed_rows_file=?,
    finished_at=now()
  WHERE batch_id=?

10min TTL 过期（用户没 execute）：
  UPDATE sys_user_import_batch SET status='EXPIRED', finished_at=now()
  WHERE status='CREATED' AND created_at < now() - 10min
  （由定时任务或下次 dry_run 触发清理时批量更新）
```

**表结构**：

```python
class UserImportBatch(Base):
    __tablename__ = "sys_user_import_batch"
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_sha256: Mapped[str] = mapped_column(String(64))
    total_rows: Mapped[int] = mapped_column(Integer)
    # dry_run 阶段写入（预检摘要）
    preview_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    summary_new: Mapped[int] = mapped_column(Integer, default=0)
    summary_exists: Mapped[int] = mapped_column(Integer, default=0)
    summary_conflict: Mapped[int] = mapped_column(Integer, default=0)
    summary_out_of_scope: Mapped[int] = mapped_column(Integer, default=0)
    # execute 阶段写入（实际结果）
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    overwritten_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_rows_file: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="失败行 Excel 文件路径 /file/import-error/{batch_id}.xlsx，#2.22 文件化",
    )
    on_conflict: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True, comment="详见 §2.26 状态机")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # execute 开始
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True) # execute 完成 / EXPIRED
```

**Phase 3 异步通道复用**：表结构 + 状态机不变，execute 改异步时只是把 `RUNNING → SUCCESS/PARTIAL_SUCCESS` 的间隔拉长，`broadcast_to_user` 推 status 变化。

**反例**: (1) 只在 execute 创建 batch → dry_run 后关浏览器的动作无审计，安全盲区。(2) failed_rows 存 JSON 字段 → 50000 行失败几十 MB，DB 单行过大。(3) 不加 status 字段 → Phase 3 异步实现时改表，破坏向前兼容。(4) 不持久化只存 Redis → 审计 90 天反查丢失。

**回归**: alembic migration 新建表 + `preview_token` UNIQUE 索引 + `(operator_id, created_at)` / `(status, created_at)` 索引；`dry_run_import_users` 内 INSERT batch 行（status='CREATED'）；`batch_create_users_from_records` 内 UPDATE 状态流转；定时任务（或下次 dry_run 顺带）清理 `CREATED + created_at < now() - 10min` → `EXPIRED`；HTTP dry_run 响应含 `batchId`；正式导入响应含 `failedRowsDownloadUrl`。

### 2.22.1 **failed_rows 文件 retention = batch retention（90 天）** — failed_rows_file 的生命周期与 batch 行强绑定：删 batch 行时同时删文件，避免文件垃圾残留。

**实现**：

```python
# 定时任务（每日凌晨）：清理 90 天前的 batch + 关联文件
async def cleanup_expired_batches(db: AsyncSession):
    cutoff = datetime.now() - timedelta(days=90)
    old_batches = await db.execute(
        select(UserImportBatch).where(UserImportBatch.created_at < cutoff)
    )
    for batch in old_batches.scalars():
        if batch.failed_rows_file:
            await file_storage.delete(batch.failed_rows_file)  # 删失败文件
        # 同时清理 #2.19 的 preview 文件（如未过期残留）
        # /tmp/import/{file_storage_key} 超 1h 的孤儿文件单独 cron 清理
        await db.delete(batch)
    await db.commit()
```

**反例**: (1) 只删 DB 不删文件 → 文件垃圾堆积，磁盘满。(2) 只删文件不删 DB → DB 行的 failed_rows_file 指向不存在的文件，下载 404。(3) 用对象存储 S3 lifecycle 独立清理 → 与 DB 行脱钩，时序错乱。

**回归**: `cleanup_expired_batches` cron 函数（每日 02:00）；事务内删 batch 行 + 删关联文件（保证原子性）；测试 `test_cleanup_deletes_batch_and_file`。

### 2.23 **Export 改 POST（避免敏感 filter 进 nginx log / 浏览器历史）** — GET `/export?user_name=zhang&dept_id=1` 的 query 会进 nginx access log / 浏览器 history / APM 监控 → 用户名 / 部门等敏感筛选条件泄露。

**契约变更**：

```
原: GET /system/user/export?user_name=xxx&dept_id=1&status=1
改: POST /system/user/export
    Body: {"userName": "xxx", "deptId": 1, "status": "1"}
    Response: StreamingResponse (xlsx bytes) 或 422 (超阈值)
```

**前端配合**：不能用 `<a href="/export">` 直接触发下载，需 `fetch(POST)` + 创建 `Blob` + `URL.createObjectURL` + 隐藏 `<a>` 触发 click。浏览器下载管理器体验稍降（无法直接接管），但**安全性 > 体验**。

**反例**: (1) 保持 GET → 用户名 / 部门 ID 进 nginx log，IT 部门有完整查询历史。(2) 用 GET + header 传 filter → 仍然进 access log（header 也会被 log）。(3) 用 query param 加密 → 增加心智负担，且加密 key 泄露面扩大。

**回归**: API 改 POST + JSON body；service 层 `export_users_to_excel(filter: UserExportFilter, current_user)` 不变；前端 `user-export-button.ts` 用 fetch+blob 模式；测试 `test_post_export_streaming` 验证 POST 路径。

### 2.24 **`employee_no` 字段：sys_user 加 employee_no（UNIQUE，企业同步 / LDAP / ERP 对接，空值规范化）** — HR 月度同步 / LDAP 对接 / ERP 集成场景需要稳定的"员工工号"作为外部 ID 锚点；user_name 是登录账号可能改（虽然 #2.21 不让 overwrite 改），employee_no 是企业级稳定 ID。

**实现要点**：

```python
# sys_user 表 ALTER 加字段
employee_no: Mapped[str | None] = mapped_column(
    String(64), nullable=True, unique=True, index=True,
    comment="员工工号（企业同步 / LDAP / ERP 对接外部 ID）",
)
```

**空值规范化（必做）**：UNIQUE 约束下，多个 NULL 允许（SQL 标准），但**空字符串 `""` 会冲突**（视为非 NULL 值）。service 层强制规范化：

```python
def normalize_employee_no(raw: str | None) -> str | None:
    """空字符串 / 全空白 → None；strip 后存"""
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None   # "" → None
```

`parse_import_excel` 解析每行时调 `normalize_employee_no`；`update_user` 接收 employee_no 时同样规范化；现有 admin 手动建用户的 endpoint 也加同样逻辑（避免后续导入时撞历史空串）。

**数据迁移兜底**：alembic migration 中加一段 UPDATE，把现有 `employee_no=''` 的行改为 NULL：

```python
def upgrade():
    op.add_column("sys_user", sa.Column("employee_no", sa.String(64), nullable=True))
    op.execute("UPDATE sys_user SET employee_no = NULL WHERE employee_no = ''")
    op.create_unique_constraint("uq_sys_user_employee_no", "sys_user", ["employee_no"])
    op.create_index("ix_sys_user_employee_no", "sys_user", ["employee_no"])
```

**UserImportRecord 加 `employee_no` 列**：模板新增 `employee_no` 列（可选，但企业同步场景必填）；overwrite 时 employee_no 在 `OVERWRITE_ALLOWED`（HR 改工号场景）。

**v2.2 P1：employee_no 加 `sync_mode` 控制（防 HR 文件误覆盖）**：

```python
# app/modules/system/user/constants.py
class EmployeeNoSyncMode(str, enum.Enum):
    """employee_no 冲突时的处理策略（v2.2 P1）"""
    CREATE_ONLY = "CREATE_ONLY"            # 默认：employee_no 已存在 → 进 failed_rows（不覆盖）
    UPDATE_PROFILE = "UPDATE_PROFILE"      # employee_no 已存在 → 仅更新 OVERWRITE_ALLOWED 字段（不含 user_name）
    FULL_SYNC = "FULL_SYNC"                # employee_no 已存在 → 完整更新（含 user_name，谨慎用）
```

**为什么需要 sync_mode**（v2.2 P1 修订）：
- 默认 `OVERWRITE_ALLOWED` 含 employee_no 但**未说清「employee_no 命中已存在时如何识别」**：原 v2.1 用 user_name 命中（user_name 是 PRIMARY IDENTIFIER），但企业同步场景应该用 **employee_no** 命中（HR 文件用 employee_no 作主键，user_name 可能改）
- HR 月度同步 Excel 全量推送，employee_no 已存在的用户应该按 sync_mode 决定行为：
  - `CREATE_ONLY`（默认 / 最安全）：HR 第一次推 → 创建用户；第二次推同一员工 → **拒绝**（防误覆盖用户已改的 nickname/phone）
  - `UPDATE_PROFILE`：第二次推同一员工 → 更新 nickname/email/phone/dept/role（不动 user_name/password）
  - `FULL_SYNC`：第二次推 → 连 user_name 都覆盖（极端场景，HR 系统权威）

**命中规则**（v2.2 P1）：
1. Excel 一行 → 优先用 employee_no 匹配（如非 NULL）→ 命中则按 sync_mode 处理
2. employee_no 为 NULL → 退化到 user_name 匹配（CREATE_ONLY 模式行为不变）
3. 两个 employee_no 都为 NULL + user_name 一致 → 进 exists_records（按 on_conflict 处理）

**实现要点**：

```python
async def _resolve_existing_user(
    db: AsyncSession,
    record: UserImportRecord,
    sync_mode: EmployeeNoSyncMode,
) -> User | None:
    """v2.2 P1：按 employee_no（优先）→ user_name（兜底）匹配已存在用户"""
    if record.employee_no:
        existing = await db.scalar(
            select(User).where(User.employee_no == record.employee_no)
        )
        if existing:
            return existing   # 命中 employee_no，按 sync_mode 处理
    # employee_no 为空或未命中 → user_name 匹配
    return await db.scalar(
        select(User).where(User.user_name == record.user_name)
    )
```

**HTTP / AI 接口扩展**：

```
POST /system/user/import
Body 加：
  sync_mode: "CREATE_ONLY" | "UPDATE_PROFILE" | "FULL_SYNC"   # 可选，默认 CREATE_ONLY
```

**反例**: (1) **默认 FULL_SYNC → HR 第二次推 Excel 把所有用户的 user_name/password 都覆盖，破坏登录**（v2.2 P1：默认 CREATE_ONLY 防误覆盖）。(2) **只用 user_name 匹配 → HR 系统改了 user_name（zhangsan → zhangsan_new），HR Excel 仍用旧名 → 创建重复账号**（v2.2 P1：employee_no 优先匹配）。(3) 不区分 sync_mode → 所有导入场景统一行为，无法适配「首次创建」vs「月度同步」差异。(4) sync_mode 不在 Pydantic Literal → 任意字符串可传，DB 写入脏数据。

**回归**: `EmployeeNoSyncMode(Enum)`；`UserImportRecord` 不加 sync_mode（record 是一行数据，不是策略），sync_mode 在 `dry_run_import_users` / `batch_create_users_from_records` 函数签名；HTTP body 加 `sync_mode` query param；AI tool `user.import_preview` / `user.import_execute` 加 `sync_mode` 参数；默认 CREATE_ONLY；测试 `test_employee_no_create_only_rejects_existing` + `test_employee_no_update_profile_updates_safe_fields` + `test_employee_no_full_sync_overwrites_username` + `test_employee_no_null_falls_back_to_username_match`。

**反例（保留 v2.1 原内容）**: (1) 复用 user_name 作外部 ID → user_name 可能改（#2.21 虽禁 overwrite 但 admin 手动改仍可），外部系统关联断裂。(2) 复用 user_id（Snowflake）→ 外部系统不愿存 19 位整数，且对内变更不可控。(3) 不加 UNIQUE 约束 → HR 同步两次同一员工创建两个账号。(4) **不规范化空字符串 → 历史 admin 手建用户的 `employee_no=''` 行让导入时报 `AI_IMPORT_EMPLOYEE_NO_DUPLICATE`，用户困惑**。

**回归（保留 v2.1 原内容）**: alembic migration `ADD COLUMN + UPDATE 空串为 NULL + CREATE UNIQUE`；`normalize_employee_no` helper 在 `parse_import_excel` / `update_user` / 现有 admin 创建用户 endpoint 三处使用；`UserImportRecord.employee_no: str | None = None`；模板「数据」sheet 加 `employee_no` 列（可选）；`OVERWRITE_ALLOWED` 加入 `employee_no`；测试 `test_employee_no_unique_constraint` + `test_employee_no_blank_normalized_to_null`（传 `""` / `"   "` → 入库为 NULL，多次导入不冲突）。

### 2.25 **并发导入兜底：user_name UNIQUE 数据库层兜底，loser 进 FailedRow** — 两个管理员同时导同一 user_name → 数据库 `user_name UNIQUE` 约束触发 `IntegrityError`，loser 行的 savepoint 自动 ROLLBACK，进 `failed_rows` 继续；不影响其他行，不需要应用层加分布式锁。

**实现要点**：

```python
async with db.begin_nested():
    try:
        await user_service.create_user(db, ...)
    except IntegrityError as e:
        if "ix_sys_user_user_name" in str(e):
            failed_rows.append(FailedRow(
                row_num=row.row_num,
                field="user_name",
                value=row.user_name,
                reason=f"用户名已存在（可能并发导入）",
                error_code="AI_IMPORT_USERNAME_DUPLICATE",
            ))
            raise   # 触发 savepoint ROLLBACK
        raise       # 其他 IntegrityError 继续抛
```

**反例**: (1) 应用层加 Redis 分布式锁防并发 → 增加复杂度，且数据库 UNIQUE 已足够兜底。(2) 不区分 IntegrityError 类型 → role_binding 的 FK IntegrityError 也被误认为 user_name 冲突。(3) loser 直接抛 500 → 半成功策略失效，整批失败。

**回归**: service 层捕获 `IntegrityError` 并区分 `ix_sys_user_user_name` 约束名；新增错误码 `AI_IMPORT_USERNAME_DUPLICATE`；测试 `test_concurrent_same_user_name_import`（用 `asyncio.gather` 模拟两个并发 batch_create，验证其中一个的 user_name 行进 failed_rows）。

### 2.26 **Import Lifecycle State Machine：状态机集中定义 + Enum + CHECK 约束（v2.2 P0：Enum 化强制）** — 导入流程跨 Redis（preview cache）+ DB（batch state）+ execute（chunk/savepoint）三处状态，状态越来越多。集中定义状态机避免实现时漂移，且为 Phase 3 异步通道 / WebSocket 进度推送 / retry 机制预留扩展点。**v2.2 P0 强制 Enum 化 + DB CHECK 约束**：原 `String(32)` 字段无法在 DB 层防 typo（`'RUNING'` / `'SUCESS'`），且 Python 端散落字符串字面量。

**完整状态机**（v2.2 P0：加 `CANCELLED`，详见 #2.29）：

```
                          ┌──────────────────────────────────┐
                          │ (用户上传 Excel + dry_run)        │
                          └────────────┬─────────────────────┘
                                       ↓
                              ┌─────────────────┐
                              │     CREATED     │  INSERT batch 行（INSERT 瞬间）
                              │  (DB INSERT)    │  operator_id / filename / file_sha256
                              └────────┬────────┘
                                       │ dry_run 完成 + preview_token 生成 + Redis 写缓存
                                       ↓
                              ┌─────────────────┐
                              │  PREVIEW_DONE   │  等待用户确认（v2.2 P1-2 新增中间态）
                              │  (DB UPDATE)    │  summary / preview_token 已就位
                              │  (Redis cache)  │  TTL 10min
                              └────────┬────────┘
                                       │
                  ┌────────────┼─────────────┬─────────────┐
                  │            │             │             │
               用户确认     10min TTL     用户关 tab    用户取消 (v2.2)
                  │            │             │             │
                  ↓            ↓             ↓             ↓
          ┌──────────────┐  ┌──────────┐  (Redis TTL)  ┌──────────┐
          │   RUNNING    │  │ EXPIRED  │               │CANCELLED │
          │ (CAS UPDATE) │  │ (cron)   │               │ (CAS)    │
          └──────┬───────┘  └──────────┘               └──────────┘
                 │
    ┌────────────┼────────────┐
    │            │            │
 全部成功    部分 success    致命错误
    │            │            │
    ↓            ↓            ↓
┌─────────┐ ┌──────────────┐ ┌────────┐
│ SUCCESS │ │PARTIAL_SUCCESS│ │ FAILED │
└─────────┘ └──────────────┘ └────────┘
    │            │            │
    └────────────┼────────────┘
                 ↓
          90 天后归档删除
          (cleanup_expired_batches)
```

**v2.2 P1-2 状态机细分理由**（CREATED → PREVIEW_DONE）：把 INSERT 瞬间与 dry_run 完成瞬间分开，让 batch_log 更清晰（CREATED event vs PREVIEW_DONE event），且 `EXPIRED` cron 可只对 PREVIEW_DONE 状态扫（避免误扫刚 INSERT 还在 dry_run 解析中的行）。CREATED 是「行已建」的瞬态，PREVIEW_DONE 是「可执行」的稳态。

**Enum 定义（v2.2 P0 + P1-2）**：

```python
# app/modules/system/user/constants.py
import enum

class ImportBatchStatus(str, enum.Enum):
    """导入批次状态机（DB 层 CHECK 约束 + Python 层 Enum 双重保证）
    v2.2 P1-2：CREATED 拆为 CREATED（INSERT）+ PREVIEW_DONE（dry_run 完成可执行）
    """
    CREATED = "CREATED"                          # INSERT 瞬间（dry_run 解析中）
    PREVIEW_DONE = "PREVIEW_DONE"                # dry_run 完成，preview_token 已生成，等待用户确认（v2.2 P1-2）
    RUNNING = "RUNNING"                          # execute 进行中（chunk + savepoint）
    SUCCESS = "SUCCESS"                          # 全部成功
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"          # 部分成功（0 < success_count < total）
    FAILED = "FAILED"                            # 全部失败 / 致命错误
    EXPIRED = "EXPIRED"                          # PREVIEW_DONE 超 10min 未 execute（cron）
    CANCELLED = "CANCELLED"                      # 用户主动取消（v2.2 #2.29）

# 合法状态转换（_transition_batch_status 校验用）
LEGAL_TRANSITIONS: dict[ImportBatchStatus, frozenset[ImportBatchStatus]] = {
    ImportBatchStatus.CREATED: frozenset({
        ImportBatchStatus.PREVIEW_DONE,   # dry_run 完成（v2.2 P1-2）
        ImportBatchStatus.FAILED,         # dry_run 解析失败（文件损坏 / 0 行）
    }),
    ImportBatchStatus.PREVIEW_DONE: frozenset({
        ImportBatchStatus.RUNNING,        # 用户确认 execute
        ImportBatchStatus.EXPIRED,        # TTL 过期（cron）
        ImportBatchStatus.CANCELLED,      # 用户取消（v2.2 #2.29）
    }),
    ImportBatchStatus.RUNNING: frozenset({
        ImportBatchStatus.SUCCESS,
        ImportBatchStatus.PARTIAL_SUCCESS,
        ImportBatchStatus.FAILED,
    }),
    ImportBatchStatus.SUCCESS: frozenset(),       # 终态
    ImportBatchStatus.PARTIAL_SUCCESS: frozenset(),  # 终态
    ImportBatchStatus.FAILED: frozenset(),        # 终态
    ImportBatchStatus.EXPIRED: frozenset(),       # 终态
    ImportBatchStatus.CANCELLED: frozenset(),     # 终态
}
```

**ORM 字段（v2.2 P0）**：

```python
from sqlalchemy import Enum as SAEnum

class UserImportBatch(Base):
    # ...
    status: Mapped[ImportBatchStatus] = mapped_column(
        SAEnum(
            ImportBatchStatus,
            name="import_batch_status",
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,         # PostgreSQL 原生 ENUM 类型
            create_constraint=True,   # 同时建 CHECK constraint（SQLite/MySQL 兼容）
        ),
        nullable=False,
        index=True,
        default=ImportBatchStatus.CREATED,
        comment="状态机详见 §2.26",
    )
```

**Alembic 迁移**（PostgreSQL 原生 ENUM，v2.2 P1-2 加 PREVIEW_DONE）：

```python
def upgrade():
    # 1. 创建 PostgreSQL ENUM 类型（v2.2 P1-2 加 PREVIEW_DONE）
    import_batch_status = sa.Enum(
        "CREATED", "PREVIEW_DONE", "RUNNING", "SUCCESS", "PARTIAL_SUCCESS",
        "FAILED", "EXPIRED", "CANCELLED",
        name="import_batch_status",
    )
    import_batch_status.create(op.get_bind(), checkfirst=True)

    # 2. 加字段
    op.add_column("sys_user_import_batch", sa.Column(
        "status", import_batch_status,
        nullable=False, server_default="CREATED",
    ))
    op.create_index("ix_sys_user_import_batch_status",
                    "sys_user_import_batch", ["status"])

def downgrade():
    op.drop_index("ix_sys_user_import_batch_status", table_name="sys_user_import_batch")
    op.drop_column("sys_user_import_batch", "status")
    sa.Enum(name="import_batch_status").drop(op.get_bind(), checkfirst=True)
```

**状态转换 helper（CAS）**：

```python
async def _transition_batch_status(
    db: AsyncSession,
    batch_id: str,
    from_status: ImportBatchStatus,
    to_status: ImportBatchStatus,
    **updates,
) -> bool:
    """CAS 状态转换，防并发覆盖（v2.2 P0）。

    返回 True 表示转换成功，False 表示 from_status 不匹配（已被其他 worker 改）。
    """
    if to_status not in LEGAL_TRANSITIONS[from_status]:
        raise BusinessRuleException(
            f"非法状态转换 {from_status} → {to_status}",
            error_code="AI_IMPORT_ILLEGAL_TRANSITION",
        )
    set_clauses = {**updates, "status": to_status.value}
    result = await db.execute(
        update(UserImportBatch)
        .where(
            UserImportBatch.batch_id == batch_id,
            UserImportBatch.status == from_status.value,
        )
        .values(**set_clauses)
    )
    return result.rowcount == 1   # False → CAS 失败（其他 worker 已改）
```

**状态转换触发点**：

| 从 → 到 | 触发 | 实现位置 |
|---|---|---|
| → CREATED | dry_run 入口 INSERT batch 行（解析前） | `dry_run_import_users` 入口 INSERT |
| CREATED → PREVIEW_DONE | dry_run 完成 + preview_token 生成 + Redis 写缓存（v2.2 P1-2） | 同函数末尾 UPDATE + 写 Redis |
| CREATED → FAILED | dry_run 阶段文件解析失败 / 0 行（v2.2 P1-2） | `dry_run_import_users` 异常分支 |
| PREVIEW_DONE → RUNNING | 用户点确认 + preview_token 三重校验通过 + CAS（v2.2 P1-2） | `_transition_batch_status(PREVIEW_DONE → RUNNING, started_at=now())` |
| PREVIEW_DONE → EXPIRED | `created_at < now() - 10min` 定时任务扫描（v2.2 P1-2：扫 PREVIEW_DONE 而非 CREATED） | `cleanup_expired_previews` cron |
| PREVIEW_DONE → CANCELLED | 用户主动取消（v2.2 #2.29，v2.2 P1-2 改为 PREVIEW_DONE → CANCELLED） | `POST /import/{batch_id}/cancel` |
| RUNNING → SUCCESS | success_count = total_rows | chunk 执行完 `_transition_batch_status(RUNNING → SUCCESS, ...)` |
| RUNNING → PARTIAL_SUCCESS | 0 < success_count < total_rows，或致命错误中断 | 同上 |
| RUNNING → FAILED | success_count = 0，或 preview_token 校验失败 | 同上 |
| SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED → (deleted) | created_at + 90 天 | `cleanup_expired_batches` cron |

**Phase 3 异步通道扩展**：
- 加 `status='QUEUED'`（CREATED → QUEUED → RUNNING，execute 入队后立即返回）
- 加 `progress_percent` 字段（chunk 完成数 / total_chunks）让前端显示进度条
- 加 `broadcast_to_user` 在每次 status 变化时推送
- `ImportBatchStatus` 加 `QUEUED = "QUEUED"` 一行 + `LEGAL_TRANSITIONS[CREATED]` 加 `QUEUED`，向后兼容

**反例**: (1) 状态机散落在多决策记录里（#2.19 提 CREATED/RUNNING，#2.22 提 SUCCESS/PARTIAL_SUCCESS）→ 实现时漏状态转换。(2) 不预留 EXPIRED → 直接 Redis TTL 过期后 DB 行永远停在 CREATED，审计反查看到一堆"CREATED + created_at 一年前"的僵尸行。(3) **`status: String(32)` 无 DB 层校验 → 写入 `'RUNING'` typo，状态机查询永远查不到该行**（v2.2 P0：Enum + CHECK 强制）。(4) **Python 端散落字符串字面量 → 重构状态名时漏改**（v2.2 P0：`ImportBatchStatus` 集中）。(5) Phase 3 异步实现时重新设计状态机 → 破坏向前兼容。

**回归**: §2.26 集中状态机图 + `ImportBatchStatus(Enum)` + `LEGAL_TRANSITIONS` 映射；ORM 用 `SAEnum(native_enum=True, create_constraint=True)`；alembic 建 PostgreSQL ENUM 类型；转换逻辑集中在 `_transition_batch_status(db, batch_id, from, to, **updates)` helper（CAS 防并发覆盖 + 非法转换抛 `AI_IMPORT_ILLEGAL_TRANSITION`）；测试覆盖每条转换边 + Enum 类型写入 / 读取往返。

### 2.27 **Import Execute Idempotency：preview_token 只能 execute 一次（v2.2 P0）** — 用户网络抖动 / 双击「确认导入」按钮 / AI 重试 / 浏览器自动重试 POST → 同一 `preview_token` 被多次提交，若不做幂等保护，第二次 execute 会把已成功的用户再创建一遍（`username UNIQUE` 兜底进 `failed_rows`，但 chunk 已经又跑了一遍 savepoint + chunk commit，浪费资源 + 误报失败）。

**幂等实现（CAS + 状态机）**：

```python
async def batch_create_users_from_records(
    self,
    db: AsyncSession,
    *,
    preview_token: str,
    current_user: User,
    ...
) -> ImportResult:
    # 1. 凭 token 反查 batch（v2.2 #2.19 Redis cache + DB SoT）
    batch = await self._get_batch_by_preview_token(db, preview_token)

    # 2. 三重 hash + operator 校验（#2.19 不变）
    self._verify_three_way(batch, file_bytes, records, current_user)

    # 3. CAS 状态机转换 CREATED → RUNNING（v2.2 P0 幂等核心）
    cas_ok = await _transition_batch_status(
        db,
        batch.batch_id,
        ImportBatchStatus.CREATED,
        ImportBatchStatus.RUNNING,
        started_at=datetime.now(),
    )

    if not cas_ok:
        # CAS 失败：状态已被其他 worker 改（RUNNING / SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED）
        # → 这次 execute 是重复提交
        fresh = await db.get(UserImportBatch, batch.batch_id)
        if fresh.status in (ImportBatchStatus.SUCCESS, ImportBatchStatus.PARTIAL_SUCCESS):
            # 已成功过：返回原批次结果（幂等成功，不抛错）
            return ImportResult(
                success_count=fresh.success_count,
                skipped_count=fresh.skipped_count,
                overwritten_count=fresh.overwritten_count,
                failed_count=fresh.failed_count,
                failed_rows_file=fresh.failed_rows_file,
                batch_id=fresh.batch_id,
                idempotent_replay=True,   # 标记「这是重放，不是首次」
            )
        elif fresh.status == ImportBatchStatus.RUNNING:
            raise BusinessRuleException(
                "批次正在执行中，请等待",
                error_code="AI_IMPORT_BATCH_RUNNING",
            )
        else:
            # FAILED / EXPIRED / CANCELLED → 不能重放
            raise BusinessRuleException(
                f"批次已 {fresh.status.value}，不能重复执行",
                error_code="AI_IMPORT_ALREADY_EXECUTED",
            )

    # 4. CAS 成功 → 执行 chunk + savepoint 落库（独占状态）
    result = await self._do_chunk_and_savepoint(...)

    # 5. RUNNING → SUCCESS / PARTIAL_SUCCESS / FAILED
    await _transition_batch_status(
        db, batch.batch_id,
        ImportBatchStatus.RUNNING,
        end_status,   # SUCCESS / PARTIAL_SUCCESS / FAILED
        success_count=result.success_count,
        ...
        finished_at=datetime.now(),
    )
    return result
```

**幂等返回契约**：

| 重放时 `batch.status` | HTTP 响应 | 行为 |
|---|---|---|
| SUCCESS / PARTIAL_SUCCESS | 200 + `idempotent_replay=true` + 原结果 | 幂等成功，不重复落库 |
| RUNNING | 422 + `AI_IMPORT_BATCH_RUNNING` | 提示「请等待」 |
| FAILED | 422 + `AI_IMPORT_ALREADY_EXECUTED` | 失败批次不能重放（需要重新 dry_run） |
| EXPIRED / CANCELLED | 422 + `AI_IMPORT_ALREADY_EXECUTED` | 同上 |

**为什么不直接返回 409 / 422 错误**：用户「双击按钮」是常见误操作，应该静默幂等（返回 200 + 重放标记）而不是抛错让前端 toast 红色报错；只有真正异常（FAILED 重放）才抛错。

**反例**: (1) **不防双击 → 用户连续点 2 次「确认导入」，第二次因为 username UNIQUE 进 failed_rows，用户看到「失败 N 行」误以为导入失败**（v2.2 P0 核心场景）。(2) 用 Redis SETNX 做幂等锁 → Redis 重启 / eviction → 锁丢失，仍可重放。(3) CAS 失败一律抛错 → 双击用户体验差。(4) 不区分「成功重放」vs「失败重放」→ 失败批次也能重放，造成数据不一致（首次失败 100 行，第二次重放恰好环境恢复，100 行又被处理一遍，但前面可能已部分 commit）。(5) CAS 用 `version` 字段而非 `status` → 状态机和幂等两套机制，复杂度翻倍。

**回归**: `_transition_batch_status` 用 CAS `WHERE batch_id=? AND status='CREATED'`，0 rows 命中说明已被其他 worker 改；SUCCESS/PARTIAL_SUCCESS 重放返回 200 + `idempotent_replay=true`；FAILED/EXPIRED/CANCELLED 重放返回 422 + `AI_IMPORT_ALREADY_EXECUTED`；RUNNING 返回 422 + `AI_IMPORT_BATCH_RUNNING`；测试 `test_execute_same_token_twice_success_replay`（首次成功，第二次返回原结果 + idempotent_replay=true）+ `test_execute_same_token_twice_running_concurrent`（并发 execute 同 token，第二个抛 BATCH_RUNNING）+ `test_execute_failed_batch_rejected`（FAILED 状态重放被拒绝）+ `test_concurrent_execute_same_batch`（asyncio.gather 模拟并发，验证 CAS 互斥）。

### 2.28 **ImportBatch 业务日志表 `sys_user_import_batch_log`（v2.2 P1）** — batch 是长期对象（CREATED → 90 天归档），单一 `status` 字段只反映最新状态，**丢失中间过程**（什么时候 RUNNING → SUCCESS？哪个 chunk 失败？EXPIRED 是 cron 扫的还是用户手动取消？）。HTTP import 走 `sys_operation_log`、AI import 走 `ai_operation_log` 都是「调用维度」审计，无法回答「batch X 经历了哪些状态」。**v2.2 P1 新增 `sys_user_import_batch_log` 表**，按 batch 维度记录状态转换节点。

**表结构**：

```python
class UserImportBatchLog(Base):
    """批次操作日志（v2.2 P1）— 按 batch 维度记录状态转换 + 关键节点"""
    __tablename__ = "sys_user_import_batch_log"
    log_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Snowflake ID
    batch_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sys_user_import_batch.batch_id", ondelete="CASCADE"),
        index=True,
    )
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(
        String(32),
        comment="事件类型：CREATED/PREVIEW_DONE/EXECUTE_START/CHUNK_PROGRESS/"
                "EXECUTE_FINISH/EXECUTE_FAILED/EXPIRED/CANCELLED",
    )
    from_status: Mapped[ImportBatchStatus | None] = mapped_column(
        SAEnum(ImportBatchStatus), nullable=True,
    )
    to_status: Mapped[ImportBatchStatus | None] = mapped_column(
        SAEnum(ImportBatchStatus), nullable=True,
    )
    detail: Mapped[dict] = mapped_column(
        JSON,
        comment="事件详情：chunk_index / chunk_size / failed_in_chunk / error_message / reason 等",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
```

**事件类型**（event 字段取值）：

| event | 触发时机 | detail 内容 |
|---|---|---|
| `CREATED` | dry_run 完成 INSERT batch 行 | `{filename, total_rows, on_conflict, sync_mode, summary}` |
| `PREVIEW_DONE` | preview_token 写 Redis cache 后 | `{preview_token, expires_at}` |
| `EXECUTE_START` | CAS CREATED → RUNNING 成功 | `{operator_id, started_at}` |
| `CHUNK_PROGRESS` | 每个 chunk 100 行 commit 后 | `{chunk_index, total_chunks, success_in_chunk, failed_in_chunk}` |
| `EXECUTE_FINISH` | chunk 全部跑完 + 状态转 SUCCESS/PARTIAL_SUCCESS/FAILED | `{success_count, skipped_count, failed_count, finished_at, duration_ms}` |
| `EXECUTE_FAILED` | 致命错误中断（OperationalError 等） | `{error_code, error_message, chunk_index_where_failed}` |
| `EXPIRED` | cron 扫到 CREATED + 10min | `{expired_by: "cron", original_created_at}` |
| `CANCELLED` | 用户 POST /cancel | `{cancelled_by: operator_id, reason}` |

**写入策略**：
- 每次状态转换都写一行 log（`_transition_batch_status` 内部 `INSERT log + UPDATE batch` 在同一事务）
- `CHUNK_PROGRESS` 默认每 chunk 写一行（100 行/chunk，2000 行 = 20 条 log，可接受）
- `detail` 用 JSON 字段（PostgreSQL JSONB），不同 event 携带不同字段

**索引**：
- `(batch_id, created_at)` — 反查 batch X 的完整状态转换历史
- `(event, created_at)` — 类型查询（例如「所有 EXECUTE_FAILED 批次」）
- `(operator_id, created_at)` — admin 查「我经手的批次」

**API 查询接口**（v2.2 P2 §5）：

```
GET /system/user/import/{batch_id}/logs
权限：system:user:list
返回：[{event, fromStatus, toStatus, detail, createdAt}, ...]
```

**Phase 3 异步通道扩展**：CHUNK_PROGRESS 推送 WebSocket 时直接读 log 表 → 前端进度条 = log 表 CHUNK_PROGRESS 行的 `chunk_index / total_chunks`。

**反例**: (1) **不建 log 表 → batch.status='FAILED' 时管理员不知道是 chunk 5 失败还是 chunk 19 失败**（v2.2 P1 修订核心）。(2) detail 用 String → 不同 event 字段不同，序列化困难。(3) log 表用 sys_operation_log 复用 → batch_id 维度查询需要扫全表，性能差。(4) 不写 CHUNK_PROGRESS → Phase 3 异步进度条无法实现。(5) ondelete=RESTRICT → 删 batch 行时残留 log 垃圾，必须 CASCADE。

**回归**: alembic migration 建 `sys_user_import_batch_log` 表 + 3 个索引 + FK CASCADE；`_transition_batch_status` 同事务 INSERT log 行；CHUNK_PROGRESS 在每个 chunk commit 后 INSERT（同事务）；EXPIRED 由 cron 写；CANCELLED 由 cancel endpoint 写；API `GET /import/{batch_id}/logs` 查询；测试 `test_log_records_all_lifecycle_events`（dry_run → execute → chunk → finish 全流程跑一遍，验证 log 行覆盖所有 event）+ `test_log_cascade_delete_with_batch`（删 batch 自动删 log）。

### 2.29 **导入取消能力（v2.2 P2：CREATED 直接 cancel / RUNNING 协作式 cancel）** — 用户 dry_run 后改主意不想导了 / 上传错文件 / 发现数据有问题 → 应该能主动取消批次，而不是等 10min TTL 过期。**v2.2 P2 加 `CANCELLED` 终态 + cancel API**。

**两种取消场景**：

**场景 1：CREATED 状态取消（简单）**
- 用户上传文件 → dry_run 完成 → status=CREATED → 用户点「取消」
- 直接 CAS `CREATED → CANCELLED`，删除 preview 文件 + Redis cache
- 不影响任何用户数据（CREATED 阶段还没落库）

**场景 2：RUNNING 状态取消（协作式 cancel，复杂）**
- 用户点确认 → status=RUNNING → chunk 1 完成 → chunk 2 进行中 → 用户点「取消」
- **不能强杀 chunk transaction**（PostgreSQL 无法 cancel 进行中的 transaction）
- **协作式 cancel**：cancel endpoint 设置 `cancel_requested=True` 标志（用 Redis 或内存），chunk 之间检查标志，下一个 chunk 开始前拒绝执行
- 已 commit 的 chunk 保留（无法回滚），剩余 chunk 不执行 → status 转 `PARTIAL_SUCCESS`（或 `CANCELLED`，详见下方 trade-off）

**实现要点**：

```python
async def cancel_batch(
    db: AsyncSession,
    batch_id: str,
    operator: User,
    reason: str | None = None,
) -> UserImportBatch:
    batch = await db.get(UserImportBatch, batch_id)
    if not batch:
        raise BusinessRuleException(error_code="AI_IMPORT_BATCH_NOT_FOUND")
    # 权限：必须是 operator 本人或超管
    if batch.operator_id != operator.user_id and not is_super_admin(operator):
        raise AuthorizationException("无权取消此批次")

    if batch.status == ImportBatchStatus.CREATED:
        # 场景 1：简单 cancel
        ok = await _transition_batch_status(
            db, batch_id, ImportBatchStatus.CREATED, ImportBatchStatus.CANCELLED,
            finished_at=datetime.now(),
        )
        if not ok:
            raise BusinessRuleException(error_code="AI_IMPORT_BATCH_NOT_CANCELLABLE")
        # 写 log + 清理 preview 文件 + Redis cache
        await self._write_log(batch, "CANCELLED", reason=reason)
        await file_storage.delete(batch.file_storage_key)
        redis.delete(f"user_import:preview:{batch.preview_token}")
        return batch

    elif batch.status == ImportBatchStatus.RUNNING:
        # 场景 2：协作式 cancel
        # 设置 cancel_requested 标志（Redis，TTL 1h）
        redis.setex(f"user_import:cancel:{batch_id}", 3600, "1")
        # batch_create_users_from_records 内每个 chunk 之间检查标志
        # 下一个 chunk 开始前检测到标志 → 跳出循环 → 状态转 PARTIAL_SUCCESS（已 commit 的 chunk 保留）
        # 异步返回（cancel 请求立即 200，不等待 chunk 真的暂停）
        return batch

    else:
        # SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED 都不可取消
        raise BusinessRuleException(error_code="AI_IMPORT_BATCH_NOT_CANCELLABLE")
```

**RUNNING → CANCELLED 还是 PARTIAL_SUCCESS？**（trade-off）
- **CANCELLED**：状态语义清晰（用户主动取消），但已 commit 的 chunk 用户可能误以为没生效
- **PARTIAL_SUCCESS**：已 commit 的 chunk 算「部分成功」，cancel 标志只让剩余 chunk 不跑
- **本 spec 选 PARTIAL_SUCCESS**（v2.2 P2）：cancel 标志作用是「停止后续 chunk」，已 commit 的 chunk 数据已落库无法回滚，PARTIAL_SUCCESS 更符合实际数据状态；`detail: {cancelled: true, cancelled_by: operator_id, remaining_chunks_skipped: N}` 在 batch_log 中标记

**协作式 cancel 检查点**（batch_create 内）：

```python
for chunk_index, chunk in enumerate(chunks(records, CHUNK_SIZE)):
    # v2.2 #2.29：协作式 cancel 检查
    if redis.exists(f"user_import:cancel:{batch_id}"):
        # 写 log 标记 cancel
        await self._write_log(batch, "CHUNK_PROGRESS", {
            "cancelled": True,
            "remaining_chunks_skipped": total_chunks - chunk_index,
        })
        break   # 跳出 chunk 循环，状态转 PARTIAL_SUCCESS

    async with db.begin():   # chunk transaction
        for row in chunk:
            async with db.begin_nested():   # savepoint
                ...
```

**反例**: (1) **RUNNING 状态强杀 PostgreSQL transaction → 不可能，PostgreSQL 不支持**（v2.2 P2：协作式 cancel 是唯一可行方案）。(2) cancel 请求同步等待 chunk 真的暂停 → HTTP 请求超时，用户体验差（v2.2 P2：cancel 立即返回 200，下一个 chunk 之间才生效）。(3) cancel 后状态转 CANCELLED → 已 commit 的 chunk 用户以为没生效，重新导入会撞 UNIQUE 约束（v2.2 P2：转 PARTIAL_SUCCESS + log 标记 cancelled=true，数据状态准确）。(4) 任何状态都能 cancel → SUCCESS 的 batch 被 cancel → 状态被破坏（v2.2 P2：SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED 终态拒绝 cancel）。

**回归**: `ImportBatchStatus.CANCELLED` 加入状态机（v2.2 #2.26 已含）；`POST /import/{batch_id}/cancel` endpoint；CREATED 直接 CAS 转 CANCELLED + 清理 preview 文件；RUNNING 设置 Redis cancel_requested 标志 + chunk 之间检查 + 跳出循环转 PARTIAL_SUCCESS；权限校验（operator 本人或超管）；终态拒绝 cancel 抛 `AI_IMPORT_BATCH_NOT_CANCELLABLE`；测试 `test_cancel_created_batch_cancels` + `test_cancel_running_batch_stops_after_current_chunk` + `test_cancel_terminal_batch_rejected` + `test_cancel_by_non_operator_forbidden`。

### 2.30 **批量操作强制 `reason` 审计参数（v2.2 P1-3）** — 批量写入操作（import_execute / cancel / export）必须携带 `reason: str` 业务理由（导入场景例：「2026年8月 HR 入职名单同步」/「ERP 集成全量推送」），写入 `sys_user_import_batch.reason` 字段 + `sys_user_import_batch_log.detail.reason`，进入审计链路（与 `sys_operation_log` / `ai_operation_log` 联动），便于事后反查「为什么 2026-08-03 凌晨 02:00 导入了 1500 个用户」。

**字段定义**：

```python
# UserImportBatch 加字段
class UserImportBatch(Base):
    # ...
    reason: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="操作理由（v2.2 P1-3）：批量操作的业务背景，进入审计链路",
    )
```

**API / AI tool 契约**：

```python
# HTTP
POST /system/user/import
Body 加：
  reason: str   # 必填，1-256 字符，业务理由

# AI tool（v2.2 P1-3）
async def user_import_preview(
    ctx: AiToolContext,
    *,
    file_path: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: EmployeeNoSyncMode = EmployeeNoSyncMode.CREATE_ONLY,
    reason: str,   # v2.2 P1-3 必填
) -> ToolResult: ...

async def user_import_execute(
    ctx: AiToolContext,
    *,
    preview_token: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: EmployeeNoSyncMode = EmployeeNoSyncMode.CREATE_ONLY,
    reason: str,   # v2.2 P1-3 必填（与 preview 一致或重新填）
) -> ToolResult: ...
```

**为什么 import_preview 也要求 reason**：preview 阶段已经写 batch 行 + Redis cache，是审计追溯起点；不要求 reason 会导致「先 preview 看看」的操作无理由记录，execute 时再补理由，审计链路不完整。**preview 与 execute 共享同一 reason**（同一次业务操作），不允许中途改理由。

**校验**：
- HTTP：Pydantic `Field(min_length=1, max_length=256)`，空字符串 / 全空白拒绝
- AI tool：同样长度校验，失败抛 `BusinessRuleException(error_code="AI_IMPORT_REASON_REQUIRED")`
- execute 阶段校验 `reason == batch.reason`（防止用户 preview 时填 A，execute 时填 B，绕过一致性）

**LLM Prompt 引导**（system prompt 追加）：
> 调用 user.import_preview / user.import_execute 时必须填写 `reason` 参数（业务背景，1-256 字符）。例如：「2026年8月 HR 入职名单同步」「ERP 全量推送」「离职用户批量禁用」。

**写入位置**：
- `dry_run_import_users` 阶段：INSERT batch 行时写 `reason`，写 batch_log CREATED event（detail.reason）
- `batch_create_users_from_records` 阶段：CAS PREVIEW_DONE → RUNNING 时校验 reason 一致，写 batch_log EXECUTE_START event（detail.reason）
- audit 反查：`sys_operation_log.path='/import'` + `body.reason` ↔ `sys_user_import_batch.reason` ↔ `sys_user_import_batch_log.detail.reason` 三方对照

**反例**: (1) **reason 可选 → 用户 99% 不填，审计数据稀疏，事后反查失效**（v2.2 P1-3：必填，1-256 字符）。(2) preview 不要求 reason 只 execute 要求 → preview 时的 batch 行无理由记录，execute 后 reason 才补上，审计链路断裂（v2.2 P1-3：preview 阶段就必填，execute 阶段校验一致）。(3) reason 无长度限制 → 用户填 10KB 文本撑大 DB 行 + JSON log 膨胀（v2.2 P1-3：256 字符上限）。(4) reason 不进 batch_log.detail → log 表查不到理由，反查必须 JOIN batch 表，复杂度高（v2.2 P1-3：log.detail.reason 冗余写入，单表查询即可）。

**回归**: `UserImportBatch.reason: str` 必填字段（alembic migration 含）；HTTP body 加 `reason`；AI tool 加 `reason` 必填参数；Pydantic `Field(min_length=1, max_length=256)`；execute 阶段校验 reason 一致；batch_log CREATED / EXECUTE_START / CANCELLED event 写 `detail.reason`；测试 `test_reason_required_validation` + `test_reason_max_length_256` + `test_reason_mismatch_between_preview_and_execute_rejected` + `test_reason_persisted_in_batch_log`。

### 2.31 **ExportTask 审计表（v2.2 P1-5：导出与导入对称设计）** — 导入有 `sys_user_import_batch` 表（dry_run → execute 全生命周期 + chunk 进度 + failed_rows 文件 + 90 天归档），导出当前完全无审计 → 「HR 在 2026-08-03 凌晨导出全公司通讯录」这种**高风险数据外流动作无任何记录**。AI 场景尤其危险：AI `user.export` 可能被诱导导出敏感数据（如「导出所有管理员账号 + 邮箱」），无审计无追溯。**v2.2 P1-5 加 `sys_user_export_task` 表**，所有导出（HTTP 同步 / HTTP 异步 / AI）一律建任务记录。

**触发建任务的场景**（v2.2 P1-5）：
1. **HTTP 同步导出**（行数 ≤ 5000）：建任务 → 行数 / filter_snapshot / status=SUCCESS / 耗时 / file_key（即使同步立即返回也要建任务）
2. **HTTP 异步导出**（行数 > 5000，Phase 3）：建任务 → status=QUEUED → RUNNING → SUCCESS / FAILED
3. **AI `user.export`**（任何行数）：强制建任务（高风险审计），与 #2.30 reason 字段联动

**与 import batch 表的对称设计**：

| 维度 | `sys_user_import_batch` | `sys_user_export_task` |
|---|---|---|
| 用途 | 用户导入生命周期 | 用户导出生命周期 |
| 触发 | HTTP / AI / Phase 3 异步 | HTTP / AI / Phase 3 异步 |
| 状态机 | CREATED → PREVIEW_DONE → RUNNING → SUCCESS/... | CREATED → RUNNING → SUCCESS / FAILED / EXPIRED |
| 文件 | failed_rows.xlsx（失败行） | export.xlsx（导出结果） |
| 审计 | operator_id + reason | operator_id + reason + filter_snapshot |
| Retention | 90 天 | 30 天（导出文件含敏感数据，更短） |
| 业务日志 | sys_user_import_batch_log | sys_user_export_task_log（Phase 3 异步时启用，同步无需） |

**为什么导出不需要 batch_log**：
- 导出无 chunk 概念（一次 query + streaming），无中间进度
- 同步导出只有 CREATED → SUCCESS 一跳，log 表过载
- Phase 3 异步导出再加 log 表（参考 import 设计）

**ExportTask ORM**：

```python
# app/modules/system/user/models.py（v2.2 P1-5）
import enum

class ExportTaskStatus(str, enum.Enum):
    """导出任务状态机（v2.2 P1-5）"""
    CREATED = "CREATED"           # 任务创建（同步路径瞬间 → SUCCESS；异步路径排队）
    RUNNING = "RUNNING"           # 异步执行中（同步路径瞬态）
    SUCCESS = "SUCCESS"           # 完成，file_key 可下载
    FAILED = "FAILED"             # 失败（filter 错 / 行数超阈值 / DB 错）
    EXPIRED = "EXPIRED"           # 文件超 30 天 / 已被删

class UserExportTask(Base):
    """用户导出任务审计（v2.2 P1-5）"""
    __tablename__ = "sys_user_export_task"
    export_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Snowflake ID
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filter_snapshot: Mapped[dict] = mapped_column(
        JSON,
        comment="filter 快照（防用户事后改 filter 反查时漂移）",
    )
    # v2.2 P1-5：与 #2.30 联动
    reason: Mapped[str] = mapped_column(
        String(256), nullable=False,
        comment="操作理由（与 import batch.reason 对称）",
    )
    # 结果
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_storage_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="导出文件 storage_key（FileStorage Protocol，#3.9）",
    )
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 状态机
    status: Mapped[ExportTaskStatus] = mapped_column(
        SAEnum(ExportTaskStatus, name="export_task_status",
               values_callable=lambda x: [e.value for e in x],
               native_enum=True, create_constraint=True),
        nullable=False, index=True, default=ExportTaskStatus.CREATED,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

**索引**：
- `(operator_id, created_at)` — admin 查「我导出过的列表」
- `(status, created_at)` — 30 天归档 cron
- `(created_at)` — Retention 扫描

**filter_snapshot 字段重要性**（v2.2 P1-5）：
- 导出记录 `filter={dept_id: 1, status: "1"}` 时 DB 中 dept_id=1 有 200 个用户
- 用户事后改 dept_id=1 的部门配置（加入更多子部门），事后反查时 DB 看到 dept_id=1 有 350 个用户
- **filter_snapshot 冻结当时的 filter + 部门结构**（dept_name / parent_path / 子部门 ids），审计反查准确还原「当时导出了什么」
- 不仅是 filter dict，还要含「当时 accessible_dept_ids 解析后的具体部门 ID 集合」

**HTTP 同步导出流程改造**：

```python
async def export_users_to_excel(self, db, filter, current_user, reason: str):
    # 1. 建 ExportTask（CREATED）
    task = UserExportTask(
        operator_id=current_user.user_id,
        filter_snapshot={
            "filter": filter.model_dump(),
            "accessible_dept_ids": list(await self._get_accessible_dept_ids(db, current_user)),
            "filter_evaluated_at": datetime.now().isoformat(),
        },
        reason=reason,
        status=ExportTaskStatus.CREATED,
    )
    db.add(task)
    await db.flush()   # 拿 export_id

    # 2. 同步执行（status CREATED → RUNNING → SUCCESS）
    task.status = ExportTaskStatus.RUNNING
    task.started_at = datetime.now()

    try:
        rows = await self._query_users_with_data_scope(db, filter, current_user)
        xlsx_bytes = self._build_excel(rows, EXPORT_ALLOWED_FIELDS)

        # 3. 写文件（FileStorage Protocol，#3.9）
        storage_key = await self._file_storage.save(
            xlsx_bytes,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            namespace="user-export",
            suffix=".xlsx",
            ttl_seconds=30 * 86400,   # 30 天
        )

        task.status = ExportTaskStatus.SUCCESS
        task.row_count = len(rows)
        task.file_storage_key = storage_key
        task.file_size_bytes = len(xlsx_bytes)
        task.finished_at = datetime.now()
        task.duration_ms = int((task.finished_at - task.started_at).total_seconds() * 1000)

        # 4. 同步路径：直接 streaming 返回 bytes（task 已建审计）
        return xlsx_bytes, len(rows), task.export_id

    except Exception as e:
        task.status = ExportTaskStatus.FAILED
        task.error_code = extract_error_code(e)
        task.error_message = str(e)[:1024]
        task.finished_at = datetime.now()
        raise

# Phase 3 异步路径：build → 入队 → broadcast 推送下载链接（结构已就位）
```

**API 契约扩展**：

```python
# HTTP POST /system/user/export 加 reason（与 import 对称）
POST /system/user/export
Body: {
  "userName": "xxx",
  "deptId": 1,
  "status": "1",
  "reason": "全公司通讯录月度归档"   # 必填（v2.2 P1-5 + P1-3 #2.30）
}

# HTTP GET /system/user/export/{export_id}（v2.2 P1-5 新增）
权限：system:user:list
返回：导出任务详情（status / row_count / file_storage_key / filter_snapshot / reason / created_at / 可选下载 URL）

# HTTP GET /system/user/export（v2.2 P1-5 新增）
权限：system:user:list
返回：导出任务列表分页（按 operator_id / status / created_at 过滤）

# AI tool（v2.2 P1-5）
async def user_export(
    ctx: AiToolContext,
    *,
    user_name: str | None = None,
    dept_id: int | None = None,
    status: str | None = None,
    reason: str,   # v2.2 P1-5 + P1-3 必填
) -> ToolResult:
    """导出后返回 detail_card：{exportId, rowCount, fileSize, downloadUrl, expiresAt}

    AI 场景：高风险数据外流，强制 reason + 建审计任务 + 30 天后自动删除
    """
```

**反例**: (1) **同步导出不建任务 → 「HR 凌晨导出 5000 行通讯录」无任何 DB 记录，事后无法追溯**（v2.2 P1-5 核心：所有导出一律建任务）。(2) filter_snapshot 只存 filter dict → 用户事后改部门结构，审计反查数据漂移（v2.2 P1-5：snapshot 含 accessible_dept_ids 解析结果 + filter_evaluated_at 时间戳）。(3) ExportTask 与 ImportBatch 表结构完全相同 → 字段语义不同（export 无 chunk / preview_token，import 无 filter_snapshot），强行复用一张表混淆职责。(4) 不加 reason 字段 → 与 #2.30 import 不对称，审计链路有缺口（v2.2 P1-5 + P1-3：export 也必填 reason）。(5) 同步路径跳过 task 创建 → 「同步即成功」的导出无审计（v2.2 P1-5：同步也要建，只是 status 瞬间 CREATED → SUCCESS）。(6) 文件永久存储 → 累积敏感数据导出文件，磁盘 + 合规风险（v2.2 P1-5：30 天 TTL，比 import 90 天更短，因含全量字段）。

**回归**: `sys_user_export_task` 表（alembic migration）+ `ExportTaskStatus(Enum)` + `UserExportTask` ORM + `filter_snapshot` JSONB 字段（含 accessible_dept_ids 解析）；HTTP `POST /export` 加 `reason` 必填；HTTP `GET /export/{export_id}` + `GET /export` 列表查询；AI `user.export` 加 `reason` 必填 + 强制建 task；同步路径建 task → 瞬间 SUCCESS；30 天 TTL cron（与 `cleanup_expired_batches` 类似）；测试 `test_export_creates_task_with_filter_snapshot` + `test_export_filter_snapshot_freezes_accessible_dept_ids` + `test_export_reason_required` + `test_export_task_30_day_retention` + `test_export_failure_records_error` + `test_export_audit_chain_joinable_with_operation_log`。

---

## 3. 数据模型（含 1 张新表 `sys_user_import_batch` + sys_user 加 `employee_no` 字段）

### 3.1 `UserImportRecord`（一行导入数据）

```python
class UserImportRecord(BaseModel):
    """Excel 一行 → 一个 record（必填字段校验在 schema 层）"""
    row_num: int                          # Excel 行号（错误定位用）
    user_name: str                         # 必填
    employee_no: str | None = None        # 可选；员工工号（企业同步 / LDAP / ERP 对接，#2.24）
    nickname: str | None = None
    user_email: str | None = None
    user_phone: str | None = None
    dept_input: str                       # 必填；部门名 or 完整路径（详见 #2.17）
    role_input: str | None = None         # 可选；逗号分隔 code/name 混合（详见 #2.18）
    user_gender: Literal["0", "1", "2"] = "0"  # 默认未知
    status: Literal["0", "1"] = "1"       # 默认启用
```

### 3.2 `ImportDryRunResult`（预检结果，v2.2 P1：records 限流 + 大批走文件下载）

```python
# app/modules/system/user/constants.py
MAX_PREVIEW_RECORDS = 2000   # v2.2 P1：与 USER_IMPORT_MAX_ROWS 对齐，预检结果最多展示 2000 行

class ImportDryRunResult(BaseModel):
    total: int
    # v2.2 P1：records 改为「截断展示」+ 全量文件下载
    new_records: list[UserImportRecord]      # 截断到 MAX_PREVIEW_RECORDS（前端展示）
    exists_records: list[UserImportRecord]
    conflict_records: list[FailedRow]
    out_of_scope_records: list[FailedRow]
    # v2.2 P1：4 个 *_truncated 标志，让前端显示「只展示前 2000 行，详见文件」
    new_records_truncated: bool = False
    exists_records_truncated: bool = False
    conflict_records_truncated: bool = False
    out_of_scope_records_truncated: bool = False
    # v2.2 P1：失败行（conflict + out_of_scope）写文件供下载
    conflict_records_file: str | None = None       # /file/import-preview/{batch_id}/conflicts.xlsx
    out_of_scope_records_file: str | None = None

    @property
    def new_count(self) -> int: return len(self.new_records)             # 截断后长度（前端展示用）
    @property
    def total_new_count(self) -> int                                    # v2.2 P1：原始总数（不含截断）
    @property
    def exists_count(self) -> int: return len(self.exists_records)
    @property
    def total_exists_count(self) -> int
    @property
    def conflict_count(self) -> int: return len(self.conflict_records)
    @property
    def total_conflict_count(self) -> int
    @property
    def out_of_scope_count(self) -> int: return len(self.out_of_scope_records)
    @property
    def total_out_of_scope_count(self) -> int
```

**限流策略**（v2.2 P1）：
- `USER_IMPORT_MAX_ROWS = 2000`（#2.10），所以 `new_records + exists_records` 总和 ≤ 2000 → `new_records` / `exists_records` 通常不会触发截断
- 但 `conflict_records` + `out_of_scope_records` 可能 100% 都是失败行（2000 行全错）→ 必须截断到前 200 行展示，剩余进 `conflict_records_file` / `out_of_scope_records_file`
- 前端展示文案：`conflict_records_truncated=true` 时显示「⚠️ 仅展示前 200 条冲突，完整清单下载 conflicts.xlsx」

**为什么 new/exists 也加 truncated 字段**（防御性）：
- 未来 Phase 3 异步通道上线后 `USER_IMPORT_MAX_ROWS` 可能放宽到 50000，那时 `new_records` 会触发截断
- 加字段零成本（默认 False），未来不用 schema migration

> **dry_run 也做权限校验**（防用户在前端预检阶段就看到越界提示，避免点了"确认导入"才发现一堆权限错误）：
> - 部门越界（#2.11）：每行 `dept_input` 反查 dept_id → 校验是否在 `accessible_dept_ids`
> - 角色越权（#2.15）：每行 `role_input` 反查 role_ids → 校验是否 ⊆ `operator_role_ids`
> - dry_run 阶段的越界行进 `out_of_scope_records`（截断展示）+ `out_of_scope_records_file`（全量下载），不阻断其他行的 new/exists 判定

### 3.3 `ImportResult`（正式导入结果，v2.2 P1：API 不返回 failed_rows 数组，只返回文件）

```python
class ImportResult(BaseModel):
    batch_id: str                            # 关联 sys_user_import_batch（v2.2 #2.27 幂等用）
    success_count: int
    skipped_count: int                       # on_conflict=skip 命中的
    overwritten_count: int                   # on_conflict=overwrite 命中的
    failed_count: int                        # 失败行数（仅计数，不返数组）
    failed_rows_file: str | None = None      # /file/import-error/{batch_id}.xlsx（下载链接）
    # v2.2 P1：可选「前 N 条失败摘要」让前端 toast 显示（不让前端解析 Excel）
    failed_rows_preview: list[FailedRow] = []    # 仅前 20 条（toast 用），全量在文件
    # v2.2 #2.27：幂等重放标记
    idempotent_replay: bool = False          # True 表示这是重放，非首次执行
    # 不含 initial_passwords：默认密码在 sys_config.auth:default_password，
    # 管理员线下告知用户（详见决策 #2.5 / #2.5.1）
```

**为什么不返回 `failed_rows: list[FailedRow]`**（v2.2 P1 修订）：
- 2000 行 Excel 全部失败 → 2000 行 FailedRow 序列化进 JSON response 几 MB
- 前端表格渲染 2000 行卡顿；用户实际修不动 2000 行错误
- 前端只需 toast 显示「失败 N 行，前 20 条：row 5 user_name 缺失 / row 8 邮箱格式错 / ...」+ 「下载完整清单」按钮
- 全量在 `failed_rows_file` Excel 文件，用户用 Excel 修改后可重新导入

**API 契约对照**：
- 旧（v2.1）：`{ failedRows: [...2000条...], failedCount: 2000 }` ← 几 MB JSON
- 新（v2.2 P1）：`{ failedCount: 2000, failedRowsPreview: [...20条...], failedRowsFile: "/file/..." }` ← 几 KB JSON + 文件下载

### 3.4 `FailedRow`（错误清单结构）

```python
class FailedRow(BaseModel):
    row_num: int
    field: str                               # 出错字段名
    value: str                               # 用户填的值
    reason: str                              # 中文原因
    error_code: str                          # i18n 映射码，如 "AI_IMPORT_USERNAME_INVALID"
```

### 3.5 `UserExportFilter`（导出筛选，POST body）

```python
class UserExportFilter(BaseModel):
    """POST /system/user/export 的 body（详见 #2.23，从 GET query 改 POST 防 access log 泄露）"""
    user_name: str | None = None
    dept_id: int | None = None
    status: str | None = None
    # ... 现有 list 端点的所有 filter 字段
```

### 3.6 `UserImportBatch`（**v2.2 P1-2 修订：唯一 aggregate root**，原 ImportPreviewSession 类已合并删除）

> **v2.2 P1-2 合并理由**：原 v2.1 同时存在 `ImportPreviewSession`（Pydantic，Redis 存）+ `UserImportBatch`（ORM，DB 存）两个概念，字段几乎完全重复（preview_token / operator_id / file_storage_key / file_sha256 / records_hash / summary / expires_at），实际使用时容易混淆「session vs batch 哪个是 SoT」。v2.2 P0 #2.19 已把 Redis 降为 cache only（业务数据全部落 DB），ImportPreviewSession 类失去存在意义。**v2.2 P1-2 删除 ImportPreviewSession 类，sys_user_import_batch 表是唯一 aggregate root**，preview_token / file_storage_key / file_sha256 / records_hash / summary 全部是 batch 表的字段。

```python
class UserImportBatch(Base):
    """一次导入的批次上下文 + 状态机（v2.2 P1-2：CREATED → PREVIEW_DONE → RUNNING → SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED）"""
    __tablename__ = "sys_user_import_batch"
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_sha256: Mapped[str] = mapped_column(String(64))
    total_rows: Mapped[int] = mapped_column(Integer)
    # dry_run 阶段写入（预检摘要）
    preview_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    summary_new: Mapped[int] = mapped_column(Integer, default=0)
    summary_exists: Mapped[int] = mapped_column(Integer, default=0)
    summary_conflict: Mapped[int] = mapped_column(Integer, default=0)
    summary_out_of_scope: Mapped[int] = mapped_column(Integer, default=0)
    # execute 阶段写入（实际结果）
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    overwritten_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_rows_file: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="失败行 Excel 路径 /file/import-error/{batch_id}.xlsx",
    )
    on_conflict: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(32), index=True,
        comment="状态机详见 §2.26：CREATED/RUNNING/SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

**索引建议**：
- `(operator_id, created_at)` — admin 查"我导过的批次"
- `(status, created_at)` — 状态机清理（CREATED + 10min → EXPIRED；任意状态 + 90 天 → 删除）
- `preview_token` UNIQUE — execute 时反查 batch 行
- `created_at` — 90 天归档

### 3.8 `OVERWRITE` 常量（详见 #2.21）

```python
# app/modules/system/service/user_service.py 顶部

OVERWRITE_NEVER = frozenset({
    "user_id",
    "user_name",
    "hashed_password",
    "create_time",
})

OVERWRITE_ALLOWED = frozenset({
    "employee_no",       # 详见 #2.24
    "nickname",
    "user_email",
    "user_phone",
    "dept_id",
    "role_ids",
    "user_gender",
    "status",
})
```

### 3.9 `FileStorage` Protocol（v2.2 P1-4：文件存储抽象，业务层不依赖 local_path）

> **v2.2 P1-4 修订理由**：原 v2.1/v2.2 决策中 #2.19 / #2.22 / #2.22.1 / #2.27 散落 `file_storage.save()` / `file_storage.read()` / `file_storage.delete()` 调用，但**未明确接口契约**。Phase 1 用本地文件系统（`/tmp/import/` + `/file/import-error/`）够用，但部署到 K8s / Docker Swarm 时本地文件系统不共享（多副本间 preview 文件丢失）；Phase 3 切对象存储（S3 / OSS / MinIO）时改业务代码代价大。**v2.2 P1-4 提前定义 `FileStorage` Protocol，业务层依赖 Protocol，部署时切换实现即可**。

**Protocol 定义**：

```python
# app/core/file_storage.py（新建模块，跨模块复用）
from typing import Protocol, runtime_checkable
from pathlib import Path

@runtime_checkable
class FileStorage(Protocol):
    """文件存储抽象（v2.2 P1-4）

    业务层只依赖 Protocol，不依赖具体实现。
    - Phase 1: LocalFileStorage（本地文件系统 /tmp/import/）
    - Phase 3+: S3FileStorage / MinIOFileStorage / GridFSFileStorage
    """

    async def save(
        self,
        data: bytes,
        *,
        mime_type: str,
        namespace: str,           # 例 "import-preview" / "import-error" / "import-template"
        suffix: str = "",         # 例 ".xlsx"，便于人眼查看
        ttl_seconds: int | None = None,   # None = 永久；正数 = 自动过期（由实现保证）
    ) -> str:
        """保存文件，返回 storage_key（不透明字符串，业务层不解析）

        storage_key 例：
        - LocalFileStorage: "import-preview/abc-uuid.xlsx"（相对路径）
        - S3FileStorage: "s3://hohu-import/import-preview/abc-uuid.xlsx"（URI）
        - 业务层不解析，只保存 + 凭 key 取文件
        """
        ...

    async def read(self, storage_key: str) -> bytes:
        """凭 storage_key 读文件 bytes，文件不存在抛 FileNotFoundError"""
        ...

    async def delete(self, storage_key: str) -> bool:
        """删除文件，返回 True（已删）/ False（文件本就不存在，幂等）"""
        ...

    async def exists(self, storage_key: str) -> bool:
        """检查文件是否存在"""
        ...

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None:
        """生成下载 URL（如有），临时签名 URL 优先；本地实现返回 None（业务层 fallback 用 /file/ 端点）"""
        ...
```

**默认实现：LocalFileStorage（Phase 1）**：

```python
# app/core/file_storage.py
class LocalFileStorage:
    """本地文件系统实现（Phase 1 默认）

    配置项（settings.LOCAL_FILE_STORAGE_ROOT）：
    - 单机部署："/tmp/hohu/"
    - Docker："/data/hohu/files/"（volume mount）
    - K8s：必须用 PVC 或换 S3FileStorage（多副本本地不共享）
    """

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, data, *, mime_type, namespace, suffix="", ttl_seconds=None):
        # namespace 子目录隔离 + 防穿越
        ns_dir = (self.root / namespace).resolve()
        if not ns_dir.is_relative_to(self.root):
            raise ValueError(f"非法 namespace: {namespace}")
        ns_dir.mkdir(parents=True, exist_ok=True)

        file_id = f"{uuid.uuid4().hex}{suffix}"
        file_path = ns_dir / file_id
        file_path.write_bytes(data)

        storage_key = f"{namespace}/{file_id}"   # 业务层保存这个相对路径
        # TTL 清理：cron 扫描 namespace 目录，删 mtime > ttl 的文件（#2.22.1）
        return storage_key

    async def read(self, storage_key: str) -> bytes:
        file_path = (self.root / storage_key).resolve()
        if not file_path.is_relative_to(self.root):
            raise FileNotFoundError(f"非法 storage_key: {storage_key}")
        return file_path.read_bytes()

    async def delete(self, storage_key: str) -> bool:
        file_path = (self.root / storage_key).resolve()
        if not file_path.exists():
            return False
        file_path.unlink()
        return True

    async def exists(self, storage_key: str) -> bool:
        return (self.root / storage_key).exists()

    def public_url(self, storage_key, *, expires_in=3600):
        return None   # 本地实现无 URL，业务层 fallback 到 /file/{storage_key} 端点
```

**Phase 3 切换 S3 示例**（不修改业务代码）：

```python
# app/core/file_storage.py
class S3FileStorage:
    """S3 / MinIO / OSS 实现（Phase 3+）"""
    def __init__(self, bucket: str, client):
        self.bucket = bucket
        self.s3 = client

    async def save(self, data, *, mime_type, namespace, suffix="", ttl_seconds=None):
        key = f"{namespace}/{uuid.uuid4().hex}{suffix}"
        await asyncio.to_thread(
            self.s3.put_object,
            Bucket=self.bucket, Key=key, Body=data, ContentType=mime_type,
            Expires=datetime.now() + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        )
        return key   # 同 LocalFileStorage 返回 "namespace/file_id" 格式

    async def read(self, storage_key):
        resp = await asyncio.to_thread(
            self.s3.get_object, Bucket=self.bucket, Key=storage_key
        )
        return resp["Body"].read()

    def public_url(self, storage_key, *, expires_in=3600):
        return self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": storage_key},
            ExpiresIn=expires_in,
        )
    # ...
```

**DI / 注入**：

```python
# app/db/session.py 或 app/main.py
from app.core.config import settings
from app.core.file_storage import LocalFileStorage, S3FileStorage, FileStorage

def get_file_storage() -> FileStorage:
    if settings.FILE_STORAGE_BACKEND == "local":
        return LocalFileStorage(Path(settings.LOCAL_FILE_STORAGE_ROOT))
    elif settings.FILE_STORAGE_BACKEND == "s3":
        return S3FileStorage(bucket=settings.S3_BUCKET, client=boto3.client("s3", ...))
    raise ValueError(f"未知 backend: {settings.FILE_STORAGE_BACKEND}")

# 业务层
class ImportService:
    def __init__(self, file_storage: FileStorage):
        self._fs = file_storage

    async def dry_run(self, db, records, current_user, file_bytes, filename):
        storage_key = await self._fs.save(
            file_bytes, mime_type="...", namespace="import-preview",
            suffix=".xlsx", ttl_seconds=3600,
        )
        # 业务层不关心是 local 还是 s3，只保存 storage_key
```

**反例**: (1) **散落 `local_path` / `f"/tmp/import/{batch_id}.xlsx"` 硬编码 → Phase 3 切 S3 时改 100+ 处调用**（v2.2 P1-4 提前抽象，业务层零改动）。(2) FileStorage 用 ABC 强约束 → Protocol（runtime_checkable）更灵活，mock 测试不需继承。(3) save 返回绝对路径 → 跨实现不兼容（local 是 /tmp/...，s3 是 s3://...）；返回相对 storage_key 实现无关。(4) 不暴露 public_url → Phase 3 S3 presigned URL 体验更好（直链下载不经服务端），但业务层不知道何时切；Protocol 加 public_url 方法返回 None 时业务层 fallback。(5) ttl_seconds 参数业务层到处算 → 由 FileStorage 实现内部记账（LocalFileStorage 用文件 mtime + cron 扫，S3 用 Expires 元数据），业务层只传意图。

**回归**: `app/core/file_storage.py` 模块（FileStorage Protocol + LocalFileStorage 默认实现）；`settings.FILE_STORAGE_BACKEND` 配置项（local / s3，默认 local）；`get_file_storage()` DI 工厂；ImportService / ExportService 通过 `__init__` 注入 FileStorage；业务层只调 `save / read / delete / exists / public_url`，**禁止 import LocalFileStorage / S3FileStorage 具体类**（lint 规则 `banned-imports`）；测试用 `MockFileStorage`（in-memory dict 实现）；Phase 3 切 S3 时只改 `get_file_storage` 工厂，业务代码零改动；测试 `test_file_storage_protocol_contract`（LocalFileStorage 通过 Protocol runtime check）+ `test_file_storage_path_traversal_blocked`（namespace 含 `../` 拒绝）+ `test_business_layer_independent_of_storage_impl`（mock LocalFileStorage / S3FileStorage 都能跑通 ImportService.dry_run）。

## 4. `user_service` 扩展（4 方法签名）

```python
# app/modules/system/service/user_service.py 追加

class UserService:
    # ... 现有 9 方法不动 ...

    # ========== Helper（公开，AI tool / HTTP 共用）==========

    async def resolve_dept(
        self, db: AsyncSession, dept_input: str
    ) -> int:
        """部门名 / 完整路径 → dept_id（详见 #2.17）。
        - '前端部' → 唯一性校验 → dept_id（重名抛 AI_IMPORT_DEPT_DUPLICATE）
        - '总公司/研发中心/前端部' → 逐级查找 → dept_id
        - 无匹配抛 AI_IMPORT_DEPT_NOT_FOUND / AI_IMPORT_DEPT_PATH_NOT_FOUND
        """

    async def resolve_role_input(
        self, db: AsyncSession, role_input_str: str
    ) -> list[int]:
        """逗号分隔 code/name 混合 → role_ids 去重（详见 #2.18）。
        - Pass 1: role_code 精确匹配
        - Pass 2: 剩余按 role_name 精确匹配
        - 未匹配抛 AI_IMPORT_ROLE_NOT_FOUND（含未匹配项列表）
        """

    async def check_permission_boundary(
        self,
        db: AsyncSession,
        records: list[UserImportRecord],
        current_user: User,
    ) -> list[FailedRow]:
        """对每行做角色越权校验（#2.15）。
        - operator_role_ids = {r.role_id for r in current_user.roles if enabled}
        - 超管 / R_SUPER → 直接返回 [] 跳过
        - 否则每行 resolve_role_input → 校验 ⊆ operator_role_ids
        - 越界行返回 FailedRow(field='role_input', error_code='AI_IMPORT_ROLE_OUT_OF_SCOPE')
        """

    async def check_dept_data_scope(
        self,
        db: AsyncSession,
        records: list[UserImportRecord],
        current_user: User,
    ) -> list[FailedRow]:
        """对每行做部门 data_scope 校验（#2.11）。
        - build_data_scope_context(db, current_user) → accessible_dept_ids
        - None = 全部可见，跳过
        - 否则每行 resolve_dept → 校验 ∈ accessible_dept_ids
        - 越界行返回 FailedRow(field='dept_input', error_code='AI_IMPORT_DEPT_OUT_OF_SCOPE')
        """

    # ========== 主流程方法 ==========

    async def parse_import_excel(
        self,
        db: AsyncSession,
        file_bytes: bytes,
        mime_type: str,
    ) -> list[UserImportRecord]:
        """解析 Excel/CSV → 验证 → 返回 records（不落库）
        
        - openpyxl 读 xlsx/xls；csv 用标准库
        - 字段校验：必填字段 / 邮箱格式 / 手机号格式（dept_input / role_input 不在这里校验存在性，留给 dry_run）
        - 失败行抛 ImportErrorCollection（含全部 FailedRow，不一次一个）
        """

    async def dry_run_import_users(
        self,
        db: AsyncSession,
        records: list[UserImportRecord],
        current_user: User,
        file_bytes: bytes,                    # 用于算 file_sha256
        filename: str,
        reason: str,                          # v2.2 P1-3 #2.30 必填
    ) -> tuple[ImportDryRunResult, UserImportBatch]:
        """预检 + 生成 preview_token（v2.2 P1-2：batch 表是唯一 aggregate root，无独立 session）。

        - 按 user_name 命中已存在记录
        - 字段冲突：dept_input 反查失败 / role_input 反查失败
        - 权限越界：check_permission_boundary + check_dept_data_scope
        - v2.2 P1-2：INSERT sys_user_import_batch (status=CREATED → PREVIEW_DONE) 含 file_sha256 / records_hash / summary / reason 全部业务字段
        - v2.2 P0 #2.19：Redis 仅 cache preview_token → batch_id（不存业务数据）
        - 返回 (dry_run_result, batch)，前端展示 summary + preview_token
        """

    async def batch_create_users_from_records(
        self,
        db: AsyncSession,
        records: list[UserImportRecord],
        *,
        preview_token: str,                   # 必填（#2.19，三重校验）
        file_bytes: bytes,                    # 重新 parse + 校验 file_sha256
        filename: str,
        on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
        current_user: User,
    ) -> ImportResult:
        """批量新建（preview_token 校验 + chunk + savepoint + 写 batch 表）。

        流程（#2.19-2.22 全套）：
        1. **preview_token 三重校验**：Redis get → 比对 file_sha256 + records_hash + operator_id；不一致拒绝
        2. **新建 batch 上下文**：INSERT sys_user_import_batch (status='RUNNING', ...)
        3. dry_run_import_users 二次跑（防 dry_run 后数据变化）→ 四象限分类
        4. conflict + out_of_scope 直接进 failed_rows（不参与落库）
        5. 对 new_records 按 on_conflict 处理 exists_records（skip / overwrite 用 OVERWRITE_ALLOWED 过滤 / fail_fast）
        6. **chunk 100 rows**（#2.20）：`for chunk in chunks(records, CHUNK_SIZE)`:
            - 外层 `async with db.begin()` chunk 级 transaction
            - 行级 `async with db.begin_nested()` savepoint
            - 失败 ROLLBACK savepoint，进 FailedRow，继续下一行
            - IntegrityError 区分 user_name UNIQUE → `AI_IMPORT_USERNAME_DUPLICATE`（#2.25）
        7. 每行 resolve_dept + resolve_role_input + ensure_targets_in_scope（二次防御）
        8. password: 读 sys_config.auth:default_password 哈希入库（#2.5）
        9. **写失败文件**：failed_rows → /file/import-error/{batch_id}.xlsx，路径存 batch.failed_rows_file
        10. **更新 batch 表**：UPDATE status='SUCCESS' / 'PARTIAL_SUCCESS' / 'FAILED' + counts + finished_at
        11. 返回 ImportResult + batch_id
        """

    async def export_users_to_excel(
        self,
        db: AsyncSession,
        filter: UserExportFilter,
        current_user: User,
    ) -> tuple[bytes, int]:
        """按 filter 导出 Excel bytes + 行数。
        
        - data_scope 自动应用：filter 加 user 的 accessible_dept_ids（HR 只能导他可见的）
        - 字段白名单 EXPORT_ALLOWED_FIELDS（hashed_password 等永不导出，#2.9）
        - 行数 > 5000 抛 BusinessRuleException("AI_EXPORT_ASYNC_REQUIRED")（#2.6）
        - 返回 (xlsx_bytes, row_count) 让 API 层包 StreamingResponse
        """
```

---

## 5. HTTP API 契约（Phase 1）

### 5.1 `POST /system/user/import`（multipart）

```
POST /system/user/import
Content-Type: multipart/form-data
Authorization: Bearer <jwt>

Body:
  file: <excel file bytes>     # 必填，≤ 10MB
  on_conflict: "skip"          # 可选，默认 skip
  sync_mode: "CREATE_ONLY"     # 可选，默认 CREATE_ONLY（v2.2 P1 #2.24）
  reason: "2026年8月HR入职同步" # 必填，1-256 字符（v2.2 P1-3 #2.30）
  dry_run: "true"              # 可选，预检模式（生成 preview_token）
  preview_token: "xxx"         # dry_run=false 时必填（详见 #2.19）

权限：system:user:import
```

**响应（dry_run=true）**：
```json
{
  "code": 200, "msg": "success",
  "data": {
    "total": 245,
    "newCount": 200,
    "existsCount": 40,
    "conflictCount": 5,
    "outOfScopeCount": 3,
    "conflictExamples": [...],
    "outOfScopeExamples": [...]
  },
  "previewToken": "xxx",
  "expiresAt": "2026-08-01T14:10:00"
}
```

> dry_run 阶段就跑权限校验（#2.11 / #2.15），让用户在确认前看到所有越界行；同时生成 `preview_token`（Redis 10min TTL，详见 #2.19），正式导入时必填。

**响应（正式导入，v2.2 P1：不返回 failed_rows 数组，只返回文件 + 摘要）**：
```json
{
  "code": 200, "msg": "success",
  "data": {
    "batchId": "uuid-xxx",
    "status": "PARTIAL_SUCCESS",
    "successCount": 200,
    "skippedCount": 40,
    "overwrittenCount": 0,
    "failedCount": 2000,
    "failedRowsPreview": [
      {"rowNum": 5, "field": "user_name", "value": "", "reason": "必填缺失", "errorCode": "AI_IMPORT_USERNAME_INVALID"},
      {"rowNum": 8, "field": "user_email", "value": "abc", "reason": "邮箱格式错", "errorCode": "AI_IMPORT_EMAIL_INVALID"}
    ],
    "failedRowsFile": "/file/import-error/uuid-xxx.xlsx",
    "idempotentReplay": false
  }
}
```

> v2.2 P1：API 响应不含 `failed_rows` 数组（防 2000 行错误撑大 response 几 MB），只返回 `failedCount` + 前 20 条 `failedRowsPreview`（toast 用）+ `failedRowsFile`（全量下载链接）。
> v2.2 #2.27：`idempotentReplay=true` 表示这是重放（用户双击），返回原批次结果，不重复落库。
> 默认密码不返回（决策 #2.5）：管理员从 `sys_config.auth:default_password` 拿默认值，线下告知用户「请用 XXX 登录」。
> `preview_token` 缺失 / 过期 / 三重校验失败 → 422 + `AI_IMPORT_PREVIEW_INVALID`。

### 5.2 `POST /system/user/export`（从 GET 改 POST，详见 #2.23）

```
POST /system/user/export
Content-Type: application/json
Authorization: Bearer <jwt>

Body:
{
  "userName": "xxx",           # 可选 filter
  "deptId": 1,                 # 可选 filter
  "status": "1"                # 可选 filter
}

权限：system:user:export
```

**响应**：
- 同步（行数 ≤ 5000）：`StreamingResponse` xlsx，`Content-Disposition: attachment; filename=users_20260801.xlsx`
- 超阈值（> 5000）：HTTP 422 + `errorCode: AI_EXPORT_ASYNC_REQUIRED`，message 提示"行数过多，请等待异步通道开放"（Phase 3 实现后改为自动异步）

> 前端用 `fetch(POST)` + `Blob` + `URL.createObjectURL` + 隐藏 `<a>` 触发下载（不能用 `<a href>` 直接触发）。

### 5.3 `GET /system/user/import/template`

```
GET /system/user/import/template
Authorization: Bearer <jwt>
权限：system:user:import（同 import，避免未授权下载模板探查字段）
```

**响应**：xlsx 文件，含 **4 sheet**（详见 #2.13 + #2.16 + #2.17 / #2.18 字典 sheet）：

| Sheet | 内容 | 来源 |
|---|---|---|
| **数据**（用户填） | 列顺序固定：`user_name` / `nickname` / `user_email` / `user_phone` / `dept_input` / `role_input` / `user_gender` / `status` + 2 行示例 | 固定 |
| **说明** | 每列字段说明 / 必填标记 / 取值范围 / 冲突处理策略 | 固定 |
| **部门字典**（参考） | `dept_name` + `full_path`（"总公司/研发中心/前端部"）+ `dept_id` + `status` | **系统生成**（每次下载时按当前 `sys_dept` 表实时查询填充） |
| **角色字典**（参考） | `role_code` + `role_name` + `status` | **系统生成**（实时查询 `sys_role`） |

**Data Validation 下拉**（详见 #2.16）：
- 「数据」sheet 的 `dept_input` 列（E 列）：`DataValidation(formula1="=部门字典!$A$2:$A$1000")`，引用字典 sheet 的 `full_path` 列
- 「数据」sheet 的 `role_input` 列（F 列）：`DataValidation(formula1="=角色字典!$A$2:$A$50")`，引用字典 sheet 的 `role_code` 列，`allow_blank=True`
- 复制粘贴不强制触发，但手动输入会校验

**生成时间标注**：「部门字典」/「角色字典」sheet 顶部加一行「⏰ 生成时间：2026-08-01 14:30（数据可能已变化，请重新下载模板获取最新）」，提示用户旧模板可能过期。

### 5.4 `GET /system/user/import/{batch_id}`（v2.2 P2：批次状态查询，为 Phase 3 异步预留）

```
GET /system/user/import/{batch_id}
Authorization: Bearer <jwt>
权限：system:user:list（list 权限即可，因为查的是导入历史不是用户敏感数据）
```

**响应**：
```json
{
  "code": 200, "msg": "success",
  "data": {
    "batchId": "uuid-xxx",
    "status": "PARTIAL_SUCCESS",
    "filename": "users_20260801.xlsx",
    "operatorId": "12345",
    "operatorName": "admin",
    "totalRows": 2000,
    "summaryNew": 1500,
    "summaryExists": 400,
    "summaryConflict": 50,
    "summaryOutOfScope": 50,
    "successCount": 1500,
    "skippedCount": 400,
    "overwrittenCount": 0,
    "failedCount": 100,
    "failedRowsFile": "/file/import-error/uuid-xxx.xlsx",
    "onConflict": "skip",
    "syncMode": "CREATE_ONLY",
    "createdAt": "2026-08-01T14:00:00",
    "startedAt": "2026-08-01T14:01:00",
    "finishedAt": "2026-08-01T14:01:30",
    "expiresAt": "2026-08-01T14:10:00"
  }
}
```

**用途**：
- 前端导入历史页面（管理员查看自己 / 团队的导入批次）
- Phase 3 异步通道上线后，前端轮询此接口拿 RUNNING 进度
- 审计反查（batch_id 来自 sys_operation_log → 反查具体批次详情）

**列表查询**（v2.2 P2 扩展）：

```
GET /system/user/import?current=1&size=20&status=PARTIAL_SUCCESS
权限：system:user:list
返回：分页 batch 摘要列表
```

### 5.5 `GET /system/user/import/{batch_id}/logs`（v2.2 P2：批次操作日志）

```
GET /system/user/import/{batch_id}/logs
权限：system:user:list
返回：[{event, fromStatus, toStatus, detail, createdAt}, ...]
```

详见 #2.28 业务日志表。

### 5.6 `POST /system/user/import/{batch_id}/cancel`（v2.2 P2：取消导入，详见 #2.29）

```
POST /system/user/import/{batch_id}/cancel
Body: {
  "reason": "用户主动取消"      # 必填（v2.2 P1-3 #2.30），1-256 字符
}
权限：system:user:import（必须是该 batch 的 operator 或超管）
返回：{"batchId": "...", "status": "CANCELLED", "cancelledAt": "..."}
```

### 5.7 错误码

| HTTP | errorCode | 触发条件 |
|---|---|---|
| 400 | `AI_IMPORT_FILE_TOO_LARGE` | 文件 > 10MB |
| 400 | `AI_IMPORT_INVALID_MIME` | MIME 不在白名单 |
| 400 | `AI_IMPORT_TOO_MANY_ROWS` | 行数 > 2000（v2.2 P0：50000 → 2000） |
| 422 | `AI_EXPORT_ASYNC_REQUIRED` | 导出行数 > 5000 |
| 400 | `AI_IMPORT_USERNAME_INVALID` | user_name 必填缺失 / 格式错 |
| 400 | `AI_IMPORT_EMAIL_INVALID` | user_email 格式错 |
| 400 | `AI_IMPORT_PHONE_INVALID` | user_phone 格式错 |
| 400 | `AI_IMPORT_DEPT_NOT_FOUND` | dept_input 无匹配（#2.17） |
| 400 | `AI_IMPORT_DEPT_PATH_NOT_FOUND` | 路径某段不存在（#2.17） |
| 400 | `AI_IMPORT_DEPT_DUPLICATE` | dept_name 重名需用路径（#2.17） |
| 400 | `AI_IMPORT_ROLE_NOT_FOUND` | role code/name 都未匹配（#2.18） |
| **403** | **`AI_IMPORT_DEPT_OUT_OF_SCOPE`** | **dept 不在操作人 data_scope 内（#2.11）** |
| **403** | **`AI_IMPORT_ROLE_OUT_OF_SCOPE`** | **操作人无权分配的角色（#2.15）** |
| 422 | `AI_IMPORT_PREVIEW_INVALID` | preview_token 缺失 / 过期 / 三重校验失败（#2.19） |
| 422 | **`AI_IMPORT_ALREADY_EXECUTED`** | **批次已 SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED，重复 execute 被拒（v2.2 #2.27）** |
| 422 | **`AI_IMPORT_BATCH_RUNNING`** | **批次 RUNNING 中，并发 execute 被拒（v2.2 #2.27）** |
| 422 | **`AI_IMPORT_ILLEGAL_TRANSITION`** | **非法状态转换（v2.2 #2.26，例如 SUCCESS → RUNNING）** |
| 422 | **`AI_IMPORT_EMPLOYEE_NO_EXISTS`** | **sync_mode=CREATE_ONLY 时 employee_no 已存在（v2.2 #2.24）** |
| 422 | **`AI_IMPORT_BATCH_NOT_FOUND`** | batch_id 不存在（v2.2 P2 §5.4/5.6） |
| 422 | **`AI_IMPORT_BATCH_NOT_CANCELLABLE`** | status 不是 CREATED/RUNNING，不能取消（v2.2 #2.29） |
| 400 | `AI_IMPORT_USERNAME_DUPLICATE` | 并发导入同 user_name，UNIQUE 约束兜底（#2.25） |
| 400 | `AI_IMPORT_EMPLOYEE_NO_DUPLICATE` | employee_no 重复（UNIQUE 约束，#2.24） |
| 400 | `AI_IMPORT_EMPTY` | Excel 解析后 0 行 |

> `AI_IMPORT_DEPT_OUT_OF_SCOPE` / `AI_IMPORT_ROLE_OUT_OF_SCOPE` 不在 controller 层抛 403 中断整批，而是在 service 层把越界行收集到 `failed_rows`（与 #2.7 半成功策略一致），HTTP 响应仍是 200 + `data.failedRows` 含详细错误。这样允许其他合法行正常落库。

---

## 6. 前端设计（Phase 1）

### 6.1 列表页工具栏新增按钮

`src/views/system/user/index.vue` 的 `TableHeaderOperation` 加：

```vue
<NButton v-permission="'system:user:import'" @click="openImportModal">
  {{ $t('page.user.import') }}
</NButton>
<NButton v-permission="'system:user:export'" @click="handleExport">
  {{ $t('page.user.export') }}
</NButton>
<NButton quaternary @click="downloadTemplate">
  {{ $t('page.user.downloadTemplate') }}
</NButton>
```

### 6.2 导入弹窗（v2.2 P2：3 步流程 + Preview UI 增强）

新建 `src/views/system/user/modules/user-import-modal.vue`：

**Step 1 - 上传**：
- `NUpload` 拖拽区 + 「下载模板」链接
- 选完文件自动上传调 `dry_run_import`（`POST /import?dry_run=true`）
- 显示解析中状态

**Step 2 - 预检（v2.2 P2 UI 增强）**：

```
┌──────────────────────────────────────────────────────────┐
│ 📄 users_20260801.xlsx                            ✕ 取消 │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  📊 预检结果（共 2000 行）                                │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │   ✅ 新增    │  │  ⏭ 已存在   │  │  ⚠️ 冲突     │   │
│  │    1500      │  │    400       │  │    50        │   │
│  └──────────────┘  └──────────────┘  └──────────────┘   │
│  ┌──────────────┐                                        │
│  │ 🚫 越界      │  ← 角色越权 / 部门 data_scope 越界    │
│  │     50       │                                        │
│  └──────────────┘                                        │
│                                                          │
│  ⚙️ 冲突处理                                              │
│  ◉ 跳过已存在（推荐）                                    │
│  ○ 覆盖已存在（仅更新白名单字段）                        │
│  ○ 首个冲突即终止                                        │
│                                                          │
│  ⚙️ 工号同步模式（v2.2 P1）                              │
│  ◉ 仅新增（CREATE_ONLY，默认安全）                       │
│  ○ 更新资料（UPDATE_PROFILE）                            │
│  ○ 完整同步（FULL_SYNC，含 user_name）                   │
│                                                          │
│  ⚠️ 错误清单（前 20 条，共 100 条 → 下载完整清单）       │
│  ┌────────┬─────────────┬─────────────────────────┐     │
│  │ Row    │ Field       │ Reason                  │     │
│  ├────────┼─────────────┼─────────────────────────┤     │
│  │ 5      │ user_name   │ 必填缺失                │     │
│  │ 8      │ user_email  │ 邮箱格式错              │     │
│  │ 12     │ role_input  │ 角色不存在: R_FOO       │     │
│  │ 15     │ dept_input  │ 部门重名，请用完整路径  │     │
│  │ ...    │ ...         │ ...                     │     │
│  └────────┴─────────────┴─────────────────────────┘     │
│                                                          │
│  ⚠️ 越界清单（前 10 条，共 50 条 → 下载完整清单）        │
│  ┌────────┬─────────────┬─────────────────────────┐     │
│  │ Row    │ Field       │ Reason                  │     │
│  ├────────┼─────────────┼─────────────────────────┤     │
│  │ 23     │ role_input  │ 无权分配角色 [R_SUPER]  │     │
│  │ 45     │ dept_input  │ 部门不在 data_scope 内  │     │
│  └────────┴─────────────┴─────────────────────────┘     │
│                                                          │
│            [取消]  [确认导入（preview_token=xxx）]       │
└──────────────────────────────────────────────────────────┘
```

- 文件名展示 + 取消按钮（v2.2 P2：用户改主意可立即取消，调 `POST /import/{batch_id}/cancel`）
- 4 卡片统计：新增 / 已存在 / 冲突 / 越界（v2.2 P1：conflict + out_of_scope 各自卡片）
- 错误清单表格（前 20 条，超出显示「⚠️ 仅展示前 20 条，完整清单下载」+ 下载按钮，v2.2 P1 #3.2 records 限流）
- on_conflict radio（默认 skip）+ sync_mode radio（默认 CREATE_ONLY，v2.2 P1 #2.24）
- 确认按钮携带 `preview_token`（不在 UI 显示完整 token，只显示前 8 位作标识）

**Step 3 - 确认（v2.2 P1 #3.3：API 响应精简）**：
- 点「确认导入」→ 调正式 `POST /import`（带 `preview_token + on_conflict + sync_mode`）
- 成功后展示 `successCount` / `skippedCount` / `overwrittenCount` / `failedCount`
- 失败行只展示 `failedRowsPreview`（前 20 条，v2.2 P1）
- 「下载完整错误清单」按钮（`failedRowsFile`，全量下载）
- `idempotentReplay=true` 时 toast 蓝色提示「⚠️ 检测到重复提交，已返回首次执行结果」（v2.2 #2.27）
- 提示文案：「新用户默认密码为 `<sys_config 中的值>`，请告知用户登录后尽快自行修改」（决策 #2.5.1：V1 不强制改密）

### 6.3 导出

直接 `window.open('/system/user/export?...')` 触发浏览器下载（GET 同步）；超阈值时 toast 提示。

### 6.4 i18n

**提取到 `common.*` 命名空间**（CLAUDE.md 约定「Common operations: reuse `common.*`」），让后续 role / dept / job / 商品管理等模块复用同款导入导出 UI 时零拷贝：

```ts
// src/locales/langs/zh-cn.ts & en-us.ts → common 命名空间追加

common: {
  // ... 现有 add/edit/delete/confirm/cancel 等 ...

  import: '导入',
  export: '导出',
  downloadTemplate: '下载模板',

  importModal: {
    step1Title: '上传文件',
    step2Title: '预检',
    step3Title: '确认',
    onConflictLabel: '冲突处理',
    onConflictSkip: '跳过已存在（推荐）',
    onConflictOverwrite: '覆盖已存在',
    onConflictFailFast: '首个冲突即终止',
    previewTitle: '预检结果',
    previewNew: '将新增 {count} 行',
    previewExists: '已存在 {count} 行',
    previewConflict: '冲突 {count} 行',
    confirmImport: '确认导入',
    downloadFailedRows: '下载错误清单',
    successResult: '成功 {success} / 跳过 {skipped} / 覆盖 {overwritten} / 失败 {failed}',
  }
}
```

**模块私有文案**（如「请告知用户用 XXX 登录」）走 `page.user.*`：
```ts
page: {
  user: {
    defaultPasswordHint: '新用户默认密码为「{password}」，请告知用户登录后尽快自行修改',
  }
}
```

**错误码 i18n**（CLAUDE.md 约定走 `errorCode.*` 命名空间）：
```ts
errorCode: {
  AI_IMPORT_FILE_TOO_LARGE: '文件超过 10MB',
  AI_IMPORT_INVALID_MIME: '不支持的文件格式（仅 xlsx / xls / csv）',
  AI_IMPORT_TOO_MANY_ROWS: '行数超过 50000，请分批导入',
  AI_IMPORT_USERNAME_INVALID: '用户名格式错误',
  AI_IMPORT_EMAIL_INVALID: '邮箱格式错误',
  AI_IMPORT_PHONE_INVALID: '手机号格式错误',
  AI_IMPORT_DEPT_NOT_FOUND: '部门不存在',
  AI_IMPORT_EMPTY: 'Excel 解析后无有效行',
  AI_EXPORT_ASYNC_REQUIRED: '导出行数过多，请分批或等待异步通道开放',
}
```

**模块私有 vs 公共的划分原则**：
- ✅ 公共（`common.*`）：导入导出按钮文字、importModal 流程、onConflict 选项、预检结果统计文案 —— 任何模块做导入导出都用同一套
- ❌ 模块私有（`page.<module>.*`）：仅该模块出现的文案（如「默认密码提示」是 user 模块独有，role 模块无密码概念）
- ❌ 错误码（`errorCode.*`）：跟错误码一一映射，独立命名空间

**后续 CLI generator 落地后**（参考 §11），`common.importModal.*` 这块直接被模板复用，新模块生成时自带导入弹窗的 i18n 引用，不需要每个模块各写一份。

---

## 7. AI tool 设计（Phase 2）

### 7.1 `user.import_preview` + `user.import_execute`（v2.2 P0：原 `user.batch_create` 拆为两个 tool）

详见决策 #2.14。两个 tool 的契约：

```python
# Tool 1: 只读，跑 dry_run + 生成 batch
@ai_tool(AiToolMeta(
    name="user.import_preview",
    agent="user_mgmt",
    summary=(
        "Parse Excel + dry-run user import → returns {batch_id, preview_token, summary}. "
        "Read-only, does NOT write users. "
        "**Workflow**: Call this first, show summary to user, then call user.import_execute after user confirms."
    ),
    required_perms=("system:user:add",),
    risk="low",                          # 只读
    readonly=True,
    dry_run_supported=False,             # 本身就是预检，不需要 dry_run 模式
    accepts_file=("text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    produces_file=False,
    result_view="detail_card",           # 展示 batch_id + preview_token + summary
    chip_target="/system/user",          # 跳转用户列表（readonly tool 标准 chip）
))
async def user_import_preview(
    ctx: AiToolContext,
    *,
    file_path: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
) -> ToolResult:
    """file_path 来自 /file/upload。

    流程：
    1. file_path → file_bytes（Gateway 内部解析）
    2. user_service.parse_import_excel(file_bytes) → records
    3. user_service.dry_run_import_users(records, current_user, file_bytes, filename)
       → INSERT sys_user_import_batch (status=CREATED) + 写 Redis cache
    4. ToolResult.success(
         data={batch_id, preview_token, summary:{new,exists,conflict,out_of_scope}},
         ui=UIResult(view_type="detail_card", view_data={...}, audit=..., label_key="user.importPreview"),
       )
    """

# Tool 2: 写入，强制 HITL
@ai_tool(AiToolMeta(
    name="user.import_execute",
    agent="user_mgmt",
    summary=(
        "Execute previously previewed user import batch. REQUIRES HITL confirmation. "
        "Pass preview_token from user.import_preview. "
        "**Idempotent**: re-executing a SUCCESS batch returns original result."
    ),
    required_perms=("system:user:add",),
    risk="high",                         # 写入，批量创建用户
    hitl_always=True,                    # 强制 HITL（批量写入永远人在回路）
    dry_run_supported=False,             # execute 不是 dry_run
    accepts_file=(),                     # 不接文件，凭 preview_token
    produces_file=True,                  # 输出 failed_rows.xlsx
    result_view="rows_affected",
))
async def user_import_execute(
    ctx: AiToolContext,
    *,
    preview_token: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
) -> ToolResult:
    """凭 preview_token 反查 batch（v2.2 #2.19 Redis cache → DB SoT 回退）→
    三重校验（file_sha256 + records_hash + operator_id）→
    CAS CREATED→RUNNING（v2.2 #2.27 幂等）→ chunk + savepoint 落库。

    幂等返回：
    - SUCCESS/PARTIAL_SUCCESS 重放 → 200 + idempotent_replay=true + 原结果
    - RUNNING → 422 + AI_IMPORT_BATCH_RUNNING
    - FAILED/EXPIRED/CANCELLED → 422 + AI_IMPORT_ALREADY_EXECUTED
    """
```

`_dry_run_user_import_execute` HITL 抽屉展示：preview summary + on_conflict 选项 + 「确认执行」按钮（不允许跳过）。

**LLM Prompt 引导**（system prompt 中追加，防 AI 跳过 preview）：

```
用户批量导入 Excel 流程：
1. 调 user.import_preview(file_path) 拿 batch_id + preview_token + summary
2. 把 summary（新增 X / 已存在 Y / 冲突 Z）展示给用户
3. 用户确认后调 user.import_execute(preview_token)
禁止跳过步骤 1 直接调 execute（会因 preview_token 不存在而失败）。
```

### 7.2 `user.export`

```python
@ai_tool(AiToolMeta(
    name="user.export",
    agent="user_mgmt",
    summary=(
        "Export users to Excel file → {'download_url': '/file/xxx'}. "
        "For 'export current filter' / 'download all users'."
    ),
    required_perms=("system:user:export",),
    risk="low",                          # 只读，不写库
    readonly=True,
    produces_file=True,
    result_view="detail_card",           # 展示下载链接 + 行数 + 字段
))
async def user_export(
    ctx: AiToolContext,
    *,
    user_name: str | None = None,
    dept_id: int | None = None,
    status: str | None = None,
) -> ToolResult:
    """调 user_service.export_users_to_excel → 写入 /file/ 目录 → 返回 download_url。
    
    超阈值（> 5000）抛 BusinessRuleException("AI_EXPORT_ASYNC_REQUIRED")，
    LLM 应告知用户「行数过多，请到管理后台分批导出或等待异步通道」。
    """
```

---

## 8. 测试矩阵

### 8.1 后端单测（`tests/modules/system/test_user_import_export.py`）

#### 解析层
| 测试 | 验证点 |
|---|---|
| `test_parse_excel_basic` | 标准 xlsx → records 数正确 |
| `test_parse_csv_basic` | CSV 同样解析 |
| `test_parse_invalid_mime` | 不在白名单 → AI_IMPORT_INVALID_MIME |
| `test_parse_too_large` | > 10MB → AI_IMPORT_FILE_TOO_LARGE |
| `test_parse_too_many_rows` | > 50000 → AI_IMPORT_TOO_MANY_ROWS |
| `test_parse_missing_required` | 缺 user_name → FailedRow 收集 |
| `test_parse_user_email_format` | 邮箱格式错 → AI_IMPORT_EMAIL_INVALID |

#### dept_input / role_input 反查（#2.17 / #2.18）
| 测试 | 验证点 |
|---|---|
| `test_resolve_dept_name_unique` | "前端部" 唯一命中 → dept_id |
| `test_resolve_dept_name_duplicate` | 重名 → AI_IMPORT_DEPT_DUPLICATE |
| `test_resolve_dept_path_mode` | "总公司/研发中心/前端部" 逐级查 → dept_id |
| `test_resolve_dept_not_found` | 无匹配 → AI_IMPORT_DEPT_NOT_FOUND |
| `test_resolve_dept_path_segment_missing` | 路径某段不存在 → AI_IMPORT_DEPT_PATH_NOT_FOUND |
| `test_resolve_role_input_by_code` | "R_DEV" → role_id |
| `test_resolve_role_input_by_name` | "开发者" → role_id |
| `test_resolve_role_input_mixed` | "R_DEV,开发者,R_QA" 混合 → 3 个 role_id 去重 |
| `test_resolve_role_input_not_found` | "不存在角色" → AI_IMPORT_ROLE_NOT_FOUND |

#### dry_run + on_conflict
| 测试 | 验证点 |
|---|---|
| `test_dry_run_new` | 全新 records → new_count = N |
| `test_dry_run_exists` | user_name 已存在 → exists_count = N |
| `test_dry_run_conflict` | 字段冲突 → conflict_count = N |
| `test_dry_run_out_of_scope` | 含越权行 → out_of_scope_count = N（dry_run 阶段就识别） |
| `test_batch_create_skip` | on_conflict=skip → 已存在跳过 |
| `test_batch_create_overwrite` | on_conflict=overwrite → 已存在更新 |
| `test_batch_create_fail_fast` | on_conflict=fail_fast → 第一个冲突终止 |
| `test_batch_create_half_success` | 半成功：失败行不阻断成功行 |
| `test_batch_create_password_uses_default` | 入库密码 = sys_config.auth:default_password 哈希 |

#### 🔴 安全与权限（#2.11 + #2.15）
| 测试 | 验证点 |
|---|---|
| `test_permission_boundary_role_out_of_scope` | HR 给用户分配 R_SUPER → 该行 FailedRow + AI_IMPORT_ROLE_OUT_OF_SCOPE |
| `test_permission_boundary_super_admin_bypass` | 超管导入可分配任意角色（豁免） |
| `test_permission_boundary_error_lists_role_names` | 错误提示含角色名（非 ID）：`无权分配角色 [系统超级管理员]` |
| `test_dept_data_scope_self_only` | DATA_SCOPE_SELF 操作人 → 只能导入自己部门用户 |
| `test_dept_data_scope_dept_and_sub` | DATA_SCOPE_DEPT_AND_SUB → 子部门用户可导入 |
| `test_dept_data_scope_violation` | 部门管理员导入其他部门 → AI_IMPORT_DEPT_OUT_OF_SCOPE |
| `test_dry_run_detects_out_of_scope` | dry_run 阶段就识别越权（不让用户点确认后才发现） |
| `test_out_of_scope_does_not_block_other_rows` | 越权行进 failed_rows，其他合法行正常落库 |

#### 导出
| 测试 | 验证点 |
|---|---|
| `test_export_basic` | filter → xlsx bytes + 行数 |
| `test_export_data_scope_applied` | HR 只能导出可见部门 |
| `test_export_field_whitelist` | hashed_password 不在导出列 |
| `test_export_async_threshold` | > 5000 行 → AI_EXPORT_ASYNC_REQUIRED |

#### 模板（#2.13 + #2.16 + #2.17/#2.18 字典）
| 测试 | 验证点 |
|---|---|
| `test_template_has_4_sheets` | 数据 / 说明 / 部门字典 / 角色字典 |
| `test_template_dept_dictionary_realtime` | 字典 sheet 含当前 DB 中所有部门（不是硬编码） |
| `test_template_role_dictionary_realtime` | 字典 sheet 含当前所有角色 |
| `test_template_data_validation_dept_column` | 「数据」sheet E 列含 DataValidation 引用部门字典 |
| `test_template_data_validation_role_column` | 「数据」sheet F 列含 DataValidation 引用角色字典 |
| `test_template_marks_generation_time` | 字典 sheet 顶部含「生成时间」标注 |

#### Preview Token（#2.19）
| 测试 | 验证点 |
|---|---|
| `test_preview_token_created_on_dry_run` | dry_run 返回 preview_token + 10min 后 Redis 自动过期 |
| `test_preview_token_expired_rejected` | execute 提交过期 token → AI_IMPORT_PREVIEW_INVALID |
| `test_preview_file_hash_mismatch_rejected` | dry_run 后改 Excel 重传 → file_sha256 不一致 → 拒绝 |
| `test_preview_records_hash_mismatch_rejected` | 同文件改字段值 → records_hash 不一致 → 拒绝 |
| `test_preview_operator_changed_rejected` | A dry_run 后 B 提交 → operator_id 不一致 → 拒绝 |
| `test_preview_not_store_full_records` | Redis value 不含 records（只 hash + summary），防大 Excel 撑爆 |

#### Row Transaction（#2.20）
| 测试 | 验证点 |
|---|---|
| `test_row_transaction_role_failed_rolls_back_user` | user 创建成功后 role 绑定失败 → savepoint ROLLBACK → user 也回滚 |
| `test_row_transaction_no_orphan_user` | 1000 行中第 500 行 role 失败 → 其他 999 行不受影响，DB 无残留 user |
| `test_chunk_commit_size_100` | 250 行导入 → 3 次 chunk commit（100+100+50） |
| `test_chunk_transaction_survives_row_failure` | chunk 内某行失败 → chunk 仍 commit 其他行 |

#### Overwrite Field Whitelist（#2.21）
| 测试 | 验证点 |
|---|---|
| `test_overwrite_password_not_changed` | overwrite 时不传 hashed_password 但传 nickname → DB 中 hashed_password 未变 |
| `test_overwrite_user_name_ignored` | overwrite 时 user_name 字段被忽略（不动） |
| `test_overwrite_allowed_fields_updated` | overwrite 时 nickname/user_email/dept_id/role_ids 正常更新 |
| `test_overwrite_employee_no_updated` | employee_no 在 OVERWRITE_ALLOWED 中（#2.24） |

#### Concurrency（#2.25）
| 测试 | 验证点 |
|---|---|
| `test_concurrent_same_user_name_import` | 两个并发 batch_create 同 user_name → 一个成功，另一个进 AI_IMPORT_USERNAME_DUPLICATE |
| `test_concurrent_does_not_corrupt_other_rows` | 并发冲突仅影响冲突行，其他行正常 |
| `test_employee_no_unique_constraint` | 同 employee_no 两次导入 → 第二次 AI_IMPORT_EMPLOYEE_NO_DUPLICATE |

#### Batch Context（#2.22）
| 测试 | 验证点 |
|---|---|
| `test_batch_record_created_on_dry_run` | dry_run 阶段 sys_user_import_batch 表 INSERT status=CREATED（v2.1 P0-2） |
| `test_batch_status_partial_success` | 部分成功 → UPDATE status=PARTIAL_SUCCESS + counts 正确 |
| `test_batch_failed_rows_file_persisted` | 失败行写 /file/import-error/{batch_id}.xlsx，路径存 batch.failed_rows_file |
| `test_batch_queryable_by_operator` | admin 查"我导过的批次" 按 operator_id 索引命中 |

#### Lifecycle State Machine（§2.26，v2.1 P2）
| 测试 | 验证点 |
|---|---|
| `test_state_created_to_running` | CREATED → execute → RUNNING（preview_token 三重校验通过） |
| `test_state_created_to_expired` | CREATED + 10min → EXPIRED（cron 扫描更新） |
| `test_state_running_to_success` | RUNNING → SUCCESS（success_count = total_rows） |
| `test_state_running_to_partial_on_fatal_error` | RUNNING → PARTIAL_SUCCESS（致命错误中断，已 commit 的 chunk 保留） |
| `test_state_transition_cas_prevents_race` | 并发 UPDATE status 用 CAS（防双 worker 同时改） |

#### Recoverable Errors（#2.20，v2.1 P1-1）
| 测试 | 验证点 |
|---|---|
| `test_recoverable_error_continues` | ROLE_NOT_FOUND 进 failed_rows，后续行继续 |
| `test_unrecoverable_db_error_aborts_batch` | 模拟 OperationalError → chunk rollback + batch.status=PARTIAL_SUCCESS + finished_at |
| `test_recoverable_codes_whitelist_complete` | RECOVERABLE_ERROR_CODES 集合覆盖所有业务校验错误码 |

#### Cleanup Crons（#2.22.1，v2.1 P1-2）
| 测试 | 验证点 |
|---|---|
| `test_cleanup_expired_batches_deletes_db_and_file` | 90 天前 batch 删 DB 行 + 同时删 failed_rows_file |
| `test_cleanup_expired_previews_marks_expired` | CREATED + 10min 的 batch → EXPIRED + 删孤儿 preview 文件 |
| `test_cleanup_preserves_recent_records` | 89 天前的 batch 不被清理（边界条件） |

#### employee_no Normalization（#2.24，v2.1 P1-3）
| 测试 | 验证点 |
|---|---|
| `test_employee_no_blank_string_normalized_to_null` | Excel 传 `""` / `"   "` → 入库为 NULL |
| `test_employee_no_multiple_nulls_allowed` | 多个用户 employee_no=NULL 不冲突（SQL 标准） |
| `test_employee_no_non_blank_unique` | 两个用户 employee_no="E001" → 第二个 AI_IMPORT_EMPLOYEE_NO_DUPLICATE |
| `test_existing_blank_strings_migrated_to_null` | alembic migration 把历史 `''` 改为 NULL 后 UNIQUE 约束能加成功 |

#### file_storage_key（#2.19，v2.1 P0-1）
| 测试 | 验证点 |
|---|---|
| `test_preview_token_carries_file_storage_key` | dry_run 返回的 preview_token 绑定 file_storage_key（execute 不需要前端重传 file） |
| `test_execute_reads_file_by_storage_key` | execute 凭 file_storage_key 取文件 + 重新 parse |
| `test_file_storage_cleanup_after_execute` | execute 成功后 preview 文件清理（10min TTL 内未 execute 由 cron 清理） |

#### Execute Idempotency（#2.27，v2.2 P0）
| 测试 | 验证点 |
|---|---|
| `test_execute_same_token_twice_success_replay` | 双击「确认导入」→ 首次成功，第二次返回 200 + `idempotent_replay=true` + 原结果（不重复落库） |
| `test_execute_same_token_twice_running_concurrent` | `asyncio.gather` 并发 2 个 execute → 一个 RUNNING，另一个抛 `AI_IMPORT_BATCH_RUNNING` |
| `test_execute_failed_batch_rejected` | FAILED 批次重放 → `AI_IMPORT_ALREADY_EXECUTED`（不能复活失败批次） |
| `test_execute_expired_batch_rejected` | EXPIRED 批次重放 → `AI_IMPORT_ALREADY_EXECUTED` |
| `test_concurrent_execute_same_batch` | `asyncio.gather` 模拟 10 并发 execute → 仅 1 个 CAS 成功，其余 9 个进幂等返回 |

#### Lifecycle State Machine Enum（#2.26，v2.2 P0）
| 测试 | 验证点 |
|---|---|
| `test_state_enum_writes_and_reads_roundtrip` | ORM `ImportBatchStatus.SUCCESS` 写入 → 读回仍是 Enum 成员 |
| `test_state_db_check_rejects_invalid_value` | 直接 SQL 写 `status="RUNING"`（typo）→ DB CHECK 拒绝 |
| `test_state_illegal_transition_rejected` | SUCCESS → RUNNING 转换 → `AI_IMPORT_ILLEGAL_TRANSITION` |
| `test_state_cas_prevents_race` | 并发 `_transition_batch_status(CREATED → RUNNING)` → 仅 1 个成功（CAS 互斥） |

#### Redis Cache Fallback（#2.19 v2.2 P0）
| 测试 | 验证点 |
|---|---|
| `test_preview_cache_missing_falls_back_to_db` | Redis 全量丢失 → execute 凭 token 反查 DB 仍能 execute（性能降级但功能不丢） |
| `test_preview_cache_corrupted_falls_back_to_db` | Redis value 被篡改 → 反查 DB 不受影响 |
| `test_preview_neither_cache_nor_db_rejected` | Redis miss + DB miss → `AI_IMPORT_PREVIEW_INVALID` |

#### employee_no sync_mode（#2.24 v2.2 P1）
| 测试 | 验证点 |
|---|---|
| `test_employee_no_create_only_rejects_existing` | sync_mode=CREATE_ONLY + employee_no 已存在 → `AI_IMPORT_EMPLOYEE_NO_EXISTS` |
| `test_employee_no_update_profile_updates_safe_fields` | sync_mode=UPDATE_PROFILE → 仅更新 OVERWRITE_ALLOWED，user_name 不动 |
| `test_employee_no_full_sync_overwrites_username` | sync_mode=FULL_SYNC → user_name 也被覆盖 |
| `test_employee_no_null_falls_back_to_username_match` | employee_no 为 NULL → 退化到 user_name 匹配 |

#### ImportBatch 业务日志（#2.28，v2.2 P1）
| 测试 | 验证点 |
|---|---|
| `test_log_records_all_lifecycle_events` | dry_run → execute → chunk → finish 全流程 → log 表覆盖 CREATED/PREVIEW_DONE/EXECUTE_START/CHUNK_PROGRESS/EXECUTE_FINISH |
| `test_log_cascade_delete_with_batch` | 删 batch 行 → 关联 log 行自动 CASCADE 删除 |
| `test_log_records_chunk_progress` | 2000 行导入 → log 表含 20 条 CHUNK_PROGRESS（chunk_size=100） |
| `test_log_records_fatal_error` | 模拟 OperationalError → EXECUTE_FAILED event 含 error_code + chunk_index |

#### Import Cancel（#2.29，v2.2 P2）
| 测试 | 验证点 |
|---|---|
| `test_cancel_created_batch_cancels` | CREATED 状态 → POST /cancel → CANCELLED + 清理 preview 文件 |
| `test_cancel_running_batch_stops_after_current_chunk` | RUNNING 状态 → POST /cancel → 当前 chunk 完成，剩余 chunk 不跑 → PARTIAL_SUCCESS + log 标记 cancelled=true |
| `test_cancel_terminal_batch_rejected` | SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED 终态 → POST /cancel → `AI_IMPORT_BATCH_NOT_CANCELLABLE` |
| `test_cancel_by_non_operator_forbidden` | 非 operator 非超管 → 403 |

#### API Response 精简（#3.3 v2.2 P1）
| 测试 | 验证点 |
|---|---|
| `test_import_response_excludes_failed_rows_array` | 2000 行全失败 → 响应不含 `failedRows` 数组，只有 `failedCount + failedRowsPreview(20) + failedRowsFile` |
| `test_import_response_includes_idempotent_replay_flag` | 重放成功批次 → `idempotentReplay=true` |

#### Max Rows 2000（#2.10 v2.2 P0）
| 测试 | 验证点 |
|---|---|
| `test_import_rejects_over_2000_rows` | 2001 行 Excel → `AI_IMPORT_TOO_MANY_ROWS` 拒绝 |
| `test_import_accepts_exactly_2000_rows` | 2000 行 Excel → 接受（边界条件） |
| `test_import_rejects_over_10mb_file` | > 10MB → `AI_IMPORT_FILE_TOO_LARGE` |

#### AI Tool Split（#2.14 v2.2 P0）
| 测试 | 验证点 |
|---|---|
| `test_ai_import_preview_returns_batch_and_token` | `user.import_preview` 返回 `batch_id + preview_token + summary`，不写用户 |
| `test_ai_import_execute_requires_token_from_preview` | 无 preview_token → execute 拒绝 |
| `test_ai_cannot_skip_preview` | LLM 直接调 execute（无 preview）→ 失败（防 AI 跳过 HITL） |
| `test_ai_import_preview_is_readonly` | preview 后 DB sys_user 行数不变 |

#### Records Truncation（#3.2 v2.2 P1）
| 测试 | 验证点 |
|---|---|
| `test_dry_run_truncates_conflict_records_to_200` | 2000 行全 conflict → `conflict_records` 截断到前 200 行 + `conflict_records_truncated=true` + `conflict_records_file` 下载链接 |
| `test_dry_run_no_truncation_when_under_limit` | 50 行 conflict → `conflict_records_truncated=false` |

#### Reason Audit Parameter（#2.30 v2.2 P1-3）
| 测试 | 验证点 |
|---|---|
| `test_reason_required_validation` | reason 缺失 → `AI_IMPORT_REASON_REQUIRED`（preview / execute / cancel / export 4 端点都验） |
| `test_reason_max_length_256` | reason 长度 257 → 拒绝；256 → 接受 |
| `test_reason_mismatch_between_preview_and_execute_rejected` | preview reason=A，execute reason=B → 拒绝（防中途改理由） |
| `test_reason_persisted_in_batch_log` | batch_log CREATED / EXECUTE_START / CANCELLED event 的 detail.reason 与传入值一致 |

#### FileStorage Protocol（#3.9 v2.2 P1-4）
| 测试 | 验证点 |
|---|---|
| `test_file_storage_protocol_contract` | LocalFileStorage 通过 `isinstance(x, FileStorage)` runtime check |
| `test_file_storage_path_traversal_blocked` | namespace 含 `../` → 拒绝 |
| `test_business_layer_independent_of_storage_impl` | mock LocalFileStorage / S3FileStorage 都能跑通 ImportService.dry_run |
| `test_file_storage_save_read_roundtrip` | save → read 字节完全一致 |
| `test_file_storage_delete_idempotent` | 删两次都不抛错 |

#### ImportBatch Single Aggregate Root（#3.6 v2.2 P1-2）
| 测试 | 验证点 |
|---|---|
| `test_no_import_preview_session_class` | grep 整个 codebase 无 `class ImportPreviewSession`（防残留） |
| `test_state_created_to_preview_done` | dry_run 入口 INSERT CREATED → 解析完成 UPDATE PREVIEW_DONE |
| `test_state_preview_done_to_running` | 用户确认 → CAS PREVIEW_DONE → RUNNING |
| `test_state_preview_done_to_expired` | PREVIEW_DONE + 10min → EXPIRED（cron） |
| `test_state_created_to_failed_on_parse_error` | 文件解析失败 → CREATED 直接转 FAILED |

#### ExportTask Audit（#2.31 v2.2 P1-5）
| 测试 | 验证点 |
|---|---|
| `test_export_creates_task_with_filter_snapshot` | 同步导出 → ExportTask 行创建 + filter_snapshot 含 accessible_dept_ids |
| `test_export_filter_snapshot_freezes_accessible_dept_ids` | 事后改部门结构 → 反查 task 时仍能看到当时的 dept_ids |
| `test_export_reason_required` | reason 缺失 → `AI_EXPORT_REASON_REQUIRED` |
| `test_export_task_30_day_retention` | created_at + 30 天 → cron 删 task + 删 file |
| `test_export_failure_records_error` | 模拟 DB 错 → status=FAILED + error_code + error_message |
| `test_export_audit_chain_joinable_with_operation_log` | sys_operation_log.export_id ↔ sys_user_export_task.export_id 可 JOIN |
| `test_ai_export_always_creates_task` | AI tool `user.export` 任何行数都建 task（即使 1 行） |

### 8.2 API 测试（`tests/modules/system/api/test_user_import.py`）

| 测试 | 验证点 |
|---|---|
| `test_post_import_dry_run` | `?dry_run=true` 返回预检结果 |
| `test_post_import_actual` | 正式导入 + 半成功策略 |
| `test_post_import_unauthorized` | 无 system:user:import → 403 |
| `test_get_export_streaming` | StreamingResponse + Content-Disposition |
| `test_get_template` | 模板下载 |

### 8.3 前端 vitest

| 测试 | 验证点 |
|---|---|
| 导入弹窗 3 步流程切换 | step1 → step2 → step3 |
| on_conflict radio 默认 skip | UI 默认值 |
| 预检 conflict 展开详情 | failed rows 展开 |
| 导出超阈值 toast 提示 | > 5000 行提示文案 |

### 8.4 E2E（Playwright）

| 场景 | 步骤 |
|---|---|
| 端到端导入 | 下载模板 → 填数据 → 上传 → 预检 → 确认 → 列表刷新看到新用户 |
| 导出 | 设 filter → 点导出 → 浏览器下载 → 用 Excel 打开校验内容 |

---

## 9. 参考借鉴

| 项目 / 文档 | 是否借鉴 | 说明 |
|---|---|---|
| 现有 `user_service.batch_delete_users` | ✅ | 半成功 + 审计 + data_scope 的实现模式直接复用 |
| 现有 `user.batch_delete` AI tool | ✅ | HITL + dry_run + result_view 模式参考 |
| SR-24 `file.parse` | ✅ | AI tool 接 file_path 而非 multipart 的设计 |
| spec §2.4 sensitive_input 二分法 | ✅ | password 不进签名策略 |
| spec §6.4 data_scope | ✅ | ensure_targets_in_scope 校验 dept_id |
| openpyxl 文档 | ✅ | xlsx 读写标准库 |
| Rails ActiveImport / Laravel Excel | ⚠️ 部分 | 借鉴「行级错误收集」语义，不借鉴具体 API |
| NestJS CLI scaffold | ❌ | CLI 模板生成器是后续 spec，本 spec 不依赖 |

---

## 10. Plan 状态块

> **v2.2 P0/P1/P2 新增 Task 已编号到 Phase 1/2 对应位置**（标注 `v2.2`）。原 v2.1 Task 编号不变，便于交叉引用。

### Phase 1（页面层，目标 3-4 周，含 v2 + v2.1 + v2.2 增强）

#### v2.2 P0/P1 基础设施（先做，所有后续 Task 依赖）
- [x] Task 0a **[v2.2 P1 #2.2]** ✅ 已完成（2026-08-03，4b7a048）：建 `app/modules/system/user/` 子包骨架（service.py / constants.py / schemas.py / models.py / import_service.py / import_parser.py / import_validator.py / import_state.py / export_service.py 9 个空文件 + `__init__.py` 重导出 facade）+ 旧 `service/user_service.py` 暂保留（双路径解析同一 singleton，零中断）
- [x] Task 0b **[v2.2 P0 #2.26 + P1-2]** ✅ 已完成（2026-08-03，d87fe44）：`ImportBatchStatus(Enum)`（含 `PREVIEW_DONE` v2.2 P1-2）+ `EmployeeNoSyncMode(Enum)` + `ExportTaskStatus(Enum)` + `LEGAL_TRANSITIONS` 映射（含 CREATED → PREVIEW_DONE / FAILED 分支）+ `_transition_batch_status` CAS helper（在 `import_state.py`，用 sqlalchemy.Table 抽象避免循环依赖 ORM）+ 11 个单测覆盖（spec 4 核心 + 7 补充，含非法转换/CAS 互斥/合法转换 4 方向）
- [x] Task 0c **[v2.2 P0 #2.10]** ✅ 已完成（2026-08-03，f838756）：`USER_IMPORT_MAX_ROWS = 2000` / `USER_IMPORT_SYNC_THRESHOLD = 2000` / `USER_EXPORT_ASYNC_THRESHOLD = 5000` / `MAX_PREVIEW_RECORDS = 2000` 常量（在 `constants.py`，**不进 settings 防误改**）
- [x] Task 0d **[v2.2 P1-4 #3.9]** ✅ 已完成（2026-08-03，8cf3970）：`app/core/file_storage.py` 模块：`FileStorage` Protocol + `LocalFileStorage` 默认实现（路径穿越防御）+ `MockFileStorage`（测试用）+ `get_file_storage()` DI 工厂（进程级单例）+ `settings.FILE_STORAGE_BACKEND` / `LOCAL_FILE_STORAGE_ROOT` 配置项 + ruff TID251 banned-api lint 规则禁止业务层 import 具体类 + 17 个单测覆盖（Protocol runtime check / 路径穿越 / roundtrip / delete 幂等 / exists / public_url）
- [x] Task 0e **[v2.2 P1-3 #2.30]** ✅ 已完成（2026-08-03，80e77d1）：`ReasonSchema` Pydantic mixin（`reason: str = Field(min_length=1, max_length=256)` + field_validator strip 后非空校验，spec §2.30 line 1420 全空白拒绝）+ `AI_IMPORT_REASON_REQUIRED` / `AI_EXPORT_REASON_REQUIRED` / `AI_IMPORT_REASON_MISMATCH` 错误码（在 service 层抛 `BusinessRuleException`）+ `validate_reason_consistency(preview_reason, execute_reason)` 一致性校验 helper + 11 个单测覆盖

#### 数据模型 + Helper（基础）
- [ ] Task 1: `UserImportRecord`（含 `dept_input` / `role_input` / `employee_no`）/ `ImportDryRunResult`（v2.2 P1 含 `*_truncated` 字段 + `*_records_file`）/ `ImportResult`（v2.2 P1 含 `failed_rows_preview` / `failed_rows_file` / `idempotent_replay`）/ `FailedRow` / `UserImportBatch`（v2.2 P1-2：原 ImportPreviewSession 已合并删除）/ `UserExportTask`（v2.2 P1-5 #2.31）Pydantic schema
- [ ] Task 2: alembic migration：`sys_user_import_batch` 新表（v2.2 P0：`status` 用 PostgreSQL ENUM 类型含 `PREVIEW_DONE` + CHECK；v2.2 P1-3：加 `reason` 字段）+ `sys_user.employee_no` 字段（UNIQUE 索引）+ `sys_user_import_batch_log` 表（v2.2 P1 #2.28，FK CASCADE）+ `sys_user_export_task` 表（v2.2 P1-5 #2.31）
- [x] Task 3 ✅ Plan 已完成（2026-08-03）：`OVERWRITE_NEVER` / `OVERWRITE_ALLOWED` / `EXPORT_ALLOWED_FIELDS` 常量落 `app/modules/system/user/constants.py`；`get_default_password(db)` helper 落 `app/modules/system/user/helpers.py`，缺失/禁用抛 `AI_IMPORT_DEFAULT_PASSWORD_NOT_SET`（直接查 sys_config 不走 redis cache，避免测试间污染 + admin 改值即时生效）。11 用例（含常量静态校验防 typo + helper DB 行为）。
- [x] Task 4 ✅ Plan 已完成（2026-08-03）：`resolve_dept` 落 `app/modules/system/user/import_validator.py`，名称 / 路径双模式 + 禁用部门（status='2'）一律视为不存在（安全原则：防用户分到停用部门）。8 用例（spec 5 + 新增 whitespace 段 + 禁用名称/路径各 1）。
- [ ] Task 5: `user_service.resolve_role_input`（code/name 双支持 + 去重，#2.18）+ 单测（4 用例）
- [ ] Task 6: `user_service.check_permission_boundary`（#2.15）+ 单测（3 用例）
- [ ] Task 7: `user_service.check_dept_data_scope`（#2.11）+ 单测（3 用例）
- [ ] Task 7a **[v2.2 P1 #2.24]**: `_resolve_existing_user`（employee_no 优先 + user_name 兜底）+ `EmployeeNoSyncMode` 应用逻辑 + 单测（4 用例：CREATE_ONLY / UPDATE_PROFILE / FULL_SYNC / NULL 兜底）

#### 主流程方法
- [ ] Task 8: `user_service.parse_import_excel` + 单测（7 用例 + v2.2 P0 边界用例 `test_import_rejects_over_2000_rows`）
- [ ] Task 9: `user_service.dry_run_import_users`（v2.2 P0 #2.19：业务数据 INSERT batch + Redis cache only；v2.2 P1 #3.2：records 截断 + 写 `*_records_file`；权限校验）+ 单测（5 用例 + 6 preview 用例 + v2.2 用例 `test_preview_cache_missing_falls_back_to_db` / `test_dry_run_truncates_conflict_records_to_200`）
- [ ] Task 10: `user_service.batch_create_users_from_records`（preview_token 三重校验 + v2.2 P0 #2.27 CAS 幂等保护 + chunk 100 + savepoint + IntegrityError 区分 + v2.2 P1 写 batch_log + v2.2 P1 API 响应精简 failed_rows 数组）+ 单测（8 用例 + 4 transaction + 3 concurrency + 4 batch + v2.2 用例 `test_execute_same_token_twice_success_replay` / `test_concurrent_execute_same_batch` / `test_log_records_all_lifecycle_events`）
- [ ] Task 11: `user_service.export_users_to_excel`（字段白名单 + data_scope + 异步阈值 + v2.2 P1-5 #2.31 强制建 ExportTask + filter_snapshot 冻结 accessible_dept_ids + reason 必填 + 30 天 TTL）+ 单测（4 用例 + v2.2 P1-5 用例：`test_export_creates_task_with_filter_snapshot` / `test_export_filter_snapshot_freezes_accessible_dept_ids` / `test_export_reason_required` / `test_export_task_30_day_retention` / `test_export_failure_records_error`）

#### HTTP API
- [ ] Task 12: HTTP `POST /system/user/import`（multipart + `?dry_run=true` 返回 preview_token + 正式导入接 `preview_token` + v2.2 P1 `sync_mode` body 参数 + v2.2 P0 #2.27 `idempotentReplay` 响应字段 + v2.2 P1 #3.3 响应精简）+ API 测试
- [ ] Task 13: HTTP `POST /system/user/export`（**从 GET 改 POST**，#2.23，body 含 filter + v2.2 P1-3 reason 必填）+ HTTP `GET /system/user/export/{export_id}` 任务详情 + HTTP `GET /system/user/export` 任务列表（v2.2 P1-5 #2.31）+ API 测试
- [ ] Task 14: HTTP `GET /system/user/import/template`（4 sheet + DataValidation，#2.13/#2.16）+ 测试（6 用例）
- [ ] Task 15: HTTP `GET /system/user/import/{batch_id}`（按 batch_id 查导入结果，复用审计反查）+ 测试
- [ ] Task 15a **[v2.2 P2 #2.28]**: HTTP `GET /system/user/import/{batch_id}/logs`（批次操作日志查询，分页 + event filter）+ 测试
- [ ] Task 15b **[v2.2 P2 #2.29]**: HTTP `POST /system/user/import/{batch_id}/cancel`（CREATED 直接 cancel + RUNNING 协作式 cancel + 终态拒绝）+ 测试（4 用例）
- [ ] Task 15c **[v2.2 P2]**: HTTP `GET /system/user/import`（批次列表分页查询，按 operator_id / status / created_at 过滤）+ 测试

#### Seed + 前端
- [ ] Task 16: 权限码 seed（`system:user:import` / `system:user:export`）+ `sys_config.auth:default_password` seed 到 `sync_menus.py` / `init_db.py`
- [ ] Task 17: 前端 `user-import-modal.vue` 3 步弹窗（v2.2 P2 §6.2 增强：4 卡片统计 + 错误清单前 20 条 + sync_mode radio + cancel 按钮 + idempotentReplay toast）+ vitest
- [ ] Task 18: 前端列表页 3 按钮（导入 / 导出 / 下载模板）+ i18n keys（`common.import*` 公共命名空间）
- [ ] Task 19: 前端导出按钮改 POST + fetch/Blob 下载逻辑（不能用 `<a href>`）+ 超阈值 toast
- [ ] Task 19a **[v2.2 P2]**: 前端「导入历史」页面（`src/views/system/user/modules/user-import-history.vue`，列表 + 状态查询 + log 详情抽屉 + cancel 按钮）+ vitest

#### 收尾
- [ ] Task 20: 后端 lint + 全量 pytest + 覆盖率 ≥ 70%
- [ ] Task 21: 前端 lint + typecheck + vitest + Playwright E2E
- [ ] Task 22: `cleanup_expired_batches` cron（每日 02:00 清理 90 天前 batch + 关联 failed_rows 文件 + 关联 batch_log CASCADE 删除，#2.22.1）+ `cleanup_expired_previews` cron（每小时清理 PREVIEW_DONE 超 10min → EXPIRED + 删孤儿 preview 文件 + 写 batch_log EXPIRED event，§2.26 v2.2 P1-2）+ `cleanup_expired_export_tasks` cron（每日 02:30 清理 30 天前 ExportTask + 关联 export 文件，v2.2 P1-5 #2.31）+ 测试
- [ ] Task 22a **[v2.2 P0/P1/P2 验收]**: v2.2 P0/P1/P2 全部决策对应测试覆盖率验证（按 §8 测试补充清单逐项跑过）+ 决策对齐审计（grep spec 中 #2.27/#2.28/#2.29/#2.30/#2.31/#3.9 引用确认实现完整）

### Phase 2（AI 层，目标 2-3 周）

- [ ] Task 23: AI tool `user.list`（补 gap）
- [ ] Task 24: AI tool `user.lookup`（补 gap）
- [ ] Task 25: AI tool `user.update`（补 gap，含 dept_id 双向校验 + OVERWRITE_ALLOWED 过滤）
- [ ] Task 26 **[v2.2 P0 #2.14 拆分]**: AI tool `user.import_preview`（只读，detail_card）+ 单测（4 用例：`test_ai_import_preview_returns_batch_and_token` / `test_ai_import_preview_is_readonly` 等）
- [ ] Task 26a **[v2.2 P0 #2.14 拆分]**: AI tool `user.import_execute`（强制 HITL，rows_affected）+ LLM prompt 引导（防跳过 preview）+ 单测（`test_ai_cannot_skip_preview` / `test_ai_import_execute_requires_token_from_preview`）
- [ ] Task 27: AI tool `user.export`（POST 同步路径，复用 user_service.export_users_to_excel；v2.2 P1-5 #2.31 强制建 ExportTask + v2.2 P1-3 reason 必填）+ 测试（含 `test_ai_export_always_creates_task`）
- [ ] Task 28: 鉴权矩阵 11 场景全覆盖（参考 spec §10.3）
- [ ] Task 29: 浏览器 E2E（贴文本"加这几个用户" → `import_preview` → HITL 抽屉展示 summary → 用户确认 → `import_execute` → 落库 + batch_id 审计反查）

### Phase 3（推迟到独立 spec）

- [ ] 异步通道 `broadcast_to_user`（arq + WebSocket/SSE 双通道）
- [ ] 批量 HITL 协议扩展（行级勾选）
- [ ] `user.export` 改造支持异步（> 阈值自动切换）
- [ ] `user.import_execute` 异步通道（> 2000 行入队，CREATED → QUEUED → RUNNING → ...）
- [ ] `role` / `dept` / `job` 模块同款改造（依赖 CLI generator 模板）

---

## 11. 后续演进路径（CLI generator）

Phase 1+2 落地后，user 模块作为「参考实现」抽象出模板：

```
hohu-cli/hohu/templates/module/
├── backend/
│   ├── {{module}}_service.py.j2     # 含 import/export/dry_run 9 方法
│   ├── {{module}}_api.py.j2         # 含 /import /export /template endpoint
│   └── {{module}}_ai_tools.py.j2    # 含 9 个 @ai_tool
├── frontend/
│   └── ...
└── config.yaml
```

详见待写 spec `2026-XX-cli-module-generator-design.md`。

---

## 12. 关联

- 实施进度：本 spec §10 Plan 状态块
- 审计设计：spec `2026-07-02-ai-tool-gateway-design.md` §2.7
- data_scope：spec `2026-07-02-ai-tool-gateway-design.md` §6.2
- HITL：spec `2026-07-02-ai-tool-gateway-design.md` §8
- 异步通道（推迟）：spec §10 v1.5+ Roadmap
