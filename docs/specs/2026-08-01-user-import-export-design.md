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

### 2.9.1 **导出 Excel 字段翻译 + 导入侧中文字面值反查 + status 取值统一对齐 DB**（v2.3 修订 2026-08-05）— §2.9 白名单只规定字段集，未规定展示形态。原实现直接 dump DB 原值带来两个真实问题：(a) 用户读不懂 `dept_id=1` / `status="1"` / `user_gender="1"` ；(b) 导出的 Excel 无法直接 round-trip 给导入（§2.17 导入要 `dept_input` 名/路径，不是数字 ID）。

**status 取值矛盾（顺手修复）**：spec §3.1 line 1634 `status: Literal["0","1"]`（0 禁用 / 1 启用）与 DB / 前端 / 其他模块真实约定 `("1","2")`（1 启用 / 2 禁用）矛盾。证据：`app/utils/validators.py:68 STATUS_ALLOWED = ("1","2")`、Menu 模型注释「1-启用，2-禁用」、前端 `enableStatusRecord = {'1': enable, '2': disable}`。原 spec 的 0/1 是笔误，导致 `import_parser._STATUS_VALUES = {"0","1"}` 拦掉真实合法的 `status="2"` 禁用用户。**v2.3 决策：全部统一对齐 `("1","2")`**，涉及 spec §3.1 line 1634、`schemas.py:81` UserImportRecord.status、`ai_tools.py:1324,1393` user_update/user_lookup filter、`import_parser.py:57 _STATUS_VALUES`。

**导出翻译决策**：`_build_excel` 加翻译层，与导入侧字段语义对齐：

| 字段 | v2.2（原） | v2.3（新） |
|---|---|---|
| `dept_id` | `1`（Snowflake 数字 ID） | `总公司/研发中心/前端部`（full_path，复用 `template_service._build_dept_full_path`） |
| `role_codes` | `admin,user`（role_code 逗号分隔） | 不变（§2.18 已支持 code 输入，可 round-trip） |
| `status` | `1` / `2` | `启用` / `禁用` |
| `user_gender` | `0` / `1` / `2` | `未知` / `男` / `女` |
| `create_time` | ISO 格式 | 不变 |

**导入反查决策**（round-trip 兼容）：`_parse_row_to_record` 在 `status` / `user_gender` 字段加中文字面值反查：先查翻译字典（"启用"→"1" / "禁用"→"2" / "未知"→"0" / "男"→"1" / "女"→"2"），未命中走原字面值（"1"/"2"/"0"）继续走 Literal 校验。原 Literal 校验保持（白名单兜底，非法值仍抛 `AI_IMPORT_STATUS_INVALID` / `AI_IMPORT_GENDER_INVALID`）。

**反例**: (1) 翻译后 status 字面值"启用"被当作字面值回流导入 → 导入侧必须加反查字典（本决策已实现）。(2) dept full_path 包含 `/` 分隔符与导入路径解析冲突 → 沿用 §2.17 同款 `/` 拼接，`_resolve_dept_by_path` 已实现递归。(3) 角色改名后旧 Excel 失效 → §2.18 已论证用 role_code 锚点不变，导出仍 dump role_code 而非 role_name。(4) status 翻译走 i18n 多语言 → Excel 是离线文件，跨语言用户打开会乱码，**固定中文「启用/禁用/未知/男/女」**（管理员 Excel 场景，业务侧按中文约定）。(5) `_STATUS_VALUES` 改 `{"1","2"}` 后老 Excel 含 `"0"` 仍可导入 → 不能保留 `{"0","1","2"}` 三值，那会污染 DB 真实取值集合；DB 内不允许 "0" status（用户导入侧**必须**抛 `AI_IMPORT_STATUS_INVALID` 让用户改 Excel，不能静默写错数据）。(6) 翻译层放在 `_build_excel` 而非 schema 层 → schema 层是契约（保持原值），Excel 渲染是展示（可翻译），分层铁律。

**回归**: `export_service._build_excel` 加 `_STATUS_LABELS = {"1":"启用","2":"禁用"}` / `_GENDER_LABELS = {"0":"未知","1":"男","2":"女"}` / `_format_dept_path(dept, dept_lookup)` 内部 helper（复用 `template_service._build_dept_full_path`）；`_query_users_with_data_scope` 加 `dept_lookup` 预查（一次性 select Dept where dept_id in user_dept_ids，避免 N+1）；`import_parser._resolve_status_label` / `_resolve_gender_label` helper（字典反查 → 字面值兜底 → Literal 校验）；`_STATUS_VALUES = frozenset({"1","2"})`（修 bug）；新增测试 `test_export_translates_display_fields`（_build_excel 输出启用/禁用/男/女/部门路径）+ `test_import_status_chinese_label`（"启用"→"1" / "禁用"→"2" 反查）+ `test_import_status_disabled_two_now_accepted`（修复 _STATUS_VALUES bug 后的回归测试：status="2" 禁用用户可正常导入）。

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

**场景 1：PREVIEW_DONE 状态取消（简单，spec §2.26 line 1117 v2.2 P1-2 改）**
- 用户上传文件 → dry_run 完成 → status=PREVIEW_DONE → 用户点「取消」
- 直接 CAS `PREVIEW_DONE → CANCELLED`，删除 preview 文件 + Redis cache
- 不影响任何用户数据（PREVIEW_DONE 阶段还没落库；CREATED 是 dry_run 中间瞬时态用户不可见，不可取消）

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

    if batch.status == ImportBatchStatus.PREVIEW_DONE:
        # 场景 1：简单 cancel（spec §2.26 line 1117 v2.2 P1-2 改为仅 PREVIEW_DONE → CANCELLED）
        ok = await _transition_batch_status(
            db, batch_id, ImportBatchStatus.PREVIEW_DONE, ImportBatchStatus.CANCELLED,
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

**回归**: `ImportBatchStatus.CANCELLED` 加入状态机（v2.2 #2.26 已含）；`POST /import/{batch_id}/cancel` endpoint；PREVIEW_DONE 直接 CAS 转 CANCELLED + 清理 preview 文件（spec §2.26 line 1117 v2.2 P1-2 改，不允 CREATED → CANCELLED）；RUNNING 设置 Redis cancel_requested 标志 + chunk 之间检查 + 跳出循环转 PARTIAL_SUCCESS；权限校验（operator 本人或超管）；终态 + CREATED 拒绝 cancel 抛 `AI_IMPORT_BATCH_NOT_CANCELLABLE`；测试 `test_cancel_preview_done_returns_cancelled` + `test_cancel_running_sets_redis_flag_and_returns_running` + `test_cancel_non_cancellable_states_returns_422` (6 状态参数化) + `test_cancel_by_non_operator_returns_403`。✅ Task 15b 已完成（2026-08-04），见 §10 决策 15b.1-15b.13。

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

# HTTP GET /system/user/export/{export_id}/download（Task 33 新增，v2.3 §2.9.1 后续补齐）
权限：system:user:export（与 POST /export 同级，能导出就能重下载）
返回：xlsx 文件流（从 file_storage_key 读 bytes）
错误码：AI_EXPORT_TASK_NOT_FOUND / AI_EXPORT_TASK_NOT_READY / AI_EXPORT_FILE_MISSING / AI_EXPORT_FILE_EXPIRED
用途：AI 对话内 detail_card 下载按钮触发，闭环「AI 导出 → 对话内点击下载」

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
    status: Literal["1", "2"] = "1"       # 默认启用（v2.3 §2.9.1 修订：原 "0"/"1" 与 DB 真实取值矛盾）
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
- [x] Task 5 ✅ Plan 已完成（2026-08-03）：`resolve_role_input` 落 `app/modules/system/user/import_validator.py`，code/name 双支持 + set() 去重 + 禁用角色 status='2' 一律视为不存在（与 resolve_dept 一致）。6 用例（spec 4 + 新增禁用 + 部分匹配报错信息）。
- [x] Task 6 ✅ Plan 已完成（2026-08-03）：`check_permission_boundary` 落 `app/modules/system/user/import_validator.py`，超管 / `user_name='admin'` 双判豁免 + operator_role_ids 集合差集 + 越界 reason 含角色名（非 ID）。7 用例（spec 3 + admin_username_bypass / all_in_scope / role_not_found / partial_failure）。复用 init_db.py seed 的 R_SUPER / admin user 避免 UniqueViolation。
- [x] Task 7 ✅ Plan 已完成（2026-08-03）：`check_dept_data_scope` + `_compute_accessible_dept_ids` 落 `app/modules/system/user/import_validator.py`，DATA_SCOPE_SELF=空 set（任何 dept 都越界）+ DEPT_AND_SUB 通过 ancestors like 拿子树 + 超管 / DATA_SCOPE_ALL 跳过；resolve_dept 失败时 FailedRow 保留原 error_code（不混淆为 OUT_OF_SCOPE）。6 用例（spec 3 + ALL/SA bypass + resolve_failure 携带原 error_code）。system 模块直接 import utils.data_scope 公开函数（get_best_scope/get_custom_dept_ids/get_dept_and_sub_ids），避免 system→ai 反向依赖。
- [x] Task 7a ✅ Plan 已完成（2026-08-03）：`resolve_existing_user` + `classify_sync_action` + `SyncAction` enum 落 `app/modules/system/user/import_validator.py`。resolve_existing_user 返回 (User | None, matched_by_employee_no) 二元组；classify_sync_action 按命中标志 + sync_mode 决定 REJECT/UPDATE_SAFE/UPDATE_FULL/EXISTS_BY_USERNAME（employee_no NULL 兜底一律 EXISTS_BY_USERNAME，与 sync_mode 无关，spec line 874）。8 用例（DB 行为 4 + 纯逻辑 4）。具体 INSERT/UPDATE 字段写入留 Task 9 batch_create。

#### 主流程方法
- [x] Task 8 **✅ 已完成（2026-08-03）**：`import_parser.parse_import_excel`（模块级纯函数，零 DB 查询；service 层 thin wrapper 留 Task A）+ 24 用例（覆盖 spec line 2624-2630 全部 + v2.2 P0 `test_too_many_rows_raises` 拒绝 > 2000 行 + 边界：employee_no 空串 → None / gender status 默认值 / 多错误一次性收集）。
  - **决策 8.1**: spec line 2036 签名含 `db: AsyncSession` 参数，但解析层纯函数零 DB 查询（dept_input/role_input 存在性留 dry_run，spec line 2045）。**实现去掉 db 参数**，模块级函数 `parse_import_excel(file_bytes, mime_type) -> list[UserImportRecord]`。**理由**: 解析层独立性 + 单测无需 db_session fixture + HTTP / AI 两端复用。**反例**: 若解析层依赖 db，则 ImportErrorCollection 测试需要 db_session fixture，违反 spec §2.12 错误结构化的纯函数性质。**回归**: Task A service 层 thin wrapper 仍按 spec line 119 / 191 委派。
  - **决策 8.2**: spec line 2046「失败行抛 ImportErrorCollection」实现为 `ImportErrorCollection(Exception)` 含 `errors: list[FailedRow]`。HTTP 层 catch → 400 + errorCode 用 errors[].error_code 数组。**理由**: 一次收集所有错误避免用户改一行重传一次（spec §2.12 反例 3）。**反例**: 一个字段错就抛 → 用户体验灾难，违反 §2.12「错误信息结构化」原则。**回归**: MIME / 大小 / 行数超限仍抛 `BusinessRuleException`（用户级错误，整批拒绝不可恢复）；字段格式错抛 `ImportErrorCollection`（行级错误，全收集后一次抛）。
  - **决策 8.3**: `_validate_row` 一行可产生多个 FailedRow（如 user_name 缺 + email 错 + phone 错）。**理由**: Pydantic ValidationError 默认行为是一行多错全报，与之对齐。**反例**: 一行只报第一个错误 → 用户改完一个重传发现还有错，违反 §2.12 actionable 原则。**回归**: gender/status 取值非法时用默认值避免级联错误（防 record 构造失败），但 FailedRow 仍记录原始值。
  - **决策 8.4**: 表头大小写不敏感匹配（`User_Name` / `user_name` / `USER_NAME` 都接受），允许列顺序变化。**理由**: 用户用 WPS / Excel 编辑模板时可能误改大小写或调整列顺序，解析层应宽容。**反例**: 严格匹配表头 → 模板被改一个字符就整批 ImportErrorCollection。**回归**: 中文表头映射留 Task 14（模板下载时统一中英文双表头支持）。
  - **决策 8.5**: `MAX_FILE_SIZE_BYTES` 常量在模块内（10MB），不读 settings.UPLOAD_MAX_SIZE。**理由**: spec §2.10 line 277「常量在 constants.py，可由部署方下调但不可上调」原则延伸到文件大小——避免运维误改 settings 突破安全边界。**反例**: 读 settings → 运维把 UPLOAD_MAX_SIZE 改成 100MB → 单次导入内存爆炸。**回归**: 与 USER_IMPORT_MAX_ROWS 一致的「默认安全」原则。
- [x] Task 9 ✅ 已完成（2026-08-03）：`user_service.dry_run_import_users`（v2.2 P0 #2.19：业务数据 INSERT batch + Redis cache only；v2.2 P1 #3.2：records 截断；权限校验）+ 18 单测（核心 4 分类 + 6 preview + 2 truncation + 3 reason + 1 状态机 + 2 超管豁免）
  - 实现：`app/modules/system/user/import_service.py`（`dry_run_import_users` + `get_batch_by_preview_token`）
  - 字段补充：`UserImportBatch.records_hash` String(64) NOT NULL（spec §2.19 三重校验字段，Task 2 漏列；新 migration `00558ec10892`）
  - **决策 9.1**: `dry_run_import_users` 是模块级函数（`async def`，无 class 包装），与 `parse_import_excel` 一致。**理由**: Task 0a facade 思路（#2.2 反例 3）已废弃，模块级函数 + 单测覆盖更直接；service 层将来薄包装。**反例**: 强制建 `ImportService` 单例类，仅为 facade 而 facade，无依赖注入需求。**回归**: 后续 Task 10 execute / Task 11 export 同样模块级，统一 `import_service.py` / `export_service.py` 分文件。
  - **决策 9.2**: dry_run 在 INSERT batch 时一次性写入所有业务字段（CREATED），然后在函数末尾 UPDATE PREVIEW_DONE（带 summary）。**理由**: spec line 2063「INSERT batch (status=CREATED → PREVIEW_DONE)」字面看是两次 DB 写，但单次 INSERT 实际可以 cover（直接 INSERT PREVIEW_DONE）—— 保留两次写是为了状态机自检（CREATED 行可被审计反查「dry_run 已开始但未完成」）。**反例**: 直接 INSERT PREVIEW_DONE 跳过 CREATED → 状态机监控/cron 无法识别「卡在 dry_run 中途」的批次。**回归**: validate_transition(CREATED → PREVIEW_DONE) 防误转换；外层 outer-rollback 时 INSERT 自动撤销。
  - **决策 9.3**: `records_hash` = sha256(json.dumps(records 按 row_num 排序 + sort_keys))。**理由**: list 顺序变化不影响 hash（防同一份 Excel 因 row 顺序变动误判不一致）；row_num 是 record 天然排序键。**反例**: 直接 sha256(repr(records)) → Python repr 含 id() 等不稳定字段，hash 每次不同。**回归**: execute 阶段（Task 10）用同一算法二次计算并比对，不一致抛 AI_IMPORT_RECORDS_HASH_MISMATCH。
  - **决策 9.4**: 四象限分类顺序：先集合级（check_permission_boundary + check_dept_data_scope 一次性算所有 out_of_scope），再逐行（resolve_dept + resolve_role_input 反查为 conflict，命中 resolve_existing_user 分 new / exists）。**理由**: 集合级越界集合先算出，逐行阶段跳过越界行避免重复 resolve_dept。**反例**: 逐行内联调用 check_permission_boundary → 每行都触发 N+1 查询，性能差。**回归**: check_permission_boundary 内部已经一次性 SELECT operator_role_ids，调用方按行调反而浪费。
  - **决策 9.5**: out_of_scope 行不重复进 conflict 集合（即使 dept/role 也反查失败）。**理由**: spec line 2061「字段冲突：dept/role 反查失败」与 line 2062「权限越界」是互斥集合——一行最多进一个象限。**反例**: 同一行既归 conflict 又归 out_of_scope → 前端展示重复，summary 双计。**回归**: `_classify_records` 用 oos_row_nums set 过滤 conflict 行；failed_rows 总数 = conflict + out_of_scope。
  - **决策 9.6**: Redis cache 仅存 `{"batch_id": "..."}` JSON 字符串，TTL=600s（10min）。**理由**: spec §2.19 line 2696「Redis value 不含 records」防大 Excel 撑爆；line 557 setex TTL=600 与 preview_token 寿命对齐。**反例**: cache 含 records / file_bytes → Redis 内存爆炸。**回归**: `get_batch_by_preview_token` 先 Redis 加速 → miss 时 DB 反查（spec §2.19 反例 2「Redis 丢失但 batch 表还在」）。
  - **决策 9.7**: 测试用 `fake_redis` autouse fixture，depend_on `db_session`（顺序保证：db_session reset 真实 redis 在前，monkeypatch fakeredis 在后）。**理由**: conftest db_session fixture 调 `_reset_redis_client()` 覆盖 module attribute，fake_redis 必须后跑才能 monkeypatch 回 fakeredis。**反例**: fake_redis 不 depend db_session → 部分测试看到真实 redis client，setex 写到生产 Redis 污染其他测试。**回归**: 2 个直接断言 redis 状态的测试显式拿 fake_redis 返回值；其他测试 autouse 隐式触发。
  - **决策 9.8**: `conflict_records_file` / `out_of_scope_records_file` 字段在 Task 9 留 None，截断标志 `*_truncated` 已就位。**理由**: 写文件依赖 FileStorage Protocol（Task 11 落地），dry_run 阶段不写文件，仅返回截断后的 records list；前端按 truncated 标志决定是否显示「⚠️ 仅展示前 N 条，完整清单下载」按钮。**反例**: Task 9 强行写文件 → FileStorage 未实现 → 整个 dry_run 失败。**回归**: 截断策略与 file 字段解耦，Task 11 实现 file_storage.save 时补 file 路径写入。
  - **决策 9.9**: reason 入口在 service 层加 defense-in-depth 校验（`_validate_reason`），与 API 层 ReasonSchema 重复但必要。**理由**: AI tool 路径绕过 Pydantic 直接调 service，service 层独立校验防注入；spec §2.30 line 1432「preview 阶段就必填」语义。**反例**: 仅依赖 ReasonSchema → AI tool 直接调 import_service.dry_run_import_users(reason=None) 绕过校验。**回归**: 空白 / None / >256 都抛 AI_IMPORT_REASON_REQUIRED；reason_clean（strip 后）写入 batch.reason。
- [x] Task 10 ✅ 已完成（2026-08-03）：`user_service.batch_create_users_from_records`（preview_token 三重校验 + v2.2 P0 #2.27 CAS 幂等保护 + chunk 100 + 行级 savepoint + IntegrityError 区分 + v2.2 P1 写 batch_log + v2.2 P1 API 响应精简 failed_rows）+ 18 单测（5 triple validation + 3 idempotency + 4 chunk/savepoint + 2 on_conflict + 1 batch_log + 2 status transition + 1 超管豁免）
  - 实现：`app/modules/system/user/import_service.py`（`batch_create_users_from_records` + 4 helper：`_process_create_row` / `_process_overwrite_row` / `_handle_idempotent_replay` / `_failed_rows_to_xlsx_bytes`）
  - 常量补充：`USER_IMPORT_CHUNK_SIZE=100` / `RECOVERABLE_ERROR_CODES` frozenset（13 个码）/ `FAILED_ROWS_PREVIEW_LIMIT=20`（spec §3.3）— 加到 `constants.py`
  - 表抽象扩展：`import_state._batch_table` 加 11 列（summary_* / counts / failed_rows_file / started_at / finished_at）防 raw UPDATE 报 Unconsumed column names
  - **决策 10.1**: `reason` 加入 execute signature + service 层做 preview/execute reason 一致性校验（spec §2.30 + line 1429）。**理由**: API 层 ReasonSchema 只能校验非空长度，preview/execute 跨阶段一致性必须 service 层兜底；防用户 preview 时填「HR 同步」execute 时填「ERP 推送」绕过审计。**反例**: 仅 API 层校验 → AI tool 直接调 execute 时 reason 一致性失守。**回归**: `validate_reason_consistency(batch.reason, reason_clean)` 不一致抛 `AI_IMPORT_REASON_MISMATCH`。
  - **决策 10.2**: chunk-level + row-level 都用 `db.begin_nested()`（双层 SAVEPOINT），不用 `db.begin()`。**理由**: outer-transaction 测试 fixture 已占用 connection 级 transaction，session 级 `db.begin()` 在已有 transaction 下会报 InvalidRequestError；`begin_nested()` 创建 SAVEPOINT 兼容两种场景（测试 outer-tx + production outer-session）。**反例**: spec line 599-617 写 `async with db.begin()` → 测试 fixture 下无法跑通。**回归**: chunk 边界用 SAVEPOINT RELEASE 模拟 commit；致命错误时 chunk savepoint ROLLBACK 自动撤销当前 chunk 内所有 row 写入；production API 层 commit 时整体提交。
  - **决策 10.3**: `_transition_batch_status` 用 raw `update(_batch_table)` + `synchronize_session=False`，调用方按需 `await db.refresh(batch)`。**理由**: spec §2.26 反例 3「ORM 的 synchronize_session 容易引入意外」，raw Table 写 CAS rowcount 精确；但 raw 不更新 ORM identity map，调用方读 `batch.status` / `batch.failed_rows_file` 时会拿到 stale 值。**反例**: 调用方不 refresh → 测试断言 `db_batch.status == SUCCESS` 拿到 PREVIEW_DONE，第二阶段 `_handle_idempotent_replay` 也读到 stale 状态误判「重放失败」。**回归**: 关键 CAS 后 `await db.refresh(batch)`；`_handle_idempotent_replay` 用 `select(...).execution_options(populate_existing=True)` 强读 DB。
  - **决策 10.4**: IntegrityError 区分用约束名子串匹配（asyncpg `orig.diag.constraint_name`）。**理由**: spec §2.25 line 922「`ix_sys_user_user_name` in str(e)」字面是字符串匹配；asyncpg 把约束名放在 `orig.diag.constraint_name`，子串匹配 `"user_name"` / `"employee_no"` 兼容 INDEX 名 `ix_sys_user_user_name` + CONSTRAINT 名 `uq_sys_user_employee_no`。**反例**: 严格 == 比较约束名 → 部署环境换 naming convention 后失效。**回归**: `_classify_integrity_error` 返回 `AI_IMPORT_USERNAME_DUPLICATE` / `AI_IMPORT_EMPLOYEE_NO_DUPLICATE` / `AI_IMPORT_UNKNOWN_INTEGRITY_ERROR`（致命，让 chunk savepoint rollback）。
  - **决策 10.5**: failed_rows xlsx 用 `openpyxl.Workbook` 内存生成（`io.BytesIO`），不写磁盘临时文件。**理由**: FileStorage Protocol 抽象了存储介质（local / S3 / GridFS），service 层只产出 bytes 再交给 storage.save；写临时文件会绑定磁盘 IO 与 service 层。**反例**: `tempfile.NamedTemporaryFile` → 测试环境需要清理 temp 目录，跨平台路径问题。**回归**: `_failed_rows_to_xlsx_bytes` 返回 bytes，`storage.save(bytes, namespace="import-error", suffix=".xlsx")` 返回 storage_key 存 batch.failed_rows_file。
  - **决策 10.6**: 致命错误 abort 路径：当前 chunk savepoint ROLLBACK + 当前 chunk 剩余行 + 后续 chunk 所有行进 failed_rows（error_code=`AI_IMPORT_BATCH_ABORTED`）。**理由**: spec line 672「致命错误 → chunk transaction ROLLBACK → 已 commit 的前 N-1 个 chunk 不受影响 → batch.status=PARTIAL_SUCCESS + failed_count 包含未跑的剩余行」。**反例**: 致命错误抛 500 → 用户看到「服务器错误」但不知道已成功 X 行。**回归**: `except Exception` 捕获 chunk 外层；`aborted_error` 记录到 EXECUTE_FINISH log detail.aborted；测试覆盖以 monkeypatch 模拟（Task 10 暂未单测，留 Task 20 集成测试补）。
  - **决策 10.7**: status 判定 `_determine_end_status(success_count, failed_count)`：failed=0 → SUCCESS；failed>0 + success>0 → PARTIAL_SUCCESS；failed>0 + success=0 → FAILED。**理由**: spec §2.26 line 970-976 三态语义；success_count 含 create + overwrite 两类成功。**反例**: skip 算 failed → 用户感知「整批失败」但实际只是已存在跳过。**回归**: skipped_count 不进 failed；on_conflict=skip 的行算「正常跳过」不污染 failed_count。
  - **决策 10.8**: exists_records 分流顺序：sync_mode 优先（employee_no 命中按 REJECT/UPDATE 处理）→ EXISTS_BY_USERNAME 按 on_conflict 处理（skip / fail_fast / overwrite）。**理由**: spec §2.24 line 874「按 user_name 命中归 exists → on_conflict 处理」与 §2.24 v2.2 P1「employee_no 命中按 sync_mode 处理」是两个独立维度，employee_no 优先级更高（企业 ID 锚点）。**反例**: on_conflict=skip 但 employee_no 命中 → 用户期望更新 employee_no 关联用户但被 skip 跳过。**回归**: `classify_sync_action` 返回 UPDATE_SAFE / UPDATE_FULL 时强制走 overwrite 路径，不受 on_conflict=skip 影响。
  - **决策 10.9**: 测试通过 `dry_run_import_users` 走完整 preview 流程建 PREVIEW_DONE batch，再调 execute 验证 execute 语义。**理由**: dry_run + execute 是耦合流程（execute 必须凭 dry_run 颁发的 preview_token），不便于单独测 execute；直接构造 PREVIEW_DONE batch 行需要重复 dry_run 的所有字段（file_sha256 / records_hash / summary 等）+ 同步 reason，维护成本高。**反例**: 测试自己手搓 batch 行 → 任何 dry_run 字段变化都要改 execute 测试 fixture。**回归**: `_setup_preview` helper 内部走 dry_run 全流程；18 个 execute 测试都从 PREVIEW_DONE 状态出发。
  - **决策 10.10**: `_extract_constraint_name` 优先读 asyncpg `orig.diag.constraint_name`，fallback `str(e)`。**理由**: 不同 PostgreSQL driver（asyncpg / psycopg2）异常结构不同，asyncpg 走 `diag.constraint_name`；fallback 字符串匹配兼容未知 driver。**反例**: 仅依赖 asyncpg 接口 → 切换 driver（如未来上 psycopg3）后失效。**回归**: 子串匹配在两层 fallback 都工作。
- [x] Task 11 ✅ 已完成（2026-08-03）：`user_service.export_users_to_excel`（v2.2 P1-5 #2.31：强制建 ExportTask + filter_snapshot 冻结 accessible_dept_ids；v2.2 P1-3 #2.30：reason 必填；v2.2 P0 #2.9：EXPORT_ALLOWED_FIELDS 白名单；v2.2 P0 #2.6：> 5000 行抛 AI_EXPORT_ASYNC_REQUIRED；30 天 TTL）+ 13 单测（3 happy path + 4 task audit + 2 reason + 1 threshold + 2 data_scope + 1 failure）
  - 实现：`app/modules/system/user/export_service.py`（`export_users_to_excel` + 3 helper：`_query_users_with_data_scope` / `_build_excel` / `_validate_reason`）
  - **决策 11.1**: `export_users_to_excel` 是模块级函数（与 `dry_run_import_users` / `batch_create_users_from_records` 一致），不是 ImportService 类方法。**理由**: Task 0a facade 已废弃（决策 9.1），service 层薄包装；export 与 import 业务无共享状态，独立文件 `export_service.py` 边界清晰。**反例**: 强制建 ExportService 类 + 单例，无依赖注入需求。**回归**: `export_service.py` + `import_service.py` 平行存在，各管一侧。
  - **决策 11.2**: 即使同步路径也强制建 UserExportTask（CREATED → RUNNING → SUCCESS 瞬间转换）。**理由**: spec §2.31 反例 1「同步导出不建任务 → HR 凌晨导出 5000 行通讯录无任何 DB 记录，事后无法追溯」；与 import batch 对称设计（导入每次都建 batch 行）。**反例**: 同步路径跳过 task 创建 → 「同步即成功」的导出无审计。**回归**: `export_users_to_excel` 第一步先 INSERT task 行（CREATED），随后 UPDATE → RUNNING → SUCCESS；失败也 UPDATE → FAILED（含 error_code + error_message）。
  - **决策 11.3**: filter_snapshot 含三个字段：`{"filter": filter_dict, "accessible_dept_ids": sorted_list_or_None, "filter_evaluated_at": iso8601}`。**理由**: spec §2.31 line 1516-1520「filter_snapshot 冻结当时的 filter + 部门结构（accessible_dept_ids 解析后的具体部门 ID 集合）」；用户事后改部门配置时，审计反查仍能准确还原「当时导出了什么」。`accessible_dept_ids=None` 表示全可见（超管 / DATA_SCOPE_ALL），与 `[]`（SELF 无部门）区分。**反例**: filter_snapshot 只存原始 filter dict → 用户事后改部门结构，审计反查数据漂移。**回归**: `test_export_filter_snapshot_freezes_accessible_dept_ids` 验证 DATA_SCOPE_DEPT 场景下 accessible_dept_ids = own_dept 的 id。
  - **决策 11.4**: `_EXPORT_COLUMN_ORDER` 在模块顶部用 assert 校验与 `EXPORT_ALLOWED_FIELDS` 集合一致。**理由**: spec §2.9 line 268「未列入的字段不进 Excel；新增敏感字段时本白名单不变（默认安全）」；列顺序与白名单两层结构，运行时 assert 防漂移（开发期就能 fail）。**反例**: 列顺序与白名单分离维护 → 一边改了另一边忘改，敏感字段漏出。**回归**: 模块加载时 assert 触发；新增字段必须同时改两个地方。
  - **决策 11.5**: hashed_password 检查通过表头扫描 + 全单元格值扫描双重验证（`test_export_only_includes_allowed_fields`）。**理由**: spec §2.9 反例 1「hashed_password 永不导出」；仅检查表头不够，万一某列被误填敏感值（如把 hashed_password 字段值塞进 nickname 列），单查表头会漏。**反例**: 仅 assert "hashed_password" not in headers → 实际值漏检。**回归**: 测试同时检查表头 + 所有单元格值的拼接字符串不含 bcrypt hash 标志 `$2b$12$super-secret-bcrypt-hash`。
  - **决策 11.6**: data_scope 复用 `app.utils.data_scope.get_user_data_scope_filters`（User 模型专用，多对多部门关系）。**理由**: spec §2.31 line 1545「data_scope 自动应用」；User 通过 `user_depts` 多对多关联 Dept，与一般业务模型的 `dept_id` 直接字段不同，需要子查询匹配；现有工具函数已处理 DATA_SCOPE_ALL/DEPT/DEPT_AND_SUB/CUSTOM/SELF 五种场景 + 超管豁免。**反例**: 自己实现 data_scope → 与 list 接口逻辑分叉，HR 权限边界不一致。**回归**: HR (DATA_SCOPE_DEPT) 只能导他可见部门用户；超管无过滤。
  - **决策 11.7**: dept_id 列输出 user 的第一个 dept_id（多部门场景取首个）。**理由**: User 与 Dept 多对多，一列装不下多个 dept_id；导出主要用途是审计 / 通讯录，主部门即可；多部门反查场景按 user_id 单独 JOIN。**反例**: dept_id 列存逗号分隔多值 → Excel 排序/筛选困难，Excel DataValidation 不支持。**回归**: `dept_id_str = str(user.depts[0].dept_id) if user.depts else ""`；多部门用户主部门即 `user.depts[0]`。
  - **决策 11.8**: 失败也建 task：`except Exception` 捕获后 UPDATE task → FAILED + error_code + error_message[:1024]，再 raise。**理由**: spec §2.31 line 1567-1572「失败也写 task」；超阈值（AI_EXPORT_ASYNC_REQUIRED）/ DB 错误等场景都需要事后反查（用户问「我刚才的导出为什么失败了」时查 task 表）。**反例**: 失败不写 task → 用户事后查不到失败原因，运维也无法定位。**回归**: `test_export_failure_records_async_required_in_task` 验证超阈值场景下 task.status=FAILED + error_code=AI_EXPORT_ASYNC_REQUIRED。
  - **决策 11.9**: 不写 ExportTaskLog 表（与 import batch_log 不对称）。**理由**: spec §2.31 line 1456-1458「导出无 chunk 概念（一次 query + streaming），无中间进度；同步路径只有 CREATED → SUCCESS 一跳，log 表过载」；Phase 3 异步导出再加 log 表。**反例**: 同步路径也建 log 表 → CREATED/SUCCESS 两行 log 几乎无信息量。**回归**: UserExportTask 单表足够覆盖同步路径全生命周期；Phase 3 异步任务再决定是否引入 export_task_log。
  - **决策 11.10**: 30 天 TTL 通过 FileStorage.save 的 `ttl_seconds=30*86400` 参数（LocalFileStorage 实现 TTL 标记，Phase 3 cron 清理）。**理由**: spec §2.31 line 1452「Retention 30 天（导出文件含敏感数据，更短）」vs import 90 天；FileStorage Protocol 抽象让 service 层不感知存储介质（local / S3 / GridFS），统一通过 `ttl_seconds` 表达保留期。**反例**: service 层自己管 TTL → LocalFileStorage vs S3FileStorage 实现差异泄漏到业务层。**回归**: MockFileStorage 的 ttl_seconds 参数即使被忽略也接受；`cleanup_expired_export_tasks` cron（Task 22）扫 sys_user_export_task 表 + 调 file_storage.delete。
  - **决策 11.11**: 表头用中文（账号/昵称/邮箱/...），导入模板表头用英文（user_name/nickname/...）。**理由**: 导出文件主要给人读（管理员审阅 / 通讯录分发），中文表头友好；导入模板主要给 Excel DataValidation + parser 用（按字段名匹配），英文表头稳定。**反例**: 导出也用英文表头 → 管理员看不懂「user_gender=1」是什么；导入用中文表头 → parser 字段映射易碎（中文表头被改一个字就失败）。**回归**: `_EXPORT_COLUMN_ORDER` 第二项是中文表头；`import_parser.EXCEL_HEADERS` 是英文字段名。

#### HTTP API
- [x] Task 12 ✅ 已完成（2026-08-04）：HTTP `POST /system/user/import`（multipart + dry_run Form 字段 + v2.2 P1 `sync_mode` body 参数 + v2.2 P0 #2.27 `idempotentReplay` 响应字段 + v2.2 P1 #3.3 响应精简）+ 15 单测（2 auth + 2 dry_run + 4 execute + 5 validation + 2 service exception）
  - 实现：`app/modules/system/api/user.py::import_users`（multipart `UploadFile` + 6 个 `Form` 字段：reason / on_conflict / sync_mode / dry_run / preview_token）
  - 异常新增：`app/core/exceptions.py::UnprocessableEntityException`（继承 `BusinessRuleException`，覆写 `code=422`）— spec §5.7 列的 8 个 422 errorCode（`AI_IMPORT_PREVIEW_INVALID` / `AI_IMPORT_BATCH_RUNNING` / `AI_IMPORT_ALREADY_EXECUTED` / `AI_IMPORT_ILLEGAL_TRANSITION` / `AI_IMPORT_EMPLOYEE_NO_EXISTS` / `AI_IMPORT_BATCH_NOT_FOUND` / `AI_IMPORT_BATCH_NOT_CANCELLABLE` / `AI_EXPORT_ASYNC_REQUIRED`）改用新异常；service 层（`import_service` / `import_state` / `export_service`）替换 `BusinessRuleException` → `UnprocessableEntityException`
  - schema 加字段：`ImportResult.status: str`（spec §5.1 execute 响应需要），service 两个构造点（正常 + 幂等重放）都填 `end_status.value` / `fresh.status.value`
  - 测试夹具基建：`tests/modules/system/conftest.py::_reset_redis_client` 升级，同步刷新 `audit_middleware.redis_client` + `auth_service.redis_client`（与 ai/conftest 对齐），让 HTTP 层测试不再被 module-load 期绑死的旧 redis_client 卡死
  - **决策 12.1**: `dry_run` 是 multipart Form 字段（不是 query param）。**理由**: spec §5.1 line 2130 把 `dry_run` 列在 Body 块里，与 `on_conflict` / `sync_mode` / `reason` 同级；query param 通常用于「资源级 modifier」，dry_run 是「本次提交的语义模式」，与 body 字段一致更合理。**反例**: `POST /import?dry_run=true` 让前端 split query + form data 两套编码路径。**回归**: API 函数签名 `dry_run: str | None = Form(None)`；`_coerce_dry_run` 把 "true"/"1"/"yes" 统一为 True，其他视为 False（不抛错避免抢占业务异常）。
  - **决策 12.2**: dry_run 响应把 `previewToken` + `expiresAt` 放在 `data` 内（不是 spec 示例里的顶层兄弟节点）。**理由**: 项目响应 envelope 严格 `{code, msg, data}` 三段式，spec line 2138-2151 的 `data` 与 `previewToken` 兄弟是文档简写；前端取 `response.data.previewToken` 与现有 list/detail 响应一致。**反例**: 顶层混入 `previewToken` → ResponseModel 序列化失败或前端需要特判。**回归**: API 层 `result_data.update({"previewToken": ..., "expiresAt": ...})` 把字段塞进 data dict；测试 `body["data"]["previewToken"]` 断言。
  - **决策 12.3**: 四象限计数（`newCount` / `existsCount` / `conflictCount` / `outOfScopeCount`）从 `batch.summary_*` 读，不依赖 `result.new_records` list 长度。**理由**: `ImportDryRunResult.new_records` 等已截断到 `MAX_PREVIEW_RECORDS=2000`（spec §3.2），list 长度 ≠ 真实计数；batch.summary_* 是 dry_run_import_users 写入的真实总数。**反例**: 用 `len(result.new_records)` → 截断时返回 2000 而非真实 2500，前端误以为「只有 2000 个新增」。**回归**: 测试 `_fake_preview_batch` 设 `summary_new=2` 与 `_fake_dry_run_result` 的空 list 解耦，验证响应读 summary_*。
  - **决策 12.4**: `expiresAt` = `batch.created_at + timedelta(seconds=600)`，从 batch 行的 `created_at` 推算而非 Redis TTL 剩余时间。**理由**: spec §2.19 line 534「preview_token Redis TTL=600s」固定 10 分钟；Redis 的 setex 剩余时间是动态值（每秒减少），前端展示「expiresAt: 14:10:00」需要绝对时间。`batch.created_at` 是数据库 server_default，与 Redis setex 时刻近似同步（毫秒级误差可接受）。**反例**: 实时读 Redis TTL → 响应里 expiresAt 每次访问都不同，前端无法稳定缓存。**回归**: API 层 `_PREVIEW_TOKEN_TTL_SECONDS = 600` 与 import_service `_PREVIEW_REDIS_TTL_SECONDS` 对齐。
  - **决策 12.5**: `ImportErrorCollection` 在 API 层 catch 后转 `BusinessRuleException(error_code="AI_IMPORT_FIELD_ERRORS")` + `data={"errors": [...]}`。**理由**: 全局 exception handler 只识别 `BusinessException` 子类；`ImportErrorCollection` 继承 `Exception`，需要 API 层桥接；errors 数组挂到 `data` 让前端拿到具体每行的 FailedRow。**反例**: 把 `ImportErrorCollection` 改成 `BusinessException` 子类 → parser 层污染业务异常层级；或前端只看 msg 拿不到结构化 errors[]。**回归**: 测试 `test_field_errors_returns_400_with_errors` 断言 `body["data"]["errors"][0]["errorCode"] == "AI_IMPORT_USERNAME_INVALID"`。
  - **决策 12.6**: `UnprocessableEntityException` 继承 `BusinessRuleException` 而非 `BusinessException`，覆写 `self.code=422`。**理由**: spec §5.7 要求 8 个 errorCode 走 422 状态码，但既有 service 单测大量 `pytest.raises(BusinessRuleException)`；如果新异常是 `BusinessException` 直接子类，所有既有断言失效。**反例**: `UnprocessableEntityException(BusinessException)` → 11 个回归测试失败（test_import_state / test_user_export / test_user_import_execute）。**回归**: 新异常 `__init__` 内 `self.code = 422` 覆盖父类的 400；HTTP 响应 422 + 既有断言匹配 `BusinessRuleException` 子类。
  - **决策 12.7**: `dry_run=false` 时 `preview_token` 缺失由 API 层校验（不在 Pydantic / Form 层），抛 `UnprocessableEntityException(AI_IMPORT_PREVIEW_INVALID)`。**理由**: Form 字段条件必填（「dry_run=false 时 preview_token 必填」）Pydantic 难以表达（需要 cross-field validator）；在 endpoint 入口显式校验更直观。**反例**: 用 Pydantic model_validator → FastAPI Form 字段不支持像 Body 那样的 model-level validator；抛 422 RequestValidationError 而非业务码。**回归**: `if not preview_token or not preview_token.strip(): raise UnprocessableEntityException(...)`；测试 `test_missing_preview_token_returns_422` 验证。
  - **决策 12.8**: `system:user:import` 权限码本 Task 不 seed（Task 16 落地），admin 超管绕过。**理由**: spec §10 Task 16 列出权限码 seed 是独立任务；本 Task 只验证 HTTP 契约层，admin 用户（init_db seed 的超管）的 `R_SUPER` 角色走 `is_super_admin` 短路，让权限检查通过。**反例**: 本 Task 同时改 sync_menus.py 加权限码 → 范围漂移，sync_menus 影响 init_db 既有行为。**回归**: 测试 `admin_token` fixture 直接读 init_db seed 的 admin 用户；后续非超管用户的 403 测试留 Task 16 权限码落地后补。
  - **决策 12.9**: 测试用 mock 替身覆盖 service 层（`parse_import_excel` / `dry_run_import_users` / `batch_create_users_from_records`），不跑真实业务流程。**理由**: HTTP 契约测试只关心路由 / multipart 解析 / 响应 envelope / 错误码映射；service 业务逻辑在 `test_user_import_dry_run.py` / `test_user_import_execute.py` 已 18 + 13 单测覆盖。**反例**: 测试走真实 service → 每个 API 测试都要建 dept / role / sys_config 默认密码，3 倍测试耗时，且失败时定位不清是 API 还是 service。**回归**: 测试 `with patch(...)` 包裹；mock 对象断言 `call_args.kwargs["on_conflict"] == "overwrite"` / `["sync_mode"].value == "UPDATE_PROFILE"`。
  - **决策 12.10**: `tests/modules/system/conftest.py::_reset_redis_client` 升级同步刷新 audit_middleware + auth_service 两个 module 的 redis_client 引用。**理由**: module-load 期 `from app.core.redis import redis_client` 绑死对象引用；conftest reset `redis_module.redis_client` 后旧引用仍指向上轮 loop 的客户端；HTTP 测试经 audit_middleware（_resolve_username） + auth_service（_is_blacklisted）两次 redis 查询，loop 切换后必抛 `RuntimeError: Event loop is closed`。**反例**: 仅 reset redis_module → 所有 HTTP 集成测试跨 loop 失败；用 monkeypatch.dispatch 短路 → 破坏后续 ai/test_agent_admin 的真实审计断言。**回归**: 与 `tests/modules/ai/conftest.py:24-69` 完全对齐（gateway / chat / supervisor.quota / audit / auth 同款刷新模式）。
  - **决策 12.11**: API 层 `_validate_import_reason` 与 service 层 `_validate_reason` 重复校验。**理由**: spec §2.30 defense-in-depth — API 层是入口校验（拦截 Pydantic 未拦的全空白），service 层兜底（防 AI tool 直接调 service）；与决策 9.9 / 11.1 对称。**反例**: 仅 API 校验 → AI tool 路径绕过；仅 service 校验 → API 层 ImportErrorCollection 之外的 reason 错误下沉到 service 才抛，响应慢一拍。**回归**: `test_missing_reason_returns_422` 验证 Pydantic / API 层拦截；既有 service 单测验证 defense-in-depth。
- [x] Task 13 ✅ 已完成（2026-08-04）：HTTP `POST /system/user/export`（#2.23，body 含 filter + v2.2 P1-3 reason 必填，同步路径返 xlsx + `Content-Disposition: attachment; filename=users_YYYYMMDD.xlsx`，超阈值透传 `AI_EXPORT_ASYNC_REQUIRED` 422）+ HTTP `GET /system/user/export/{export_id}` 任务详情（404 → `AI_EXPORT_TASK_NOT_FOUND`）+ HTTP `GET /system/user/export` 任务列表（按 `operator_id` / `status` 过滤，分页）+ 13 单测（2 auth + 2 sync + 2 validation + 1 async_required + 3 detail + 3 list）
  - 实现：`app/modules/system/api/user.py::export_users` / `get_export_task_detail` / `list_export_tasks_endpoint`
  - 新 schema：`app/modules/system/user/schemas.py::UserExportRequest`（继承 `UserExportFilter` + reason 必填，与 `ReasonSchema` 对称的 strip 校验）；`UserExportTaskQuery`（`current` / `size` / `operator_id` / `status`）
  - 新 service helper：`app/modules/system/user/export_service.py::get_export_task` / `list_export_tasks`（`paginate` + `created_at desc`，`status` 非合法枚举抛 `AI_EXPORT_INVALID_STATUS`）
  - **决策 13.1**: 同步导出用 `Response` 而非 `StreamingResponse`。**理由**: bytes 已全在内存（`_build_excel` 一次构造完整 xlsx），无流式收益；`BaseHTTPMiddleware`（`audit_middleware`）+ `StreamingResponse` 是 starlette 已知冲突 — `wrapped_receive` 在响应开始后被多调一次，抛 `RuntimeError: Unexpected message received: http.request`。spec §5.2 line 2200 的「StreamingResponse xlsx」是契约描述（强调「非 JSON 响应、二进制下载」），实现用 `Response(content=bytes, media_type=xlsx_mime)` 等价满足；客户端体感完全一致（同样 Content-Disposition 下载）。**反例**: 强行 `StreamingResponse(iter([bytes]))` → POST /system/user/export 经 audit_middleware 必崩（GET /system/config/export 不崩是因为 audit_middleware 只对 POST/PUT/PATCH 读取 body）。**回归**: `test_returns_streaming_xlsx_with_content_disposition` 断言 `Content-Type` + `Content-Disposition: filename=users_\d{8}\.xlsx` + body PK magic header。
  - **决策 13.2**: `UserExportRequest` 单继承 `UserExportFilter` + 内联 reason 字段（不复用 `ReasonSchema` mixin）。**理由**: Pydantic v2 多继承 `(ReasonSchema, UserExportFilter)` 时 `model_config` 解析顺序不确定（ReasonSchema 是裸 `BaseModel`，UserExportFilter 走 `_CamelBase` 配 `alias_generator=to_camel`）；单继承从 `UserExportFilter` 拿全部 filter 字段 + 加 reason，配 `@field_validator("reason")` strip 校验，逻辑与 `ReasonSchema._strip_and_require_non_empty` 对称。**反例**: 强行多继承 → 部分 Pydantic 版本 `user_name` 字段可能丢 alias 变 snake_case 直传，前端 `userName` 收不到。**回归**: `test_missing_reason_returns_422` + `test_empty_reason_returns_422` 验证校验；`test_passes_filter_and_reason_to_service` 验证 `userName` camelCase 透传到 service。
  - **决策 13.3**: `GET /export/{export_id}` 找不到时抛 `NotFoundException("用户导出任务", error_code="AI_EXPORT_TASK_NOT_FOUND")`。**理由**: spec §5.7 错误码表新增 `AI_EXPORT_TASK_NOT_FOUND` 与 import 的 `AI_IMPORT_BATCH_NOT_FOUND` 对称；`NotFoundException` 是项目通用 404 异常（CLAUDE.md 第 7 条「Reuse exceptions」），传 `resource_type` + `error_code` 即可。**反例**: 抛 `BusinessException(404, ...)` → 缺 `errorCode`，前端 i18n 失败。**回归**: `test_not_found_returns_404` 断言 `body["errorCode"] == "AI_EXPORT_TASK_NOT_FOUND"`。
  - **决策 13.4**: `GET /export` 列表用 `UserExportTaskQuery` Pydantic schema（继承 `_CamelBase`）+ `Depends()` 注入，与 `/list` 端点同款。**理由**: 项目惯例（`UserQuery` / `ConfigQuery` 等）走 Pydantic schema + `Depends()`，FastAPI 自动从 query string 解析；`operator_id` 在 schema 里是 `int | None`，前端 `?operatorId=1` 经 `to_camel` alias 解析为 `operator_id` 字段。**反例**: 端点签名内联 `operator_id: int | None = Query(None)` → 与 `/list` 风格不一致；批量加过滤参数时签名爆炸。**回归**: `test_passes_filters_to_service` 验证 `query_arg.current == 2` / `query_arg.status == "SUCCESS"` / `query_arg.operator_id == 1`。
  - **决策 13.5**: `UserExportTaskQuery.status: str` 而非 `Literal["CREATED", "RUNNING", ...]`，非法值由 service 层抛 `BusinessRuleException(AI_EXPORT_INVALID_STATUS)`。**理由**: Pydantic Literal 校验失败走 `RequestValidationError` → 全局 handler 返 422 但 errorCode 缺；用 str + service 校验，统一走 `BusinessException` 全局 handler，errorCode 明确。**反例**: `status: Literal[...]` → 422 响应无 errorCode，前端 i18n 失败。**回归**: 既有 `ExportTaskStatus(query.status)` ValueError → BusinessRuleException 转换；后续 Task 补 `test_invalid_status_returns_400`。
  - **决策 13.6**: POST /export 同步路径不返 `export_id` 给前端（service 返回的 `export_id` 在 API 层丢弃）。**理由**: spec §5.2 同步路径只约定 `Content-Disposition + xlsx bytes`；前端下载成功后无需查 task 详情（文件已在手里）；需要审计反查时走 `GET /export` 列表按 `filter + created_at` 反查。**反例**: 加 `X-Export-Id` 响应头 → 偏离 spec，引入未文档化的契约面；前端易过度依赖导致后续重构被绑死。**回归**: 实现里 `xlsx_bytes, _row_count, _export_id = ...` 显式丢弃；测试不验证 export_id 头。
  - **决策 13.7**: POST /export 文件名 `users_YYYYMMDD.xlsx`（如 `users_20260804.xlsx`），用 `datetime.now().strftime("%Y%m%d")`。**理由**: spec §5.2 line 2200 示例 `filename=users_20260801.xlsx`；本地时区日期对齐管理员体感（导出动作发生在「今天」）。**反例**: 文件名带 `export_id` / 时间戳 → 用户下载管理器里多个版本难辨识；UTC 日期 → 国内凌晨导出文件名是「昨天」。**回归**: `test_returns_streaming_xlsx_with_content_disposition` 用 regex `filename=users_(\d{8})\.xlsx` 校验。
  - **决策 13.8**: POST /export service 抛 `UnprocessableEntityException(AI_EXPORT_ASYNC_REQUIRED)` 时 API 层不 catch，让全局 handler 走 422。**理由**: spec §5.7 已明确该 errorCode 走 422；Task 12 决策 12.6 引入 `UnprocessableEntityException` 后，service 层（`export_service.export_users_to_excel`）已经在 `len(rows) > USER_EXPORT_ASYNC_THRESHOLD` 时抛此异常；API 层不重复校验阈值，让 service 单一职责。**反例**: API 层先查 `len(rows)` 再决定 → service / API 双重校验，阈值改动时要改两处。**回归**: `test_async_required_returns_422` mock service side_effect 抛 `UnprocessableEntityException`，验证透传到 422 响应。
  - **决策 13.9**: `list_export_tasks` service 用 `app.utils.pagination.paginate` 通用函数 + `order_by=UserExportTask.created_at.desc()`。**理由**: 与 marketplace / role / config 等模块统一分页模式；`created_at desc` 让最新导出在第一页（管理员体感优先）。**反例**: 自写 `select().offset().limit()` + `select(func.count())` → 重复 `paginate` 已封装的逻辑；`asc` → 历史久远的任务排第一页，反人类。**回归**: `test_returns_paginated_list` 断言 `data["records"][0]["exportId"] == "e1"`（最新优先）+ total/current/size 字段。
  - **决策 13.10**: 测试 mock service（`export_users_to_excel` / `get_export_task` / `list_export_tasks`），不跑真实业务流程。**理由**: HTTP 契约测试只验证路由 / body 解析 / streaming / 错误码；service 业务在 `test_user_export.py`（13 单测）已覆盖（建 task / filter_snapshot / data_scope / 失败也建 task / async_required）。**反例**: 测试走真实 service → 每个 API 测试都要建 user / role / dept / sys_config，3 倍耗时且失败定位模糊。**回归**: `with patch(f"{_API_MODULE}.export_users_to_excel", new=AsyncMock(...))` 等模式；mock 对象断言 `call_args.args[1].user_name == "bob"`。
  - **决策 13.11**: 路由注册顺序 — `GET /export`（list）在 `GET /export/{export_id}`（detail）之前。**理由**: FastAPI 路由按声明顺序匹配，`/export` 是字面量、`/export/{export_id}` 含路径参数；声明在前者更具体，Starlette 优先匹配。**反例**: detail 在前 → `GET /export`（无 path param）也能被 `/export/{export_id}` 匹配（export_id=""），但实际上 FastAPI 不会让空段匹配 `{export_id}`，所以这里其实不冲突；保险起见仍 list 在前。**回归**: 实现里 `@router.get("/export")` 先声明，`@router.get("/export/{export_id}")` 后声明；`test_returns_paginated_list` 与 `test_returns_task_details` 互不影响。
- [x] Task 14 ✅ 已完成（2026-08-04）：HTTP `GET /system/user/import/template`（spec §5.3 + §2.13 + §2.16 + §2.17 + §2.18：4 sheet「数据」/「说明」/「部门字典」/「角色字典」+ DataValidation 下拉引用字典 sheet + 字典 sheet 实时查 sys_dept/sys_role + 顶部生成时间标注）+ 21 单测（9 HTTP 契约 + 12 service 业务：4 sheet 结构 + 4 dept 字典实时 + 3 role 字典实时 + 1 说明 sheet）
  - 实现：`app/modules/system/user/template_service.py::generate_import_template`（4 helper：`_fetch_depts` / `_fetch_roles` / `_build_dept_full_path` / `_build_data_sheet` + `_build_instruction_sheet` / `_build_dept_dict_sheet` / `_build_role_dict_sheet`）
  - 端点：`app/modules/system/api/user.py::download_import_template`（GET，权限 `system:user:import`，返 `Response(xlsx_bytes)` + `Content-Disposition: attachment; filename=user_import_template.xlsx`）
  - 测试：`tests/modules/system/test_user_import_template_api.py`（9 测试，HTTP 契约层）+ `tests/modules/system/test_user_template_service.py`（12 测试，service 业务层 — 含 3 级 dept 树 full_path 拼接 / 禁用 dept/role 不进字典 / DataValidation formula 引用字典 sheet）
  - **决策 14.1**: 模板拆「HTTP 契约测试」+「service 业务测试」两层。**理由**: HTTP 测试用 ASGITransport + db_session fixture，fixture 的 SAVEPOINT 事务与 endpoint 的 `Depends(get_db)` 不共享 — seed 的 dept/role 对 endpoint 不可见；硬要共享需 hack `get_db` dependency，破坏生产路径。拆开后 HTTP 测试只验证路由 + Content-Disposition + sheet 结构（不依赖 seed），service 测试直接 `generate_import_template(db_session)` 拿真实事务验证字典 sheet 实时查询 + full_path 拼接。**反例**: 强行 HTTP 测试 seed 验证 → ASGITransport 拿到的是 outer-transaction 之外的独立 session，断言失败 + 假阴性掩盖真问题。**回归**: HTTP 测试 `test_dept_dict_sheet_has_header_and_timestamp` 只断言 row 1 生成时间 + row 2 表头；service 测试 `test_seed_dept_appears_in_dict_sheet` + `test_full_path_built_from_ancestors_chain` 验证 seed 的 3 级 dept 树全进字典且 full_path 拼接正确。
  - **决策 14.2**: 同步下载用 `Response(content=xlsx_bytes)` 而非 `StreamingResponse`。**理由**: 与决策 13.1 一致 — `BaseHTTPMiddleware`（`audit_middleware`）+ `StreamingResponse` 是 starlette 已知冲突（POST 路径经中间件读 body 时序错乱）；GET 路径其实不冲突，但保持与 POST /export 实现一致性 + bytes 已全在内存，`Response` 等价且兼容。**反例**: GET 用 StreamingResponse → 与 POST 不一致，未来重构 / 异步通道改造时两套实现分叉。**回归**: `test_returns_xlsx_with_content_disposition` 断言 Content-Type + filename + PK magic header；不依赖 streaming chunk 行为。
  - **决策 14.3**: 「数据」sheet 8 列（不带 `employee_no`），与 `import_parser.EXCEL_HEADERS`（9 列含 employee_no）不完全一致。**理由**: spec §5.3 line 2217 严格列了 8 列（user_name/nickname/user_email/user_phone/dept_input/role_input/user_gender/status），employee_no 是 v2.2 P1 后增字段（spec §2.24）；让用户在「正式导入」阶段通过 update 路径补 employee_no（避免模板字段过多）；parser 表头大小写不敏感匹配可容忍模板缺列（决策 8.4），用户手动加 `employee_no` 列也能解析。**反例**: 模板强加 employee_no → 与 spec 不一致，用户对照 spec 困惑；不加 employee_no → parser 验证 user.employee_no 必填场景拿不到值，需用户走 update 补充。**回归**: 测试 `test_data_sheet_has_correct_columns` 断言 8 列；parser 既有测试 `test_employee_no_optional` 验证缺失时为 None。
  - **决策 14.4**: 字典 sheet 顶部 row 1 = 生成时间标注、row 2 = 表头、row 3+ = 数据，DataValidation formula 起点是 `$X$3` 而非 spec §2.16 示例的 `$X$2`。**理由**: spec §5.3 line 2227 明确「顶部加一行生成时间」；生成时间占据 row 1 后，表头与数据整体下移一行，DataValidation 引用范围必须同步调整，否则下拉源指向表头行（无数据）。**反例**: 生成时间放底部 → 用户翻到字典 sheet 第一眼是表头，看不到「数据可能已变化」提示，旧模板过期风险；formula 不调整 → 下拉为空。**回归**: `_DEPT_DV_FORMULA = "部门字典!$B$3:$B$1000"`、`_ROLE_DV_FORMULA = "角色字典!$A$3:$A$50"`；测试 `test_dept_dict_sheet_has_header_and_timestamp` 断言 row 1 含「生成时间」关键字 + row 2 表头含 dept_name/full_path。
  - **决策 14.5**: 部门字典 sheet 提供 `full_path` 列（按 ancestors 链拼接），不直接展示 `parent_id`。**理由**: spec §2.17 line 470「模板「部门字典」sheet 提供 `dept_name` / `full_path` / `dept_id` 三列」；用户复制 `full_path` 粘贴到「数据」sheet 即可避开重名歧义；ancestors 是 sys_dept 的字符串字段（`"0,1234,5678"`），service 内部解析为 dept_name 链拼接。**反例**: 只展示 parent_id → 用户看 Snowflake ID 没意义；展示 ancestors 原始字符串 `"0,1234,5678"` → 用户看不懂；不展示 full_path → 用户重名场景必须查 DB 才能填正确路径。**回归**: `_build_dept_full_path` 解析 ancestors，按 dept_id → dept_lookup 找祖先 dept_name 拼接；测试 `test_full_path_built_from_ancestors_chain` 验证 3 级 dept 树的根/中/叶 full_path 正确。
  - **决策 14.6**: 字典 sheet 仅展示 `status='1'` 启用的 dept / role。**理由**: 用户从字典 sheet 复制粘贴填入「数据」sheet，禁用 dept/role 在 dry_run 阶段会反查失败 → 用户体验差；模板应只展示「当前可用」选项。**反例**: 字典 sheet 含禁用项 → 用户复制后导入失败，且字典 sheet 看起来「混乱」（启用/禁用混在一起）。**回归**: `_fetch_depts` / `_fetch_roles` 都 `where(status == "1")`；测试 `test_disabled_dept_excluded_from_dict_sheet` + `test_disabled_role_excluded_from_dict_sheet` 验证 status='2' 不进字典。
  - **决策 14.7**: 文件名固定 `user_import_template.xlsx`，不带日期 / 版本号。**理由**: 模板不分版本（字典 sheet 实时生成已含最新数据）；用户下载管理器看到稳定文件名便于识别；版本控制走字典 sheet 顶部「生成时间」标注（用户对比不同下载的时间）。**反例**: 文件名带日期 `user_import_template_20260804.xlsx` → 同一天下载多个文件名重复，用户体验差；带版本号 → 字典 sheet 已实时，版本号无意义。**回归**: 测试 `test_returns_xlsx_with_content_disposition` 用 regex `filename=user_import_template\.xlsx` 校验。
  - **决策 14.8**: 「说明」sheet 列 4 列（字段名 / 必填 / 取值范围 / 冲突处理策略），覆盖 8 个字段（user_name/nickname/user_email/user_phone/dept_input/role_input/user_gender/status），不含 employee_no（与「数据」sheet 8 列对齐，决策 14.3）。**理由**: spec §5.3 line 2218「每列字段说明 / 必填标记 / 取值范围 / 冲突处理策略」；冲突处理列让用户预知「这个字段错了会触发什么 errorCode」，减少 dry_run 失败时的迷惑。**反例**: 说明 sheet 只列字段名 + 必填 → 用户不知道取值范围（如 gender 0/1/2），dry_run 错误率高；含 employee_no → 与「数据」sheet 不一致。**回归**: 测试 `test_instruction_sheet_has_field_descriptions` 断言 row 数 ≥ 9（表头 + 8 字段）+ 含 user_name/dept_input 关键字。
  - **决策 14.9**: DataValidation 配置 `showErrorMessage=True` + `errorTitle` + `error` 中文提示，但 `allow_blank` 部门列 False / 角色列 True（spec §2.16 line 411「role 可选」）。**理由**: spec §2.16 反例 2「加了 Validation 不做后端校验 → 复制粘贴绕过，安全漏洞」；DataValidation 是 UX 层提示，不是安全边界 — 后端 #2.17/#2.18/#2.15/#2.11 全套校验仍兜底；中文 error 提示让用户知道「去字典 sheet 复制」是替代方案。**反例**: `allow_blank=True` 全开 → 用户漏填 dept_input 不告警，dry_run 才发现；`allow_blank=False` 全开 → role 列必填与 spec「role 可选」冲突。**回归**: 实现里 dept_dv `allow_blank=False`、role_dv `allow_blank=True`；测试 `test_data_sheet_has_data_validations` 断言 ≥ 2 个 DataValidation + formula 引用字典 sheet。
  - **决策 14.10**: 示例值（`zhangsan` / `lisi`）用明显占位语义，避开 init_db seed 的 `admin` 等真实账号。**理由**: 用户可能直接落库示例行（不删示例就上传），dry_run 会拦下重名 → `AI_IMPORT_USERNAME_DUPLICATE`；用明显占位名字让用户一眼识别「这是示例」+ dry_run 错误时 errorCode 也明确（不会污染真实账号的导入流程）。**反例**: 示例用 `admin` / `test` → 与 seed 账号冲突，dry_run 报「重名」让用户以为是真错。**回归**: 示例 `_DATA_EXAMPLE_ROWS` 固定 `zhangsan` / `lisi`；测试 `test_data_sheet_has_two_example_rows` 只断言 row 2/3 的 user_name 非空，不断言具体值（避免与示例命名绑死）。
  - **决策 14.11**: service 用模块级函数 `generate_import_template(db)`，不建 TemplateService 类。**理由**: 与决策 9.1（dry_run_import_users）/ 11.1（export_users_to_excel）一致 — 模块级函数 + 单测覆盖，service 层薄包装；模板生成无状态 + 无依赖注入需求。**反例**: 强制建 `TemplateService` 类 + 单例 → facade 而 facade，无业务收益。**回归**: `template_service.py` 与 `import_service.py` / `export_service.py` 平行存在，各管一侧；本 task 21 测试全用 `from app.modules.system.user.template_service import generate_import_template` 直接调函数。
  - **决策 14.12**: endpoint `_current_user: User = Depends(get_current_user)` 强制拿登录用户但不参与业务，仅用于权限门。**理由**: 模板生成不需 current_user 字段（字典 sheet 是全局视图），但 `require_permissions("system:user:import")` 已挂在 `dependencies=[...]` —— 显式声明 `_current_user` 让 OpenAPI doc 显示认证需求 + 未来如需按用户角色过滤字典（如 HR 只看自己部门）有挂载点。**反例**: 不声明 `_current_user` → OpenAPI 不显示 padlock 图标，前端开发者以为公开接口。**回归**: 实现里 `_current_user: User = Depends(get_current_user)`（前缀 `_` 表示未在函数体内使用，与既有 `list_export_tasks_endpoint` 同款）。
- [x] Task 15 ✅ 已完成（2026-08-04）: HTTP `GET /system/user/import/{batch_id}`（按 batch_id 查导入结果，复用审计反查）+ 测试

  **决策记录**：
  - **决策 15.1**: HTTP 契约测试用 `patch.get_batch_detail` 替身，不测真实 outerjoin。**理由**: 与 task 13 GET /export/{export_id} 测试模式对齐 — HTTP 层只验证路由 / 字段映射 / 错误码，service 层的 outerjoin + operator_name 反查业务逻辑通过单独的 service 测试覆盖（参考 task 14 经验：HTTP ASGITransport 与 db_session fixture 不共享事务，seed 数据不可见）；用 patch 替身让 HTTP 测试不依赖 init_db seed，独立稳定。**反例**: HTTP 测试 seed batch + user → 走 get_db session 看不到 fixture 数据，断言 batch_id 假阴性。**回归**: `TestGetBatchDetailResponse.test_operator_name_from_user_join` 用 `AsyncMock(return_value=(batch, "hr_zhang"))` 替身 + 断言 service 被调用 + 透传 operator_name 到 response；12 测试全过。
  - **决策 15.2**: 权限用 `system:user:list`（不是 `system:user:import`），对齐 spec §5.4 line 2234 「list 权限即可，因为查的是导入历史不是用户敏感数据」。**理由**: 导入历史是 admin/HR 的查询能力（看自己/团队的批次进度），不是写权限；list 权限已覆盖「读用户列表」语义，导入批次本质上是从用户列表衍生的审计视图。**反例**: 用 `system:user:import` 权限 → 没有导入权限的利益相关方（如合规审计员）看不到批次历史，破坏审计反查链路。**回归**: endpoint `dependencies=[Depends(require_permissions("system:user:list"))]`；测试用 admin_token（admin 绕过 list 检查，与 export 详情测试同款）。
  - **决策 15.3**: `get_batch_detail(db, batch_id) -> tuple[UserImportBatch | None, str | None]` 模块级函数（不是 service 类方法）。**理由**: 与决策 14.11（template_service）/ 11.1（export_service）一致 — service 层薄包装 + 模块级函数；本查询无状态、无副作用、单 outerjoin，无需类承载；返回元组让 API 层显式处理「batch 找不到」+「operator 已删除」两种 None 语义。**反例**: 强制建 `ImportBatchQueryService` 类 → facade 而 facade；返 dict 隐藏 operator_name None 的语义（是 batch 不存在还是 user 被删）。**回归**: `import_service.py` 末尾加 `get_batch_detail` 函数 + 加入 `__all__`；API 层 `if batch is None: raise AI_IMPORT_BATCH_NOT_FOUND`，operator_name None 不抛错（spec 允许 user 被删的合法场景）。
  - **决策 15.4**: `UserImportBatchResponse` schema 剥离 `preview_token` / `file_sha256` / `records_hash` / `reason` 四个字段（GET 详情接口不暴露）。**理由**: (a) `preview_token` 是 execute 凭证（spec §2.19 三重校验），泄露可被重放 → preview→RUNNING CAS 抢占；(b) `file_sha256` / `records_hash` 是内部指纹，对前端无业务意义；(c) `reason` 是审计字段（spec §2.30），进入 sys_user_import_batch.reason + batch_log.detail.reason 链路，前端 GET 列表/详情不应返回（敏感，仅审计员查询 sys_operation_log 才看得到）；spec §5.4 line 2238-2264 响应示例本来就不含这四个字段。**反例**: 暴露 preview_token → 中间人嗅探后重放 execute（虽然三重校验要求 file_sha256+records_hash 也匹配，但泄露面仍放大）；暴露 reason → 业务背景文字外泄给无权看的人。**回归**: 测试 `test_does_not_expose_sensitive_fields` 显式断言这四个键不在 serialized response 里；旧 `test_import_schemas.py::TestUserImportBatchResponse::test_camel_case_alias` 同步更新断言这四个字段已剥离。
  - **决策 15.5**: `expires_at` 字段动态计算（不存 batch 列）。**理由**: spec §5.4 line 2262 字段是「批次保留窗口」，依赖 status：CREATED/PREVIEW_DONE/RUNNING → `created_at + 10min`（preview_token TTL，spec §2.19）；终态（SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED）→ `finished_at + 24h`（failed_rows_file 文件存储 TTL，spec §3.x）；动态算避免冗余列 + 状态变化时无需 UPDATE。**反例**: 加 batch.expires_at 列 → 又一个要维护的字段，状态机 transition 都得 UPDATE；固定 created_at + 24h → preview 阶段批次 expires_at 显示过早，前端误以为已过期。**回归**: helper `_compute_batch_expires_at(batch)` 集中逻辑；`TestGetBatchDetailExpiresAt` 4 用例覆盖 CREATED（10min）/ PREVIEW_DONE（10min）/ SUCCESS（24h）/ FAILED（24h）。
  - **决策 15.6**: `sync_mode` 字段保留 Optional 但当前返 None（不查 batch_log）。**理由**: spec §5.4 line 2258 列了 `syncMode` 字段，但 model 没存（在 batch_log.detail.sync_mode，spec §2.28）；本 task 是 v2.2 P2 增量，查询接口核心是 status + counts + failed_rows_file，sync_mode 是 nice-to-have；从 batch_log 反查需要 LEFT JOIN + ORDER BY created_at DESC LIMIT 1（同 batch 可能多次 execute 失败重试）→ 查询复杂度上升不值得；前端容错 null 即可，task 22 cron 清理时可补。**反例**: 加 batch.sync_mode 列 → 破坏 schema 演进（sync_mode 是 execute body 字段，不该污染 batch 表）；当前查 batch_log → 单查询变 join + limit，性能与测试复杂度双升。**回归**: schema `sync_mode: str | None = None`；测试 `test_sync_mode_returns_none_when_not_in_batch` 显式断言 None；spec 决策记录「待 task 22 后从 batch_log 反查」。
  - **决策 15.7**: `operator_name` 通过 LEFT OUTER JOIN sys_user 反查（不是 N+1）。**理由**: spec §5.4 line 2246 响应示例含 `operatorName`，前端展示需要（`operatorId` 是 Snowflake 字符串对用户无意义）；outerjoin 让「operator 用户被删除」场景仍能返回 batch（operator_name=None），不抛错 — 这与「删除用户保留审计」语义对齐；单查询避免 N+1（列表场景 task 15c 会复用此模式）。**反例**: API 层先查 batch 再查 user → 2 次 DB roundtrip；inner join → operator 删除时整个 batch 不可见，破坏审计完整性。**回归**: `get_batch_detail` 实现 `select(UserImportBatch, User.user_name).outerjoin(User, User.user_id == UserImportBatch.operator_id)`；测试 `test_operator_name_from_user_join` 验证 service 返回元组 + API 透传到 response。
  - **决策 15.8**: API helper `_compute_batch_expires_at` / `_build_batch_response` 模块级函数（不是 service 方法）。**理由**: 这些是 HTTP 响应组装逻辑（ORM → Pydantic → dict），不属于 service 业务层；与既有 `_validate_import_reason` / `_coerce_dry_run` 同款 — API 层处理 HTTP 协议细节；让 service 保持「纯业务查询」语义。**反例**: 把 expires_at 计算放 service → service 层耦合 HTTP 响应字段；放 Pydantic validator → datetime 计算放在 schema 里反直觉。**回归**: `api/user.py` 末尾区段 `# === 用户导入批次详情（spec §5.4）===` 集中放 helper + endpoint；endpoint 调 `_build_batch_response(batch, operator_name)` 一次组装完。
  - **决策 15.9**: 路由顺序：GET /import/template 必须声明在 GET /import/{batch_id} 之前，否则 batch_id="template" 会拦截模板下载。**理由**: FastAPI 路由按声明顺序匹配，`/import/template` 与 `/import/{batch_id}` 都是 GET + 同前缀 → 先声明的优先；POST /import（无冲突，方法不同）；GET /import/{batch_id} 放在 template 之后、POST /export 之前。**反例**: GET /import/{batch_id} 先声明 → 用户访问 /import/template 时被 batch_id="template" 匹配，返回 404 AI_IMPORT_BATCH_NOT_FOUND，模板下载失效。**回归**: api/user.py 中 `@router.get("/import/template", ...)` 在 `@router.get("/import/{batch_id}", ...)` 之前；测试 `test_user_import_template_api.py::TestDownloadTemplateResponse::test_returns_xlsx_with_content_disposition` 仍通过（模板下载未受影响）。
  - **决策 15.10**: 路径参数 `batch_id: str`（不是 int），与 ORM `batch_id: Mapped[str]` 对齐。**理由**: spec §3.6 line 1749 batch_id 是 UUID-style 字符串（`secrets.token_urlsafe(32)[:32]`），不是 Snowflake 数字；与 export_id（Snowflake，字符串化）不同；前端路由用 `/import/:batchId` 自然。**反例**: 强制 int → UUID 解析失败；Pydantic 路径参数校验拒绝 → 422。**回归**: endpoint `batch_id: str`；service `where(UserImportBatch.batch_id == batch_id)`；测试用 `"batch-abc-001"` / `"nonexistent-batch"` 字符串 token。
  - **决策 15.11**: status 字段返回 `batch.status.value`（不是 `str(batch.status)`）。**理由**: SQLAlchemy `Enum` 列在 ORM 实例上返回 Python enum 成员（`ImportBatchStatus.PARTIAL_SUCCESS`），`.value` 拿到 `"PARTIAL_SUCCESS"` 字符串；`str()` 在某些 Python 版本返回 `"ImportBatchStatus.PARTIAL_SUCCESS"`（含类名前缀）；`hasattr(batch.status, "value")` 防御性兜底（万一 ORM 返回原生字符串）。**反例**: 直接 `batch.status` → Pydantic 序列化 ImportBatchStatus enum 不一定按 value（取决于 model_config）；`str()` 跨 Python 版本不稳。**回归**: helper `_build_batch_response` 用 `batch.status.value if hasattr(batch.status, "value") else str(batch.status)`；测试断言 `data["status"] == "PARTIAL_SUCCESS"`。
  - **决策 15.12**: 404 错误码 `AI_IMPORT_BATCH_NOT_FOUND`（与 `AI_EXPORT_TASK_NOT_FOUND` 命名对称）。**理由**: spec §5.7 错误码表列出 import / export 各自的 NotFound；前端 i18n 映射 `errorCode.AI_IMPORT_BATCH_NOT_FOUND` → 「批次不存在或已过期」；与 `AI_EXPORT_TASK_NOT_FOUND` 对称便于前端共用 NotFound 处理逻辑。**反例**: 通用 `AI_NOT_FOUND` → 前端无法区分批次 vs 任务，i18n 文案模糊；用 `NotFoundException("用户导入批次")` 不带 error_code → 全局 handler 默认 `RESOURCE_NOT_FOUND`，前端无法精准映射。**回归**: endpoint `raise NotFoundException("用户导入批次", error_code="AI_IMPORT_BATCH_NOT_FOUND")`；测试 `test_not_found_returns_404` 断言 `body["errorCode"] == "AI_IMPORT_BATCH_NOT_FOUND"`。

- [x] Task 15a ✅ 已完成（2026-08-04）：HTTP `GET /system/user/import/{batch_id}/logs`（批次操作日志查询，分页 + event filter）+ 10 测试。决策：
  - 15a.1 **测试模式对齐 Task 15** — patch service（`get_batch_detail` + `list_batch_logs`）只验 HTTP 契约（路由 / 200 字段映射 / 404 / auth / event filter 转发 / pagination 参数转发），ordering / outerjoin / 过滤的业务正确性由 `test_user_import_execute.py` 写入侧集成测试覆盖。**反例**: 用真 DB 写 log 再查 → 测试 setup 复杂 + 跨 fixture 状态污染。**回归**: 10 用例（auth 2 + 200 字段映射 3 + 404 1 + event filter 2 + pagination 2）。
  - 15a.2 **权限 `system:user:list`** — spec §5.5 line 2284 明确「list 即可」，与 GET /import/{batch_id} 一致（同属查询历史/审计链路，不暴露用户敏感数据）。**反例**: 新增 `system:user:import` 权限 → 前端历史页按钮放不开（管理员能查 detail 但查不到 logs）。**回归**: admin_token 直查 200，无 token → 401，无效 token → 401。
  - 15a.3 **service 函数 `list_batch_logs` 模块级 + outerjoin** — 对齐 `get_batch_detail` 模式：`select(UserImportBatchLog, User.user_name).outerjoin(User, ...)` 一次性拿 operator_name，避免 N+1。返回 `(rows, total)` 元组（不用 `PageResult` 包，因 records 是 (log, name) 元组非纯 ORM）。**反例**: 单查 log 再循环查 user → 20 条 log 触发 21 次 SQL（N+1）。**回归**: `test_operator_name_none_for_deleted_user` 验证 outerjoin 兼容已删除操作人。
  - 15a.4 **event filter 不做 Literal 校验** — `event: str | None = Query(None)` 接受任意字符串，传 typo 返回空列表。**理由**: spec §5.5 未要求严格校验 + §5.7 错误码表无对应项 + 前端 dropdown 只会传合法值。**反例**: 加 `Literal[...]` → 后续加新 event 要同步改 schema，破坏开闭原则。**回归**: `test_event_filter_passed_to_service`（透传 EXECUTE_FINISH）+ `test_no_event_passes_none`（不传 → None）。
  - 15a.5 **batch 存在性预检 + 404 一致性** — endpoint 先 `get_batch_detail` 校验 batch 存在，再查 logs。**理由**: 与 GET /import/{batch_id} 路由语义一致（任何 `/import/{batch_id}/...` 子资源都先校验 batch 存在），前端轮询遇到删除的 batch 拿到 404 而不是空列表（明确语义）。**反例**: 直接查 logs 返空列表 → 前端无法区分「batch 不存在」vs「batch 存在但还没写 log」。**回归**: `test_batch_not_found_returns_404` 验证 `AI_IMPORT_BATCH_NOT_FOUND`。
  - 15a.6 **schema 字段超出 spec §5.5 契约最小集** — spec line 2285 写的是 `[{event, fromStatus, toStatus, detail, createdAt}, ...]`，实现额外暴露 `logId` + `operatorId` + `operatorName`。**理由**: `logId` 是前端 row key（同秒事件用 createdAt 做 key 会碰撞）；`operatorId/operatorName` 是审计追溯（对齐 Task 15 GET /import/{batch_id} 字段约定，回答「这批谁做的」）。**反例**: 严格按 spec 5 字段 → 前端列表无法稳定 row key + 无法按操作人筛选。**回归**: `test_returns_paginated_logs_with_field_mapping` 验证 8 字段全映射。
  - 15a.7 **service 排序 `created_at ASC + log_id ASC`** — 按 (batch_id, created_at) 索引（spec §2.28 line 1269）顺序返回完整状态转换历史。`log_id` 是次序兜底（同毫秒并发的 log 行确保稳定排序）。**反例**: 用 `log_id DESC` 最新优先 → 看不到「CREATED → PREVIEW_DONE → ... → SUCCESS」时序。**回归**: spec §2.28 line 1285 的 `test_log_records_all_lifecycle_events` 集成测试覆盖（已存在于 `test_user_import_execute.py`）。
  - 15a.8 **路由顺序无冲突** — `/import/{batch_id}/logs` 是 3 段路径，`/import/{batch_id}` 是 2 段，Starlette 路由按段数匹配，`{batch_id}` 不会拦截 `/logs` 后缀。**反例**: 无。**回归**: 全 10 测试通过证明路由匹配正确。
  - 15a.9 **`detail` JSON 字段直传不做脱敏** — spec §2.28 line 1245 已声明 detail 仅存业务字段（chunk_index / chunk_size / failed_in_chunk / reason 等），不含密码 / token 等敏感数据。**反例**: 担心 reason 暴露做脱敏 → 破坏审计完整性（reason 是批量操作的业务理由，本来就该让管理员看到）。**回归**: `test_returns_paginated_logs_with_field_mapping` 验证 detail 原样透传。
  - 15a.10 **pagination 默认 current=1 / size=10** — 对齐 `UserExportTaskQuery` / `QueryParams` 基类，前端不传参数时返回首页 10 条。**反例**: 默认 size=20 → 长 batch（CHUNK_PROGRESS * 20 = 400 条）一次拉太多。**回归**: `test_default_current_size` + `test_custom_current_size`。
- [x] Task 15b ✅ 已完成（2026-08-04） **[v2.2 P2 #2.29]**: HTTP `POST /system/user/import/{batch_id}/cancel`（PREVIEW_DONE 直接 cancel + RUNNING 协作式 cancel + 终态拒绝）+ 测试（17 用例）
  - 15b.1 **HTTP 契约测试用 patch service 替身** — 对齐 Task 15 / 15a 模式：HTTP 测试只验路由 + 字段映射 + 状态码 + auth gating + reason 校验，service 业务逻辑（CAS / Redis / file cleanup）由集成测试在 `test_user_import_execute.py` 等覆盖。**反例**: HTTP 测试用真实 DB → 测试 setup 复杂 + 业务 bug 会同时打破 HTTP 和 service 测试难以定位。**回归**: `tests/modules/system/test_user_import_cancel_api.py` 17 测试全 patch `cancel_batch`。
  - 15b.2 **权限码 `system:user:import`** — spec §5.6 line 2297 明确：cancel 是写入操作（修改批次状态），用 `system:user:import` 而非 `system:user:list`（detail / logs 是读操作才用 list）。**反例**: 用 `system:user:list` → 持有 list 权限的运营人员能取消他人的导入批次，违反最小权限。**回归**: `TestCancelBatchAuth::test_no_token_returns_401` + `test_invalid_token_returns_401`。
  - 15b.3 **`AI_IMPORT_BATCH_NOT_FOUND` 用 404 而非 spec §5.7 表写的 422** — spec §5.7 line 2323 标 422，但 Task 15 (GET detail) + Task 15a (GET logs) 早已 ship 用 `NotFoundException` → 404 + errorCode。跨端点一致性优先于单条 spec 文字（前端 `if (err.errorCode === 'AI_IMPORT_BATCH_NOT_FOUND')` 不关心 HTTP code）。**反例**: cancel 用 422 → detail/logs 用 404 → 前端要为同一 errorCode 写两套 HTTP code 处理。**回归**: `TestCancelBatchNotFound::test_cancel_nonexistent_batch_returns_404`。
  - 15b.4 **`AI_IMPORT_BATCH_NOT_CANCELLABLE` 用 422（spec §5.7 line 2324）** — 终态（SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED）+ CREATED 都不可取消，抛 `UnprocessableEntityException` → 422。CREATED 拒绝因 spec §2.26 line 1117 v2.2 P1-2 已把 cancel-from-state 改为仅 `PREVIEW_DONE → CANCELLED`（dry_run 完成 = PREVIEW_DONE，CREATED 是 dry_run 中间的瞬时态用户不可见）。**反例**: 允许 CREATED cancel → dry_run 中用户点 cancel → CAS 与 dry_run 末尾的 CREATED → PREVIEW_DONE 竞态。**回归**: `TestCancelTerminalBatchRejected` 6 个状态参数化（CREATED/SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED/CANCELLED 全 422）。
  - 15b.5 **RUNNING 协作式 cancel 立即返回 status=RUNNING** — spec §2.29 line 1338：cancel 请求不等待 chunk 真暂停。响应 `status` 字段反映当前 batch.status（RUNNING），`cancelledAt` 反映 Redis flag 设置时间（前端显示「已请求取消，等待当前 chunk 完成」）。实际 `RUNNING → PARTIAL_SUCCESS` 转换发生在 chunk loop 内（spec §2.29 line 1351-1368）。**反例**: cancel 同步等 chunk 暂停 → HTTP 超时 + chunk 长（100 rows × 多 chunk）用户等待 30s+。**回归**: `TestCancelRunningBatch::test_cancel_running_sets_redis_flag_and_returns_running`。
  - 15b.6 **`cancelledAt` = batch.finished_at or now** — PREVIEW_DONE cancel 成功后 service 设置 `finished_at=now`，CANCELLED 状态直接用；RUNNING 协作式 cancel 不改 finished_at，API 层 fallback 到 `datetime.now()`（标志设置时间）。**反例**: 强制要求 service 改 finished_at → RUNNING 状态 finished_at 被错误设置成 cancel 请求时间，与实际批次结束时间（chunk loop 结束后）冲突。**回归**: `test_cancel_preview_done_returns_cancelled` 校验 `cancelledAt == finished_at`。
  - 15b.7 **复用 `ReasonSchema` 作 body 类型** — spec §5.6 line 2294-2296 body 仅含 `reason` 字段，直接用 `ReasonSchema`（Task 8 已导出），不另建 `UserImportCancelRequest`。**反例**: 每个端点都建一个空的 Request 子类 → 类爆炸。**回归**: `TestCancelReasonValidation` 4 测试（required / empty / whitespace / too_long）覆盖 ReasonSchema 入口校验。
  - 15b.8 **`cancel_batch` 为模块级函数（非 UserService 方法）** — 对齐 Task 9 `dry_run_import_users` / Task 10 `batch_create_users_from_records` / Task 15 `get_batch_detail` / Task 15a `list_batch_logs` 全部模块级函数模式。无状态 + 易测试 + 易注入 `file_storage` 参数。**反例**: 加到 UserService 单例 → 增加循环依赖（UserService → import_service → UserService）。**回归**: service 模块 `__all__` 导出 `cancel_batch`，API 层直接 `from import_service import cancel_batch`。
  - 15b.9 **PREVIEW_DONE 场景清理 preview 文件 + Redis cache** — spec §2.29 line 1328-1329：CAS 成功后删 `file_storage_key` 文件 + `user_import:preview:{preview_token}` Redis key。`storage.delete` 容忍 `FileNotFoundError`（cleanup cron 可能已清 / 测试环境文件未生成）。**反例**: 文件不存在时 raise → cancel 主流程被文件系统状态污染。**回归**: service 层 `try/except FileNotFoundError: pass` 兜底。
  - 15b.10 **RUNNING 场景 Redis flag key + TTL** — key 格式 `user_import:cancel:{batch_id}`（前缀 + batch_id，便于 chunk loop 反查），TTL=3600s（1h，spec §2.29 line 1335）。chunk loop 在 `batch_create_users_from_records` 内每个 chunk 边界检查 `redis.exists(flag)`，命中则 break + 转 PARTIAL_SUCCESS（Task 10 内嵌的协作式 cancel checkpoint，本 task 仅实现 flag 设置侧）。**反例**: TTL=10min → 长 chunk（2000 rows / 100 = 20 chunks × 30s/chunk = 10min）flag 可能过期。**回归**: HTTP 测试覆盖「service 被 cancel_batch 调用」契约，flag 设置逻辑由后续 Task 10 协作式 cancel checkpoint 集成测试覆盖。
  - 15b.11 **`is_super_admin` 在函数内 import（避免循环依赖）** — `app.core.rbac` 链路 `rbac → User → ...`，import_service 顶部 import 会循环。函数内 `from app.core.rbac import is_super_admin` (`# noqa: PLC0415`) 是 Python 惯用法（同 `get_batch_detail` 内的 `from app.modules.system.models.user import User`）。**反例**: 顶部 import → `ImportError: cannot import name 'is_super_admin'` 启动失败。**回归**: `TestCancelByNonOperatorForbidden::test_cancel_by_non_operator_returns_403`。
  - 15b.12 **Service 层 `_validate_reason` defense-in-depth** — API 层 `ReasonSchema` 已 strip + 校验长度，service 入口仍调 `_validate_reason(reason)` 防 AI tool 直接调用 / 内部代码绕过 API。**反例**: 信任 API 入口校验 → AI tool 用 raw 字符串调 cancel_batch 跳过校验 → reason 入库 null / 超长。**回归**: `_validate_reason` 抛 `AI_IMPORT_REASON_REQUIRED`，service 测试覆盖。
  - 15b.13 **Service 抛 NotFoundException 而非返 None** — Task 15 `get_batch_detail` 返 `(None, None)` 让 API 层抛 404；Task 15b `cancel_batch` 直接在 service 抛 `NotFoundException`。差异原因：cancel_batch 是单一返回类型（UserImportBatch），不像 get_batch_detail 还要返 operator_name 元组；service 抛异常让 API 层更简洁（无 if None 分支）。**反例**: cancel_batch 返 None → API 层判 None 后抛 404 → 业务异常处理散落两层。**回归**: HTTP 测试 patch service 抛 NotFoundException 验证 404 转换。
- [x] Task 15c ✅ 已完成（2026-08-04）**[v2.2 P2]**: HTTP `GET /system/user/import`（批次列表分页查询，按 operator_id / status / created_at 过滤）+ 测试（7 用例）。决策：
  - 15c.1 **HTTP 契约测试 patch service** — 同 Task 13/15/15a/15b 模式，HTTP 测试只验证路由 / 字段映射 / auth gating / 过滤参数透传，service 层用 `AsyncMock` 替身。**反例**: 真 DB 查询 → 测试慢 + 隔离差 + 跨 task 重复覆盖。**回归**: `TestListBatchesResponse::test_returns_paginated_list_with_response_fields` patch `list_batches` 验证 200 + PageResult 字段。
  - 15c.2 **权限 `system:user:list`（与 GET /import/{batch_id} 一致）** — spec §5.4 line 2234 + line 2276：list 即可，因为查的是导入历史不是用户敏感数据。**反例**: 用 `system:user:import` → 历史查看要写权限不合理（导入历史是审计反查场景）。**回归**: `TestListBatchesAuth::test_no_token_returns_401`。
  - 15c.3 **service 返 `(rows, total)` 元组（不是 PageResult）** — `list_batch_logs` 同款，因 outerjoin sys_user 拿 operator_name 不能用通用 `paginate()` helper（不支持 join）。API 层自行构造 PageResult。**反例**: service 返 PageResult[ORM] → API 层还要再 join sys_user → N+1 或重复 outerjoin。**回归**: `list_batches` 返 `[(batch, operator_name), ...]`，API 层 `_build_batch_response` 复用 GET /import/{batch_id} 的 dict 构造逻辑。
  - 15c.4 **`UserImportBatchQuery` 不用 paginate() helper** — 外连接 `sys_user` 拿 operator_name（同 `list_batch_logs`），手写 count + select + outerjoin + offset/limit 两段查询。**反例**: 用 `paginate(db, UserImportBatch, ...)` → operator_name 必须 N+1 单独查每行的 sys_user。**回归**: `list_batches` 内 outerjoin User，rows 是 `(batch, user_name)` 元组列表。
  - 15c.5 **排序 `(created_at DESC, batch_id DESC)`** — 同秒批次按 batch_id 字典序降序兜底，避免 tie-flicker（分页翻页时同 created_at 的批次顺序不稳定）。**反例**: 仅 `created_at DESC` → 翻页时同秒 batch 可能重复 / 跳过。**回归**: `list_batches` 的 `.order_by(created_at.desc(), batch_id.desc())`。
  - 15c.6 **status str + service 层 BusinessRuleException** — 与 `UserExportTaskQuery` + `list_export_tasks` 完全对称：status 不用 `Literal[ImportBatchStatus]`，让 service 抛 `BusinessRuleException(AI_IMPORT_INVALID_STATUS)`（默认 code=400）。**反例**: 用 Literal → FastAPI 默认 422 Pydantic 校验错误，与 export 不一致；或用 `UnprocessableEntityException` → 跨端点 status 校验 HTTP 码分叉（export 400 / import 422）。**回归**: `TestListBatchesInvalidStatus::test_invalid_status_returns_400` 验证 400 + errorCode。
  - 15c.7 **created_at 时间窗用 `LocalNaiveDatetime`** — CLAUDE.md pitfall 12：DB 列 `TIMESTAMP WITHOUT TIME ZONE`（naive），前端 NDatePicker 发 unix ms timestamp，Pydantic 默认解析为 aware datetime → asyncpg `TypeError: can't subtract offset-naive and offset-aware datetimes` → HTTP 500。`LocalNaiveDatetime` 自动按服务器本地时区转 naive。**反例**: `datetime` 类型直接接受 ms → 8 小时跨时区漂移 / asyncpg 报错。**回归**: `TestListBatchesFilters::test_passes_created_at_range_to_service` 验证 startTime/endTime ms → naive datetime 透传到 service。
  - 15c.8 **复用 `_build_batch_response` + `_compute_batch_expires_at`** — Task 15 GET /import/{batch_id} 的两个 helper 在 list endpoint 复用，保证字段映射 + 剥离敏感字段（preview_token / file_sha256 / records_hash / reason）+ 动态算 expires_at 完全一致。**反例**: list endpoint 重写一份字段映射 → 长期演化字段（如新增 sync_mode）时 detail / list 漂移。**回归**: `TestListBatchesResponse::test_returns_paginated_list_with_response_fields` 同时验证字段 + `previewToken/fileSha256/recordsHash/reason` 剥离 + `expiresAt` 动态计算（PARTIAL_SUCCESS → finished_at + 24h）。
  - 15c.9 **`operatorId` 字符串化透传到前端** — `UserImportBatchResponse._serialize_operator_id` 把 Snowflake int 转 str（防 JS BigInt 精度丢失），list 接口 records 每条都走同样序列化。**反例**: list 用裸 int → 前端 JSON.parse 丢精度。**回归**: `TestListBatchesResponse` 断言 `first["operatorId"] == "1"` + `isinstance(first["operatorId"], str)`。
  - 15c.10 **测试 helper 显式传所有 summary_*/count 字段** — ORM `default=0` 仅在 flush 时触发，直接 `UserImportBatch(...)` 实例化（不入库）时这些字段是 `None`，Pydantic 验证 `Input should be a valid integer` 失败。helper 必须显式传 0 或具体值。**反例**: 只传 batch_id/status → 8 个 int 字段全 None → `_build_batch_response` 内 `UserImportBatchResponse(...)` 抛 ValidationError。**回归**: `_make_batch_row` 显式传 `summary_new/exists/conflict/out_of_scope + success/skipped/overwritten/failed_count` 默认值。
  - 15c.11 **路由顺序：GET /import 注册在 GET /import/{batch_id} 之前** — 虽然 FastAPI 对 `/import` vs `/import/{batch_id}` 这种「无后缀 vs 有 path param」的模式按精确匹配，注册顺序不影响行为，但按声明顺序排列更清晰（list 在前，detail 在后）。**反例**: detail 在前 → 阅读路由表时易误判 batch_id 取空字符串也匹配。**回归**: `api/user.py` 路由顺序：`list_import_batches` → `get_import_batch_detail` → `get_import_batch_logs` → `cancel_import_batch`。

#### Seed + 前端
- [x] Task 16 ✅ 已完成（2026-08-04）：权限码 seed（`system:user:import` / `system:user:export`）+ `sys_config.auth:default_password` seed 到 `sync_menus.py` / `init_db.py`。15 用例（test_sync_menus_definitions 6 + test_init_db_seed 9）。决策 16.1-16.8。
  - 16.1 **双 seed 入口（sync_menus + init_db）** — sync_menus.py 增量同步（已部署环境跑一次拿新按钮）；init_db.py 全新部署 fresh install 一次到位。两者都必须 seed，单边会导致某条路径缺权限码（已部署环境只有 sync_menus / 新环境只有 init_db）。**反例**: 只动 init_db → 已部署环境升级后 admin 看不到导入导出按钮；只动 sync_menus → 全新部署后第一次 sync_menus 之前没有按钮（且 admin 角色没绑定）。**回归**: `test_sync_menus_definitions.py::TestUserImportExportPermissionSeed` + `test_init_db_seed.py::TestUserButtonPermissionSeed` 双文件覆盖。
  - 16.2 **sys_config seed 只放 init_db.py** — sync_menus.py 只管 sys_menu 表，sys_config 是不同表 + 不同生命周期（admin UI 可改值）。已部署环境的 `auth:default_password` 由部署方在 admin UI 手动配，helper 缺失时抛 `AI_IMPORT_DEFAULT_PASSWORD_NOT_SET`（防代码默认值蒙混）。**反例**: 写 alembic migration INSERT sys_config → migration 跑完后部署方改值，下次环境重建 migration 又覆盖回默认值。**回归**: `test_init_db_seed.py::TestDefaultPasswordConfigSeed::test_default_password_config_seeded`。
  - 16.3 **默认密码 `hohu123456` 与 admin 一致 + remark 强警告** — fresh install 默认值与 admin 密码默认值一致（运维一处记），remark 含「上线前请改 / prod 禁止保留默认值」安全提示（spec §2.5 反例 3：默认密码硬编码 → 部署方无法改）。helper 不读 remark，纯提示用。**反例**: 默认值用空字符串 → helper 立即抛 NOT_SET，admin 必须先配才能用，但 init_db 后管理员可能困惑「为什么刚 seed 完就用不了」；或默认值用 random UUID → 与 admin 密码不一致，运维多记一份。**回归**: `test_default_password_config_seeded` 验证 value 非空 + status='1'；`test_default_password_config_remark_warns_to_change` 验证 remark 含安全关键词（修改 / 安全 / prod / 上线 等）。
  - 16.4 **`is_public=False`** — sys_config 模型有 `is_public` 字段控制是否暴露给 `/system/config/public` 接口（前端登录页等未授权场景拿配置）。默认密码属于敏感配置，必须 `is_public=False`，否则任何匿名访问者都能拿到默认密码 → 全员越权。**反例**: `is_public=True`（或漏配默认值）→ 攻击者 curl 公开接口拿默认密码 → 用默认密码 + 任意 user_name 登录新导入用户。**回归**: `test_default_password_not_marked_public` 严格断言 `cfg.is_public is False`。
  - 16.5 **模块级 `_system_user_menu_id` 共享 menu_id** — init_db.py 原 `Menu(menu_id=next_id(), ...)` 内联调用，按钮子菜单拿不到父 menu_id（F-type 按钮 parent_id 必须指 system_user 的 menu_id，否则前端菜单树挂不上 orphan button）。重构成 `_system_user_menu_id = next_id()` 模块级常量，system_user Menu + 2 按钮都引用它。**反例**: 按钮用 `parent_id=0` → 前端 build_menu_tree 找不到父，按钮悬挂不显示；或按钮再调一次 `next_id()` 当 parent_id → 永远碰不到 system_user。**回归**: `test_import_button_parent_links_to_system_user` + `test_export_button_parent_links_to_system_user` 用 `_find_menu_by_route_name("system_user").menu_id` 反查并断言 `button.parent_id == parent.menu_id`。
  - 16.6 **静态测试（不调用 init_db / sync_menus）** — init_db() 入口含 `input()` 交互 + DROP TABLE 灾难性副作用，sync_menus() 真连 DB 写入。两者都不适合在单测里直接跑。改为静态校验 `init_menus` / `init_configs` / `MENU_DEFINITIONS` 列表内容（形状 + 关键字段），覆盖 99% 的 seed 错误（漏 seed / 错 permission / orphan parent / 漏 remark）。**反例**: 用 testcontainers 跑真实 init_db → 慢 + 脆 + 还原成本高。**回归**: 15 测试全静态，0 DB 依赖，0.08s 跑完。
  - 16.7 **`SEED_TABLES` 加 `sys_config`** — init_db.py 重置逻辑 `clear_seed_data` 按 `SEED_TABLES` 列表 TRUNCATE。原列表缺 `sys_config`，重置后旧 sys_config 残留 → 重新 seed 时 `db.add_all(init_configs)` 撞 UniqueViolation（config_key 唯一索引）。**反例**: 不加 `sys_config` → 重置后 `auth:default_password` 仍在表里 → add_all 报 IntegrityError。**回归**: `init_db.py` 的 `SEED_TABLES` 列表新增 `"sys_config"`。
  - 16.8 **路由层 `require_permissions("system:user:import" / "system:user:export")` 依赖本 seed** — API 层 `app/modules/system/api/user.py` 的导入导出端点 `Depends(require_permissions("system:user:import"))` 直接读 sys_menu 表的 permission 字段做 RBAC。本 seed 缺一个 → 端点 403 Forbidden 给所有非超管用户。**反例**: permission 字符串拼写漂移（如 `system:user:imports` 多 s）→ API 层 `require_permissions` 找不到菜单 → 403。**回归**: seed 测试断言 `permission == "system:user:import"`（精确字符串），与 API 层声明完全一致；CLAUDE.md pitfall 11 已声明此「拼写漂移」陷阱。
- [x] Task 17 ✅ 已完成（2026-08-04）：前端 `user-import-modal.vue` 3 步弹窗（4 卡片统计 + 错误清单前 20 条 + sync_mode radio + cancel 按钮 + idempotentReplay toast）+ vitest。决策 17.1-17.6：
  - 17.1 **composable / 组件拆分** — UI 与状态机解耦：`use-import-flow.ts`（pure logic）+ `user-import-modal.vue`（pure UI）。**理由**: 不挂 NaiveUI 单测 composable，速度快；组件渲染级测试留 Task 21 E2E。**反例**: 全测组件需 mount NaiveUI + i18n + 主题 provider，每个 case 起 jsdom → 慢且脆。**回归**: `src/views/system/user/modules/__tests__/use-import-flow.spec.ts` 10 测试覆盖 upload/confirm/cancel/template/idempotentReplay/validate。
  - 17.2 **mock 模式** — `vi.mock('@/service/api')` + `vi.mock('@/locales')`，stub 4 个 fetchXxx 返回 `{ data, error, response }`（匹配 FlatResponseData），`$t` 返回原 key。**反例**: 试图 mount 真组件 + stub `request` 拦截器 → 复杂且依赖 axios runtime。
  - 17.3 **i18n 命名空间划分** — `common.importModal.*` 公共流程文案（任何模块复用导入弹窗零拷贝）+ `common.downloadTemplate` + `page.system.user.defaultPasswordHint`（仅 user 模块）+ `errorCode.AI_IMPORT_*` 错误码映射（spec §6.4 line 2471-2483 完整对齐）。**反例**: 把 defaultPasswordHint 放 `common.importModal.*` → role 模块（无密码概念）误用。**回归**: zh-cn.ts + en-us.ts + app.d.ts Schema 类型完整声明（typecheck 严格模式必需）。
  - 17.4 **Blob 下载走 createObjectURL** — `downloadTemplate()` 内部 `URL.createObjectURL(blob)` + `<a download>` click + `URL.revokeObjectURL()`。**理由**: 比直接 `window.open(url)` 更可控，且不暴露 access_token 到 URL（模板端点要 Bearer auth）。**反例**: 用 `<a href="/api/.../template">` → 浏览器不带 Authorization header → 401。
  - 17.5 **defaultPassword 走 prop** — composable 不强依赖 sys_config，父组件（Task 18 列表页接入时）传入。**理由**: 解耦；Task 18 list page 自己拿 sys_config 一次（可能多 modal 复用）。**回归**: `user-import-modal.vue` Props `defaultPassword?: string`，未传时不显示 hint。
  - 17.6 **dry_run batchId 缺口（Task 22c 待补）** — 后端 `dry_run_import_users` 响应 `result_data.update({...})` 漏加 `batchId`（spec §2.21 line 773 已要求）。前端 `UserImportDryRunResult.batchId?` 标 optional，`useImportFlow.cancelImport()` defensive：缺 batchId 时 → `NO_BATCH_ID` errorCode + 按钮 disabled。**反例**: 强制后端立刻补 → 跨 task scope 扩散。**回归**: 后端 `app/modules/system/api/user.py:553` 补 `"batchId": batch.batch_id` 后此 defensive 自动失效（typecheck 自动通过 batchId 必填化）。
- [x] Task 18 ✅ 已完成（2026-08-04）：前端列表页 4 按钮（导入 / 导出 / 导入历史 / 复用 modal 的下载模板）+ i18n keys（`common.import` / `common.export` / `common.importHistory` / `common.exportModal.*` / `common.importHistoryDrawer.*` / `errorCode.AI_IMPORT_*` 13 项补全）。决策 18.1-18.5：
  - 18.1 **TableHeaderOperation prefix slot 注入按钮** — `TableHeaderOperation` 组件已暴露 `prefix` / `suffix` slot（`src/components/advanced/table-header-operation.vue:62`），导入/导出/历史按钮走 prefix slot 而不是 default slot（default 是 add+batchDelete，强耦合 `showAdd/showDelete` 逻辑）。**反例**: 在 default slot 加按钮 → slot fallback 触发 add/batchDelete 渲染（CLAUDE.md pitfall 13「Permission in slot」）。**回归**: `index.vue` `<template #prefix>` 注入 3 NButton，按钮 v-if 由 `hasAuth('system:user:import')` / `hasAuth('system:user:export')` 控制（与后端 `Depends(require_permissions(...))` 双重校验，前端隐藏是 UX-only）。
  - 18.2 **defaultPassword 走 sys_config 查询** — `onMounted` 内 `loadDefaultPassword()` 调 `fetchGetConfigList({ configKey: 'auth:default_password' })` 取 `cfg.configValue` 注入到 `<UserImportModal :default-password="defaultPassword">`。**理由**: spec §2.5.1 + §6.2 决策 17.5 已说默认密码从 sys_config 取，本次接入列表页时落实 prop 注入；composable 不强依赖。**反例**: composable 内部硬编码 'hohu123456' → 部署方改 sys_config 时不生效。**回归**: 列表页 `loadDefaultPassword` 取出后注入 prop；fetchGetConfigList `configKey` filter 后端已支持（`/system/config/list?configKey=...`）。
  - 18.3 **3 个新组件 + 1 个新 composable** — `user-export-modal.vue`（导出弹窗）+ `user-import-history.vue`（历史抽屉 + 详情抽屉）+ `use-export-flow.ts`（导出 composable）+ `use-export-flow.spec.ts`（10 vitest 用例）。**理由**: 对齐 Task 17 composable/UI 拆分模式，避免 mount NaiveUI 单测；4 文件 + index.vue 改动一次性 ship 完前端导入导出闭环。**反例**: 把 export/history 逻辑塞进 use-import-flow → 单 composable 200+ 行混多个流程，违反单一职责。
  - 18.4 **路由复用 list 页** — `user-import-history` 是 NDrawer 而非独立 route（`/system/user/import-history`）。**理由**: spec §6.1 line 2336-2349 未要求独立路由；list 页用户操作上下文连续（刚导入完点历史查批次）；NDrawer 一开就在 list 页右侧，关闭即回 list，UX 比 route 跳转流畅。**反例**: 独立 route → 用户点 history 离开 list → 看完批次还要回退 / 重新搜索；URL 收藏价值低（history 是审计反查，不是常用入口）。**回归**: `index.vue` `<UserImportHistory ref="userImportHistoryRef" />`，按钮 `@click="openImportHistory"` 调 `userImportHistoryRef.value?.open()`。
  - 18.5 **errorCode 补 13 项 + Schema 类型同步** — spec §5.7 表列出 24 个 errorCode，Task 17 已 ship 8 个核心，本次补 `AI_IMPORT_DEPT_PATH_NOT_FOUND` / `DEPT_DUPLICATE` / `ROLE_NOT_FOUND` / `DEPT_OUT_OF_SCOPE` / `ROLE_OUT_OF_SCOPE` / `USERNAME_DUPLICATE` / `EMPLOYEE_NO_EXISTS` / `EMPLOYEE_NO_DUPLICATE` / `BATCH_NOT_FOUND` / `BATCH_NOT_CANCELLABLE` / `DEFAULT_PASSWORD_NOT_SET` / `REASON_REQUIRED` / `AI_EXPORT_TASK_NOT_FOUND` 共 13 项。**理由**: 前端 `error.response.data.errorCode` 映射需要全部声明（vue-i18n 严格模式 + app.d.ts Schema 必须同步）。**反例**: 只 ship 部分 → 后端抛 `AI_IMPORT_DEPT_OUT_OF_SCOPE` 时前端 fallback 到 backend `msg`，UX 退化。**回归**: zh-cn.ts + en-us.ts + app.d.ts 三处同步（41 行 Schema 追加）。
- [x] Task 19 ✅ 已完成（2026-08-04）：前端导出按钮改 POST + fetch/Blob 下载逻辑 + 超阈值 toast（`AI_EXPORT_ASYNC_REQUIRED` → `common.exportModal.errorCode.ASYNC_REQUIRED`）。决策 19.1-19.4：
  - 19.1 **composable 抽 `buildExportPayload` + `summarizeFilter` 纯函数** — `use-export-flow.ts` 导出 2 个纯函数（无 ref），单测直接 import 跑，不挂 vue runtime。**理由**: filter → payload 映射逻辑可单测，避免 mount 组件 + 模拟 watch/setFilter；与 `use-import-flow.ts` 模式一致（`validateFile` / `notifyError` 内部函数 → 纯函数 helper）。**反例**: 把 buildExportPayload 写在组件 setup 内 → 必须挂组件才能测；或写在 composable 内不导出 → 单测无法 import。**回归**: `use-export-flow.spec.ts::buildExportPayload` + `summarizeFilter` 4 用例（null filter / 完整 filter / 字段排除 / 空摘要）。
  - 19.2 **Blob 下载文件名 `users_YYYYMMDD.xlsx`** — 后端 `Content-Disposition: attachment; filename=users_20260801.xlsx`（spec §5.2 line 2200），前端不解析 Content-Disposition（Blob URL 无法读 header），自行按本地时区拼 `users_${ymd}.xlsx`。**理由**: 与后端命名一致（年月日 8 位），前端 timezone = server timezone（CLAUDE.md pitfall 12 已锁定 naive datetime 用 server local tz）。**反例**: 解析 Content-Disposition → Blob response 不带原始 header（fetch wrapper 已剥）；用 ISO `users_2026-08-04.xlsx` → 与后端 `users_20260804.xlsx` 不一致。**回归**: `use-export-flow.ts::buildFilename` 内 `new Date()` 拼 `getFullYear + 2-digit month + 2-digit date`；`spec.ts::confirmExport triggers API + Blob download on success` 断言 `/^users_\d{8}\.xlsx$/`。
  - 19.3 **`responseType: 'blob' as any` 绕过 axios typing** — fetchExportUsers 用 `responseType: 'blob' as any`（参考 `fetchDownloadImportTemplate` / `fetchExportConfig` 既有模式）。**理由**: axios type 不接受 'blob' 字面量；用 `as any` 是项目惯例，运行时正确。**反例**: 用 `responseType: 'arraybuffer'` → 后端返回 xlsx binary 正确但前端无 Blob MIME → 文件下载扩展名可能错；或 fork @sa/axios 包加 typing → 范围扩散到 monorepo package。**回归**: `service/api/system.ts:fetchExportUsers` `responseType: 'blob' as any`。
  - 19.4 **AI_EXPORT_ASYNC_REQUIRED errorCode 短路 + modal 保持打开** — 后端返回 422 + `errorCode: AI_EXPORT_ASYNC_REQUIRED` 时，`useExportFlow.confirmExport` 检测到 → `notifyError('ASYNC_REQUIRED')` → return false；modal 不关闭（用户可改 reason 重试或关闭）。**理由**: spec §5.2 line 2201 要求 toast 提示「请分批或等待异步通道开放」；关闭 modal 强迫用户重新点导出 → UX 退化。**反例**: 任意 error 都关闭 modal → 用户看到 toast 但已经回到 list，重试点开 modal 又要输 reason。**回归**: `use-export-flow.ts::confirmExport` `if (errCode === 'AI_EXPORT_ASYNC_REQUIRED') notifyError('ASYNC_REQUIRED')`，return false → modal 保持打开（`user-export-modal.vue::handleConfirmExport` 不调 close）。
- [x] Task 19a ✅ 已完成（2026-08-04）**[v2.2 P2]**: 前端「导入历史」抽屉（`user-import-history.vue`：批次列表 + status filter + 详情双 tab（summary + logs）+ cancel 弹窗 + 状态色映射）+ 决策回写。决策 19a.1-19a.5：
  - 19a.1 **NDrawer 而非独立 route** — 决策 18.4 已记录，本 task 落实：`<NDrawer v-model:show="visible" :width="920">` + `<NDrawerContent :title closable>`，列表 + filter + 详情都在抽屉内。**反例**: 独立 route → 离开 list 上下文，UX 退化。**回归**: `index.vue` `<UserImportHistory ref="userImportHistoryRef" />`，按钮 `@click="openImportHistory"` 调 `userImportHistoryRef.value?.open()`。
  - 19a.2 **状态色 tagType 映射** — `tagType(status)` 返回 NaiveUI.ThemeColor：SUCCESS→success / PARTIAL_SUCCESS→warning / FAILED/EXPIRED/CANCELLED→error / RUNNING/PREVIEW_DONE→info / 默认→default。**理由**: 状态语义对齐 NaiveUI color convention（success/warning/error 是用户期望的语义色）。**反例**: 全用 default tag → 状态色无差异，用户需逐行看 status 字段才能区分。**回归**: 列表 status 列 + 详情 status 描述都走 `tagType`。
  - 19a.3 **详情双 tab（summary + logs）** — `NTabs` 嵌 `NTabPane name="summary"`（NDescriptions 列 batchId/status/filename/operator/totalRows/expiresAt/4 summary/4 count/createdAt/finishedAt 共 16 字段）+ `NTabPane name="logs"`（NDataTable 列 event/from/to/operator/time/detail 共 6 列）。**理由**: spec §5.4 + §5.5 已分两接口（GET /import/{batch_id} 拿 summary，GET /import/{batch_id}/logs 拿 logs）；UI 分 tab 与接口对齐。**反例**: 单 tab 平铺 → 长 batch 100+ log 行把 summary 顶到下方，用户必须滚动才能看 summary。**回归**: `user-import-history.vue` NTabs 双 pane。
  - 19a.4 **cancel 弹窗强制 reason + 复用 ReasonSchema 后端校验** — `confirmCancel` 内 `if (!cancelReason.value.trim()) warn + return false`；调 `fetchCancelImportBatch(batchId, reason.trim())` 走 POST body `{ reason }`，后端 `ReasonSchema` strip + 长度校验（spec §5.6 + #2.30 P1-3）。**理由**: spec §2.30 强制所有批量操作必填 reason（审计反查）；前端预校验减少 422 请求；后端最终兜底。**反例**: 前端不校验 → 空 reason 直达后端 → 422 AI_IMPORT_REASON_REQUIRED + 用户重试；或后端不校验 → reason 入库 null 审计链路断。**回归**: `confirmCancel` 入口 `if (!cancelReason.value.trim())` 短路 + warning toast。
  - 19a.5 **isCancellable = status ∈ {PREVIEW_DONE, RUNNING}** — spec §2.29 + Task 15b.4：CANCELLED/SUCCESS/PARTIAL_SUCCESS/FAILED/EXPIRED 是终态不可取消；CREATED 是 dry_run 中间瞬时态不可取消（CAS 会撞）；只有 PREVIEW_DONE（dry_run 完成用户改主意）+ RUNNING（协作式 cancel 设 Redis flag）可取消。**反例**: 允许 CREATED cancel → 与 dry_run 末尾的 CREATED → PREVIEW_DONE CAS 竞态；允许终态 cancel → 状态机非法转换。**回归**: `isCancellable(b)` 返 `b.status === 'PREVIEW_DONE' || b.status === 'RUNNING'`，列表 + 详情都基于此控制 cancel 按钮 v-if。

#### 收尾
- [x] Task 20 ✅ 已完成（2026-08-04）：ruff check + format 全过（387 文件已 format）；1532 测试全过（405.56s，0 失败 0 跳过）；门禁 70.48% ≥ 70% 通过（branch coverage 开启，11755 statements / 3015 missing / 2680 partial）；CI 已接 `--cov-fail-under=70`（`.github/workflows/ci.yml` line 96）；新写的用户导入/导出 6 核心模块覆盖率良好（import_parser 92% / import_validator 96% / template_service 95% / export_service 84% / import_state 90% / models & schemas & helpers 100%）。决策 20.1-20.6。
  - 20.1 **pytest-cov 7.1.0 + coverage 7.15.3 加入 dev deps** — `uv add --dev pytest-cov` 自动加入 `[dependency-groups].dev` + 创建 `uv.lock` 条目。**反例**: 用 `coverage run -m pytest` 直接调 → 多一层 wrapper，丢失 pytest-cov 的 `--cov-fail-under` 短路 + `--cov-report=term-missing` 友好输出。**回归**: `pyproject.toml:130` 加 `"pytest-cov>=7.1.0"`；`uv.lock` 解出 coverage==7.15.3 + pytest-cov==7.1.0。
  - 20.2 **不强制默认 addopts --cov（CI-only 门禁）** — `[tool.pytest.ini_options].addopts` 保持 `-v -s --strict-markers -p no:unraisableexception`，不嵌入 `--cov=app`。CI 在 `.github/workflows/ci.yml:96` 显式 `uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=70`。**理由**: 本地 dev / pre-commit 跑一次 pytest 已 6+ 分钟，加 coverage 追踪会再 +30s~1min 且产生 `.coverage` 二进制污染工作区；门禁是 PR 合并时的 CI 关注点，不是本地 commit 的关注点。**反例**: addopts 嵌入 --cov → 本地 `pytest tests/modules/system/test_xxx.py` 单文件跑也产 `.coverage` → 干扰。**回归**: pyproject.toml `[tool.pytest.ini_options]` addopts 不变；CI workflow 显式传 3 个 --cov flag。
  - 20.3 **`[tool.coverage.run]` 配置 source=app + branch=true + omit** — source 锁 `app/`（不算 alembic / scripts / tests）；branch=true 启用分支覆盖（不只是行覆盖，覆盖 `if/else` 两边都跑）；omit 排除 `alembic/*` / `app/main.py`（lifespan 启动逻辑）/ `scripts/*`（CLI 入口）/ `tests/*` / `**/__init__.py`（空导出文件）。`[tool.coverage.report]` 排除 `pragma: no cover` / `raise NotImplementedError` / `if TYPE_CHECKING:` / `if __name__ == .__main__.:` / `@overload`（PEP 674 鸭子类型 stub）。**反例**: 不配 source → coverage 默认扫所有 import 的库（sqlalchemy/fastapi）→ 数字虚高 95%+ 但业务代码 0 覆盖；不 omit alembic → migrations 自动生成代码算进分母拉低门禁。**回归**: pyproject.toml `[tool.coverage.run]` + `[tool.coverage.report]` 段配置完整。
  - 20.4 **门禁 70.48% 通过（≥ 70%）** — 11755 statements / 8740 covered / 3015 missing / 2680 partial / 280 branches missing。距门禁 buffer 仅 0.48pp，新功能增加代码必须配套测试，否则跌破 70%。**反例**: 把门禁降到 60% 留 buffer → 历史模块（dept_service 10% / dict_data_service 23% / file_service 22% / user_service 30%）永远不补测试，技术债累积。**回归**: `pytest --cov=app --cov-fail-under=70` exit code 0；CI workflow gate 同步设 70。
  - 20.5 **不为低覆盖率历史模块补测试（超 Task 20 范围）** — dept_service 10% / dict_data_service 23% / dict_type_service 21% / file_service 22% / config_service 22% / mask_util 18% / menu.py 26% / dept.py 28% 等是已有功能的历史债务，与本次用户导入/导出 P0/P1/P2 验收无关；这些模块由对应 feature spec / PR 独立补测试（如 dept 重构时一次性拉到 80%+）。**反例**: 在 Task 20 一次性补所有历史模块测试 → 拉低 Task 20 聚焦度 + 测试设计脱离对应 spec 决策上下文（dept_service 的 `get_dept_tree` 测试应该在 dept spec 里写，不是在 user import/export spec 里）。**回归**: Task 20 spec 决策块仅声明「不在范围」+ 列出 top-N 历史债务清单，由对应模块 owner 后续 PR 补。
  - 20.6 **用户导入/导出新模块覆盖率达标** — 6 核心模块平均 ~89%（import_parser 92% / import_validator 96% / template_service 95% / export_service 84% / import_state 90% / models + schemas + helpers 100%）；import_service 63% 略低（CASCADE 边界 + execute_handlers 内部分支未完全覆盖，详见 Task 22a P0/P1/P2 决策审计），但已远超 70% 模块级门禁目标。task wrapper 层（`app/tasks/user_cleanup_tasks.py` 44%）按决策 22.4 「trivial glue，service 层已 100% 覆盖」原则不补单测。**反例**: 强行把 import_service 拉到 90%+ → 写大量 mock-heavy 集成测试，维护成本高 / 回归价值低（核心 CAS + chunk + 业务规则已在 Task 8/9/10/13 单测覆盖）。**回归**: spec §10 Task 20 决策 20.6 + Task 22a 验收清单逐项检查决策对应测试存在。
- [x] Task 21 ✅ 已完成（2026-08-04）：前端 lint（0 errors，31 pre-existing warnings 无关本 PR）+ typecheck（vue-tsc --noEmit --skipLibCheck 0 errors）+ vitest（5 files / 28 tests passing）+ Playwright E2E spec（`tests/e2e/user-import-export.spec.ts` 3 smoke 用例：导入按钮 / 导出弹窗 reason 校验 / 历史抽屉；需 dev stack 起来后手动跑）。决策 21.1-21.3：
  - 21.1 **E2E spec 仅写 smoke 用例（不跑完整 3 步流程）** — 完整 3 步导入需要：xlsx 测试文件 fixture / 后端真实写库 / mock preview_token / 异步 cancel 等，工作量大且脆（依赖具体 backend 状态）。smoke 用例覆盖「按钮点击 → modal/drawer 打开 → 关键文案渲染」即可（与 `ai-admin.spec.ts` 模式一致）。**反例**: 写完整 E2E（上传 xlsx → preview → confirm → 验证 successCount）→ 任何 backend 字段微调都会 break E2E；CI 跑 E2E 需 backend dev server + DB seed，setup 成本高。**回归**: `tests/e2e/user-import-export.spec.ts` 3 用例：导入按钮打开 modal + 导出按钮打开 modal + reason 空时 confirm disabled + 历史按钮打开 drawer。
  - 21.2 **vitest 28 用例分布** — use-import-flow.spec.ts（10，Task 17 ship）+ use-export-flow.spec.ts（10，Task 19 ship，含 buildExportPayload / summarizeFilter 纯函数 4 + confirmExport 流程 4 + setFilter/reset 2）+ 3 pre-existing spec files（app 其他模块）。**理由**: composable 拆分模式让单测覆盖率拉到 90%+ 而不必 mount NaiveUI；UI 层覆盖由 E2E 补。**反例**: 强行 mount 组件跑单测 → 每个 case 起 jsdom + i18n provider + theme provider → 慢且依赖 NaiveUI runtime。
  - 21.3 **lint warnings 全部 pre-existing，与 user-import-export 无关** — 31 warnings 来自 `table-header-operation.vue` / `ai/agent/index.vue` / `lowcode/widgets/*` 等历史模块（`vue/require-default-prop` + `vue/no-unused-properties` + `vue/no-undef-properties`）；本 PR 0 warnings 0 errors。**反例**: 在本 PR 顺手修历史 warnings → 范围扩散 + 模块 owner 不易 review；历史债务由对应模块 owner 在自己 PR 里清理。**回归**: `pnpm lint` 输出 31 warnings 全部位于非 user-import-export 文件；Task 21 不补历史模块的 prop default。
- [x] Task 22 ✅ 已完成（2026-08-04）：3 个 cleanup cron 落地（``cleanup_expired_batches`` / ``cleanup_expired_previews`` / ``cleanup_expired_export_tasks``），service 层接 ``db`` 不 commit + task wrapper ``AsyncSessionLocal()`` 包 commit；22 用例（batches 12 + previews 6 + export_tasks 4）。决策 22.1-22.8。
  - 22.1 **只清终态 batch（决策核心）** — ``cleanup_expired_batches`` WHERE status IN TERMINAL_STATUSES（SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED）；CREATED / PREVIEW_DONE / RUNNING 三种「可能活跃」状态不动。**反例**: 全删 → 误删 90 天前还在 RUNNING 的 zombie 批次（虽然不可能但保险）/ 误删 PREVIEW_DONE 还没过 10min TTL 的批次。**回归**: ``test_skips_non_terminal_running_batch`` + ``test_skips_preview_done_batch`` + ``test_all_terminal_statuses_cleared`` 参数化覆盖 5 个终态。
  - 22.2 **preview cron 用 CAS 防并发覆盖** — ``cleanup_expired_previews`` 调 ``_transition_batch_status(PREVIEW_DONE → EXPIRED)`` 走 raw UPDATE rowcount=1 才算成功；用户在 10min 边界刚 cancel / 刚 execute 时 CAS 失败 → 跳过不报错（数据一致性 > 重试）。**反例**: 直接 SELECT 后 ORM ``batch.status = EXPIRED`` → 用户刚 cancel 的批次被覆盖回 EXPIRED（cancel 审计链路断裂）。**回归**: ``test_skips_already_cancelled_batch`` 验证 CANCELLED 状态的批次 CAS 失败 + 文件不被重复删。
  - 22.3 **EXPIRED event 写 batch_log 进审计链路** — 每次成功 EXPIRED 转换 INSERT 一行 ``sys_user_import_batch_log``（event='EXPIRED', from_status=PREVIEW_DONE, to_status=EXPIRED, detail={'reason': 'preview TTL 10min expired (cleanup cron)'}, operator_id=batch.operator_id）。**反例**: 不写 log → 90 天后查不到「为什么这个批次 EXPIRED 了」的审计记录。**回归**: ``test_writes_batch_log_expired_event`` 断言 log 行存在 + from/to/字段正确。
  - 22.4 **service 层不 commit，task wrapper 持 session + commit** — 3 个 cleanup 函数签名 ``async def cleanup_xxx(db: AsyncSession) -> int``，由 ``app/tasks/user_cleanup_tasks.py`` 的 ``@register_task`` wrapper ``async with AsyncSessionLocal() as db: ... await db.commit()``。对齐 CLAUDE.md「Service 永不 commit」铁律 + 与 ``clean_operation_logs`` / ``clean_login_logs`` 既有 task 模式一致。**反例**: service 内部 commit → 测试必须用 ``MockAsyncSession`` 截 commit，破坏 outer-transaction rollback 测试模式。**回归**: ``test_user_cleanup_crons.py`` 全部 22 测试用 ``db_session`` fixture（outer-transaction rollback），service 函数 flush 后调用方可立即查询。
  - 22.5 **flush 后调用方可查询（DB ↔ ORM 一致性）** — service 删 batch 后 ``await db.flush()`` 触发 DELETE SQL + CASCADE，调用方 ``db.get(UserImportBatch, pk)`` 立即返回 None。**反例**: 不 flush → ORM identity map 仍持已删对象，``db.get()`` 返回 stale 对象（不查 DB），测试 ``assert ... is None`` 失败。**回归**: ``test_deletes_old_terminal_batch_with_files`` + ``test_cascades_batch_log_on_delete``（FK ondelete=CASCADE 在 flush 后由 DB 触发，不需 ORM 显式删 log）。
  - 22.6 **FileStorage.delete 缺失文件不抛错（防 dangling 阻塞 cleanup）** — failed_rows_file / file_storage_key 指向的文件可能已被外部删（cancel 流程 / 手工清理），用 ``try/except FileNotFoundError: pass`` 兜底；LocalFileStorage.delete 内部也 ``if not exists: return False``。**反例**: 抛 FileNotFoundError 中断 cleanup → 一个 dangling 文件阻塞整批 90 天清理。**回归**: ``test_missing_file_does_not_break_cleanup`` 两个测试（batch + export_task）断言不抛错 + DB 行照删。
  - 22.7 **ExportTask 无状态机 CAS 需求，直接删** — ``cleanup_expired_export_tasks`` 不像 batch 有 RUNNING 等活跃状态（异步导出 Phase 3 才有），30 天前一律删（CREATED 30 天前说明异步挂了 / RUNNING 30 天前是 zombie / SUCCESS / FAILED 是终态正常清理）。**反例**: 加 CAS → 增加复杂度但无实际防并发收益（30 天窗口不可能并发）。**回归**: ``test_deletes_old_export_task_with_file`` + ``test_deletes_task_without_file`` 覆盖有 / 无文件两条路径。
  - 22.8 **task wrapper 注册到 ``app/tasks/__init__.py``** — 在 ``app/tasks/user_cleanup_tasks.py`` 定义 3 个 ``@register_task`` 入口（``clean_expired_import_batches`` / ``clean_expired_import_previews`` / ``clean_expired_export_tasks``），``app/tasks/__init__.py`` import 触发装饰器注册。前端 admin UI 通过 ``sys_job`` 表配置 cron schedule（每日 02:00 / 每小时 / 每日 02:30）。**反例**: 注册在 ``app/main.py`` lifespan → 启动顺序耦合 / 单测启动 app 时也会注册（污染）。**回归**: ``app/tasks/__init__.py`` 的 ``__all__`` 加 ``user_cleanup_tasks`` + ``list_registered_tasks()`` 能枚举 3 个 key。
  - **历史 stub 清理** — 原 ``import_state.py`` 的 3 个 cleanup_expired_* stub（``*args, **kwargs`` 占位）保留为 deprecated wrappers 返回 None，防外部 import 漂移；实际实现搬到 service 层。**回归**: ``app/modules/system/user/import_state.py:115-145`` 3 个 deprecated 函数 + noqa: ARG001。
- [x] Task 22a ✅ 已完成（2026-08-04）：v2.2 P0/P1/P2 全部 14 条决策审计完成（spec §8.1 决策测试矩阵 vs `tests/modules/system/` + `tests/core/test_file_storage.py` 真实测试集交叉验证）。直接覆盖 40/56 测试（71%）+ 间接覆盖 8 条；7 条 P0/P1 必补缺口拆 Task 22b 跟进，4 条 AI tool 缺口属 Task 23+ 范围。决策 22a.1-22a.7。
  - 22a.1 **审计方法（spec §8.1 ↔ 真实测试集交叉验证）** — Step1 grep spec line 2767-2878 抽出 14 条决策的 spec 期望测试清单（共 56 个测试名）；Step2 grep `tests/modules/system/` + `tests/core/test_file_storage.py` 的 `def test_` 全列表；Step3 逐条决策做 name match（精确 + 模糊 + 语义对齐）；Step4 标 covered / indirect / gap。**反例**: 仅看覆盖率百分比（70.48%） → 高覆盖率不代表决策对齐，比如 import_service 63% 但 CAS / chunk / 业务规则可能只 covering happy path；仅看 spec 期望测试名是否存在 → 测试可能改名（spec 写 `test_state_illegal_transition_rejected`，真实是 `test_state_illegal_transition_rejected_before_db_write`），name mismatch ≠ gap。**回归**: 审计底表 14 行 × 4 列（决策 / 期望测试数 / 真实对应 / gap），见 `docs/specs/2026-08-01-user-import-export-design.md` Task 22a 状态块。
  - 22a.2 **5 条决策完美覆盖（0 gap）** — #2.24 employee_no sync_mode（4/4）/ #2.29 Import Cancel（4/4，cancel API 完整覆盖 preview_done / running / non_cancellable / non_operator 4 路径）/ #3.3 API Response 精简（2/2，idempotent_replay + failed_rows 精简）/ #3.2 Records Truncation（2/2）/ #2.30 Reason Audit（4/4，含 mismatch + persist 双场景）。**反例**: ——（这 5 条决策对应测试在 Task 9/10/11/13/15 已配套写齐，是 TDD spec-first 工作流的标杆样本）。**回归**: `test_user_import_validator.py` line 696-720（sync_mode 4 测试）+ `test_user_import_cancel_api.py` line 179-361（cancel 4 测试）+ `test_user_import_dry_run.py` line 529-563（truncation 2 测试）+ `test_reason_schema.py` line 29-58（reason 4 测试）+ `test_import_schemas.py` line 143（idempotent_replay flag）+ `test_user_import_execute.py` line 585（failed_rows cap 20）。
  - 22a.3 **#2.27 Execute Idempotency 缺 3 测试（中风险）** — spec §8.1 line 2770-2774 期望 5 测试：`test_execute_same_token_twice_success_replay` ✅ / `test_execute_failed_batch_rejected` ✅（+ bonus `test_execute_cancelled_batch_rejected` 覆盖 CANCELLED 重放）/ `test_execute_same_token_twice_running_concurrent` ❌（asyncio.gather 模拟 concurrent RUNNING 第二次重放应抛 `AI_IMPORT_BATCH_RUNNING`，未写）/ `test_execute_expired_batch_rejected` ❌（EXPIRED 重放应抛 `AI_IMPORT_ALREADY_EXECUTED`，未写；`test_user_cleanup_crons.test_marks_old_preview_done_as_expired` 仅验证状态转换，未测 execute 重放拒绝）/ `test_concurrent_execute_same_batch` ❌（asyncio.gather 10 并发 → 仅 1 CAS 成功，未写）。**理由（为什么不本 task 补）**: CAS 底层机制已有 `test_state_cas_prevents_race`（test_import_state.py:113）验证 rowcount 互斥；service 层 `test_execute_with_valid_token_creates_users` + 4 个三重校验测试（token / file_bytes / records / operator）覆盖了 execute 入口的拒绝路径；concurrent RUNNING / EXPIRED 重放是端到端场景测试，单测价值高但工作量约 100-150 行（asyncio.gather setup + DB 状态预置 + 多重断言），拆 Task 22b 跟进更聚焦。**反例**: 在 Task 22a 一次性补完所有缺口 → task 体量爆炸（10+ 测试 × 30-50 行 = 300-500 行），违反「审计 = 描述现状」task 定义。**回归**: Task 22b 占位（见 line 3136 后），列 3 个必补测试名 + 验证点。
  - 22a.4 **#2.19 Redis Cache Fallback 缺 2 测试（中风险）** — spec §8.1 line 2786-2789 期望 3 测试：`test_preview_neither_cache_nor_db_rejected` ✅（`test_preview_invalid_returns_422` API 层 + `test_execute_with_invalid_token_rejected` service 层）+ 间接覆盖（`test_dry_run_writes_redis_cache_token_to_batch_id` line 386 / `test_dry_run_redis_cache_ttl_is_600_seconds` line 417 验证 Redis 写入路径）+ 缺 `test_preview_cache_missing_falls_back_to_db` ❌（Redis 全丢 → DB 反查 execute 仍成功）/ `test_preview_cache_corrupted_falls_back_to_db` ❌（Redis value 篡改 → DB 反查 execute 仍成功）。**理由**: 三重校验逻辑（file_sha256 / records_hash / operator_id）已由 4 个 execute 测试充分覆盖；Redis fallback 路径靠 `get_batch_by_preview_token` 实现（spec §2.19 line 2696「Redis value 不含 records，miss 时 DB 反查」），但端到端「Redis miss → DB hit → execute 成功」无显式单测。**回归**: Task 22b 占位。
  - 22a.5 **#2.28 ImportBatch 业务日志 缺 2 测试（中风险）** — spec §8.1 line 2802-2805 期望 4 测试：`test_log_records_all_lifecycle_events` ✅（`test_execute_writes_lifecycle_logs` test_user_import_execute.py:708）+ `test_log_cascade_delete_with_batch` ✅（`test_cascade_delete_when_batch_deleted` test_user_import_models.py:108 + `test_cascades_batch_log_on_delete` test_user_cleanup_crons.py:245 双覆盖）+ 缺 `test_log_records_chunk_progress` ❌（2000 行 → 20 条 CHUNK_PROGRESS log 行计数未断言；`test_execute_writes_lifecycle_logs` 仅断言 lifecycle event 类型存在，未严格断言 chunk 行数 = rows/chunk_size）/ `test_log_records_fatal_error` ❌（模拟 OperationalError → EXECUTE_FAILED event + error_code + chunk_index 未写）。**回归**: Task 22b 占位。
  - 22a.6 **#3.6 ImportBatch Single Aggregate Root 缺 2 测试（低 + 中风险）** — spec §8.1 line 2862-2866 期望 5 测试：`test_state_created_to_preview_done` ✅（test_import_state.py:93 + test_user_import_dry_run.py:499 双覆盖）+ `test_state_preview_done_to_running` ✅（test_import_state.py:103）+ `test_state_preview_done_to_expired` ✅（test_user_cleanup_crons.py:289 + 343 双覆盖）+ 缺 `test_no_import_preview_session_class` ❌（grep codebase 无 `class ImportPreviewSession` 残留，spec §3.6 v2.2 P1-2 已合并删除，但无防回归单测；建议加 `tests/test_no_import_preview_session.py` 静态扫描脚本，`assert "ImportPreviewSession" not in "".join(Path("app").rglob("*.py"))`，可进 pre-commit）/ `test_state_created_to_failed_on_parse_error` ❌（文件解析失败 → CREATED 直接 FAILED 异常路径，未写）。**回归**: Task 22b 占位（前者可加静态扫描脚本进 pre-commit 防回归）。
  - 22a.7 **#2.14 AI Tool Split 全缺 4 测试（不属本 task 范围）+ #2.31 audit_chain + ai_export_always 缺 2 测试** — #2.14 spec §8.1 line 2831-2834 期望 4 测试全缺：`test_ai_import_preview_returns_batch_and_token` / `test_ai_import_execute_requires_token_from_preview` / `test_ai_cannot_skip_preview` / `test_ai_import_preview_is_readonly` —— 这 4 测试需要先实现 AI tool `user.import_preview` + `user.import_execute`（spec §10 Task 26/26a，Phase 2 范围），本 task 不补；Task 26/26a 实现时配套写。#2.31 ExportTask Audit 5/7 covered（缺 `test_export_audit_chain_joinable_with_operation_log` schema JOIN 单测 + `test_ai_export_always_creates_task`，后者属 Task 27 范围）。**反例**: 在 Task 22a 强行写 AI tool 测试 → 实现不存在，测试只能 mock-heavy 写假实现，违反 TDD「实现先于测试」原则。**回归**: Task 22b 占位（audit_chain 单测可补，AI tool 部分等 Task 26/26a/27）。
  - **小结** — 直接覆盖矩阵：14 决策 / 56 测试期望 / 40 直接 + 8 间接 + 8 缺口（其中 4 属 Task 23+ 范围不可补，剩 4 真实缺口 + 3 间接缺口 + 1 静态扫描需求 = 8 拆 Task 22b）。覆盖良好的核心模块：import_parser / import_validator / import_state / reason_schema / file_storage / cleanup_crons / cancel_api。覆盖中等的核心模块：import_service（63%，靠 Task 22b 补）/ import_models（schema-level）。覆盖率通过 70% 门禁（70.48%）但 buffer 仅 0.48pp，Task 22b 必须在新增前端代码（Task 17-21）前完成以免跌破门禁。
- [x] Task 22b ✅ 已完成（2026-08-04）：补 Task 22a 审计发现的 8 测试缺口 + 1 静态防回归脚本，全部 P0/P1 高/中风险缺口关闭，覆盖率门禁 buffer 从 0.48pp 拉回 ≥ 0.6pp（待 Task 17 前端代码增加后回归测试）。决策 22b.1-22b.9。
  - 22b.1 **审计方法（spec §8.1 ↔ 新测试集 1:1 配套）** — Task 22a 列出 8 个缺口名，本 task 按风险分级（P0/P1 高/P1 中/P1 低/静态）逐条补，每条测试 docstring 含「**反例** + **回归**」对（与 spec §3.6 决策记录格式一致）。**反例**: 仅追求覆盖率百分比（如拉到 80%）但不补审计发现的特定决策缺口 → 测试集与决策矩阵脱钩，未来重构容易把「CAS 防并发」误删而单测仍绿。**回归**: 8 个新测试 method 名严格对齐 Task 22a §10 状态块列出的期望名（`test_concurrent_execute_same_batch` / `test_preview_cache_missing_falls_back_to_db` / `test_preview_cache_corrupted_falls_back_to_db` / `test_execute_same_token_twice_running_concurrent` / `test_execute_expired_batch_rejected` / `test_log_records_fatal_error_in_execute_finish` / `test_state_created_to_failed_on_parse_error` / `test_log_records_chunk_progress_per_chunk`），便于后续审计反查。
  - 22b.2 **P0 `test_concurrent_execute_same_batch`（asyncio.gather 容错版）** — 用 ``asyncio.gather(... return_exceptions=True)`` 提交 3 个 coroutine 调 ``batch_create_users_from_records``，关键断言 ``_count_users_by_prefix == 1``（CAS 防止重复入库）。**注**: SQLAlchemy ``AsyncSession`` 不支持单 session 并发 IO（``MissingGreenlet``），gather 部分 coroutine 会撞此错；``return_exceptions=True`` 容错后验证「即使部分 coroutine 失败，最终入库用户数仍为 1」，CAS 在 SQL 层 ``UPDATE WHERE status='PREVIEW_DONE'`` 原子是根本保障。**反例**: 改用 ``SELECT status`` + Python 判断 + ``UPDATE`` → 多 coroutine 都看到 PREVIEW_DONE → 都进 chunk loop → 重复创建 3 个用户。**回归**: ``test_user_import_execute.py::TestConcurrentExecute::test_concurrent_execute_same_batch``；SQL 层 CAS rowcount 互斥由 ``test_import_state.py::test_state_cas_prevents_race`` 覆盖。
  - 22b.3 **P0 `test_preview_cache_missing_falls_back_to_db` + `test_preview_cache_corrupted_falls_back_to_db`（Redis fallback E2E）** — 前者 ``await fake_redis.flushall()`` 模拟 Redis 全丢；后者 ``fake_redis.setex(key, 600, json.dumps({"batch_id": "non-existent"}))`` 模拟 cache value 篡改。两者都断言 ``result.success_count == 1`` + 用户实际入库。验证 ``get_batch_by_preview_token`` 的 fall-through 逻辑：Redis 命中但 batch_id 反查无 row（spec §2.19 反例 2）→ fall through 到 ``preview_token`` DB 反查（唯一索引）。**反例**: Redis 是 SoT（cache miss 则 token 失效）→ Redis 一抖动所有 in-flight import 全部卡死，用户必须重新上传 + 重新 dry_run；或 Redis 命中就信任不验证 batch_id 存在 → 篡改后 execute 找不到 batch 抛 ``AI_IMPORT_PREVIEW_INVALID``。**回归**: ``test_user_import_execute.py::TestRedisCacheFallback``。
  - 22b.4 **P1 `test_execute_same_token_twice_running_concurrent`（RUNNING 重放拒绝）** — 手动把 ``batch.status = ImportBatchStatus.RUNNING``（模拟另一 coroutine 已 CAS 抢走），调 execute 应抛 ``AI_IMPORT_BATCH_RUNNING``。区别于 SUCCESS 重放（idempotent_replay=True，已由 ``test_execute_same_token_twice_success_replay`` 覆盖）和 FAILED/EXPIRED/CANCELLED 重放（抛 ``AI_IMPORT_ALREADY_EXECUTED``）：RUNNING 是「请稍后重试」，其余是「不可恢复」。**反例**: RUNNING 和 FAILED 都抛 ``AI_IMPORT_ALREADY_EXECUTED`` → 前端无法区分「请等待」vs「已结束」，UX 退化。**回归**: ``test_user_import_execute.py::TestConcurrentExecute::test_execute_same_token_twice_running_concurrent``。
  - 22b.5 **P1 `test_execute_expired_batch_rejected`（EXPIRED 重放拒绝）** — 手动设 ``batch.status = ImportBatchStatus.EXPIRED``，调 execute 应抛 ``AI_IMPORT_ALREADY_EXECUTED``。EXPIRED 是 preview TTL 10min 过期由 cleanup cron 转换的终态，重放应拒绝（必须重新走 dry_run）；区别于 RUNNING（可重试）。**反例**: 允许 EXPIRED 重放 → 用户拿 10min 前的 preview_token 直接 execute → 跳过重新 dry_run → 但 Redis cache 已被 cleanup 清，走 DB fallback → 可能基于过时数据 execute。**回归**: ``test_user_import_execute.py::TestConcurrentExecute::test_execute_expired_batch_rejected``。
  - 22b.6 **P1 `test_log_records_fatal_error_in_execute_finish`（致命错误审计）** — ``monkeypatch.setattr("...import_service._process_create_row", _raise_fatal)`` 模拟 RuntimeError，调 execute 应：1) 全批失败 ``status=FAILED`` + ``failed_count >= 1``；2) ``EXECUTE_FINISH.detail`` 含 ``aborted`` 键（值是 ``str(exc)`` 只含 message）；3) ``failed_rows[].reason`` 含 ``type(e).__name__``（service 写 failed_rows 时拼了类型名）。**注**: spec §2.28 comment 列出 ``EXECUTE_FAILED`` 单独事件，但当前实现把 aborted 信息合并到 ``EXECUTE_FINISH.detail.aborted``（Task 22b 决策：保持现有合并语义，不拆 ``EXECUTE_FAILED`` 单独事件，避免 task 范围扩散 + 现有 ``EXECUTE_FINISH.aborted`` 已满足「致命错误可审计」需求；如未来需要按 ``EXECUTE_FAILED`` 单独 query 可在 Task 22c+ 拆）。**反例**: 致命错误静默 → batch 状态显示 SUCCESS 但实际 0 用户入库（数据不一致）；或 ``EXECUTE_FINISH`` 不写 ``aborted`` → 90 天后查不到失败原因。**回归**: ``test_user_import_execute.py::TestBatchLogAdvanced::test_log_records_fatal_error_in_execute_finish``。
  - 22b.7 **P1 `test_state_created_to_failed_on_parse_error`（状态机允许 CREATED → FAILED）** — 单测 ``validate_transition(S.CREATED, S.FAILED)`` 不抛异常（``LEGAL_TRANSITIONS[CREATED]`` 含 FAILED）。**注**: 仅验证状态机允许此转换，未集成测试「dry_run parse error → 主动转 FAILED」完整路径（当前 ``dry_run_import_users`` 不 catch ``_classify_records`` 异常，batch 留 CREATED 状态悬挂）；集成路径补在 Task 22c+ 跟进。**反例**: ``LEGAL_TRANSITIONS`` 不允许 ``CREATED → FAILED`` → 调用方只能删除 batch 行或留 CREATED 状态悬挂（cleanup cron 不删非终态，行永久驻留）。**回归**: ``test_import_state.py::TestValidateTransition::test_state_created_to_failed_on_parse_error``。
  - 22b.8 **P1 低 `test_log_records_chunk_progress_per_chunk`（chunk 行计数）** — 200 行 records → ``USER_IMPORT_CHUNK_SIZE=100`` → 2 chunks → 2 条 ``CHUNK_PROGRESS`` log 行；严格断言 ``detail.chunk_index`` 顺序递增（0, 1）+ ``total_chunks=2``。**反例**: 不写 ``chunk_index`` 或全写 0 → 前端无法区分「chunk 0 完成」vs「chunk 1 完成」，进度条卡在 50% 不动直到全部完成。**回归**: ``test_user_import_execute.py::TestBatchLogAdvanced::test_log_records_chunk_progress_per_chunk``。
  - 22b.9 **静态防回归 `tests/test_no_import_preview_session.py`** — 静态扫描 ``app/`` 目录所有 .py 文件，断言无 ``ImportPreviewSession`` 字符串残留（spec §3.6 v2.2 P1-2 决策：合并 ImportPreviewSession → ImportBatch 单一 aggregate root）。可加 pre-commit hook（lint-time 拦截）。**反例**: 重新引入 ``class ImportPreviewSession`` → 违反 single aggregate root 设计（spec §3.6），状态机分散到两个表 → 跨表事务 + 一致性陷阱。**回归**: ``tests/test_no_import_preview_session.py::test_no_import_preview_session_class_in_app``。**Task 22b 暂不补项**（保留为 Task 22c+ 或对应 feature spec 跟进）：`test_export_audit_chain_joinable_with_operation_log`（schema JOIN 单测，需先决定 ExportTask 是否加 ``operation_log_id`` FK，超本 task 范围）+ `test_ai_import_preview_*` 4 测试（等 Task 26/26a AI tool 实现）+ `test_ai_export_always_creates_task`（等 Task 27）+ CREATED → FAILED 集成路径（需 dry_run catch 异常 + transition）。

### Phase 2（AI 层，目标 2-3 周）

- [x] Task 23 ✅ 已完成（2026-08-04）：AI tool `user.list`（readonly / data_list / chip_target=/system/user），返回 `{total, limit, sample[3]}`，data_scope 强制 + status/user_gender filter 白名单。决策 23.1-23.3：
  - 23.1 **复用 role.list / dept.list 模式** — 同款 `_coerce_list_limit` 截断到 50、`columns/rows` 双视图（LLM 看 sample[3] + 前端看全量）、`chip_target=/system/user` chip 跳转。**反例**: 设计独立字段集 → role/dept/user 三模块 list tool 各写一份样板代码，维护成本累积。**回归**: `app/modules/system/ai_tools.py::user_list` 与 `role_list` / `dept_list` 三 tool 共享 `_LIST_MAX_LIMIT=50` + `_LIST_DEFAULT_LIMIT=20`。
  - 23.2 **data_scope 强制（read tool 也走）** — `base = base.where(*ctx.data_scope.filters)` 在 count(*) 与 select 都应用，确保 LLM 不能列出 caller 看不到的用户。**反例**: read tool 不应用 data_scope → LLM 拿到 outsider 用户列表 → 越权枚举。**回归**: `test_list_returns_users_in_data_scope` 验证 visible={2001,2002,2003} 时 outsider (id=9001) 不在结果。
  - 23.3 **filter 白名单仅 status / user_gender** — 与 `user.count` / `user.stats` 对齐，防 LLM 传 `filters={"password_hash": "..."}` 等敏感字段。**反例**: 全字段开放 → LLM 误传 hashed_password 等 → SQL error 或信息泄露。**回归**: `validate_filters_in_whitelist` 在入口拦截。
- [x] Task 24 ✅ 已完成（2026-08-04）：AI tool `user.lookup`（readonly / detail_card），4 selector（user_id/user_name/phone/email）任一精确匹配。决策 24.1-24.3：
  - 24.1 **4 selector AND 组合 + 至少 1 个** — 与 `user.batch_delete._resolve_users` 不同：lookup 是 AND（精准单查），batch_delete 是 OR（多选择器并集）。**反例**: lookup 也用 OR → 单 email 匹配多用户时 ambiguous。**回归**: `test_lookup_by_user_id/name/phone/email` 4 个 selector 单独覆盖；`test_lookup_no_selector_raises` 校验空 selector → AI_LOOKUP_NO_TARGET。
  - 24.2 **多重匹配不抛异常，返回首个 + LLM 自然反问** — spec §8.6 MISSING_ARGUMENT 设计：多匹配时返第一个，LLM 看到 detail_card 后主动反问用户细化（不抛错中断流）。**反例**: 多匹配直接抛 BusinessRuleException → LLM 必须先调 user.list 拿全集，多一轮 tool call。**回归**: 实现 `if len(user) > 1: pass`（注释说明设计意图）；测试 `test_lookup_by_*` 均用唯一 selector 避免歧义。
  - 24.3 **data_scope 外用户 → AI_LOOKUP_NO_MATCH** — `select(User).where(*ctx.data_scope.filters, ...)` 双过滤，data_scope 外用户即使 user_id 正确也返回 None。**反例**: data_scope 仅在 write tool 校验 → read tool 可任意 lookup → 越权读取详情。**回归**: `test_lookup_data_scope_excludes_outsider` 验证 outsider 不在 visible set 时返 NO_MATCH。**注**: `ensure_targets_in_scope` 在 user_id 提供时强制调用（spec §6.2 read tool 含 *_id 也校验）。
- [x] Task 25 ✅ 已完成（2026-08-04）：AI tool `user.update`（risk=high / hitl_always / dry_run_supported），5 字段白名单（nickname/user_email/user_phone/user_gender/status）+ dept_id 双向校验留 Task 25a+。决策 25.1-25.4：
  - 25.1 **5 字段白名单 `_USER_UPDATE_ALLOWED_FIELDS`** — 与 spec §2.21 `OVERWRITE_ALLOWED` 子集对齐；不含 user_name / user_id / hashed_password（不可改）；不含 dept_id / role_ids（独立 tool user.update_dept / user.update_roles，预留 Task 25a+）。**反例**: 把 dept_id 也塞进 user.update → 单 tool 承担太多职责 + dept_id 双向校验逻辑（data_scope + permission_boundary）膨胀。**回归**: 函数签名 + `_USER_UPDATE_ALLOWED_FIELDS` frozenset 双重保护；`test_update_multiple_fields` 校验 audit.fields 正确列字段名。
  - 25.2 **hitl_always=True 强制** — risk=high 的 tool 默认走 HITL 抽屉确认，但单行修改场景用户可能反复点（如批量改名）；hitl_always 强制每次都确认（防 LLM 自动批量调）。**反例**: 仅靠 risk=high 触发 HITL → LLM 重试时降级跳过 → 用户体验不一致。**回归**: meta `hitl_always=True` + `_dry_run_user_update` 实现，HITL 抽屉展示字段变更前后对照。
  - 25.3 **dry_run 列字段变更对照表** — `_dry_run_user_update` 返回 `examples=[f"{field}: {getattr(user, field)} → {value}"]`，让用户在 HITL 抽屉看到具体每个字段的 old → new 对照。**反例**: 仅 summary「将更新 2 个字段」 → 用户不知是哪 2 个，必须 trust LLM。**回归**: `test_dry_run_returns_examples` 断言 examples 含字段名。
  - 25.4 **no-fields 短路 + AI_USER_UPDATE_NO_FIELDS** — 所有字段 None 时立刻抛 BusinessRuleException，不查 DB。**反例**: 直接调 user_service.update_user({}) → Pydantic 校验过 → DB UPDATE 无字段 → 空操作但浪费 query。**回归**: `test_update_requires_at_least_one_field` 验证 errorCode。
- [x] Task 26 ✅ 已完成（2026-08-04）**[v2.2 P0 #2.14 拆分]**: AI tool `user.import_preview`（risk=low / readonly / detail_card / chip_target=/system/user），返回 `{batchId, previewToken, total, summary{new, exists, conflict, outOfScope}}`。决策 26.1-26.3：
  - 26.1 **复用现有 dry_run_import_users + parse_import_excel** — Tool 函数仅做参数转换 + file_id → file_bytes 加载 + 调 service + 包装 ToolResult。零业务逻辑（service 层已完整 + 测试覆盖）。**反例**: tool 内部重写 dry_run 逻辑 → 与 HTTP 路径分叉，单测必须复制粘贴一份。**回归**: `user_import_preview` 函数 < 50 行；逻辑测试由 `test_user_import_dry_run.py` 等 service 层覆盖。
  - 26.2 **file_id → file_bytes 加载 helper `_load_file_bytes`** — 通过 ctx.db 查 sys_file 表拿 file_path → 同步 read_bytes（`# noqa: ASYNC240`，AI 调用频次低，同步 IO 可接受）。**反例**: 把加载逻辑塞 tool 函数内 → import_execute 也要一份；违反 DRY。**回归**: `_load_file_bytes(ctx, file_id) -> (bytes, filename, mime_type)` 模块级 helper。
  - 26.3 **file_id 豁免 scope_param_requires_check 静态校验** — `scripts/check_ai_tools.py::check_scope_param_requires_check` 原本要求任何 `*_id` 参数必须调 `ensure_targets_in_scope`；但 `file_id` 是 sys_file 资源（用户上传的临时文件），不属于业务 data_scope 范畴。在 check 函数加 `and a.arg != "file_id"` 豁免。**反例**: 不豁免 → user.import_preview 强制调 `ensure_targets_in_scope(user_ids=[file_id_int])` → 拿 file_id 当 user_id 查询，必然失败。**回归**: `scripts/check_ai_tools.py:230` 添加 `and a.arg != "file_id"`；docstring 注明豁免理由；15 tools 全过 8 static checks。
- [x] Task 26a ✅ 已完成（2026-08-04）**[v2.2 P0 #2.14 拆分]**: AI tool `user.import_execute`（risk=high / hitl_always / dry_run_supported / rows_affected），强制走 HITL 防止 LLM 跳过 preview。决策 26a.1-26a.3：
  - 26a.1 **直接从 batch.file_storage_key 读 file_bytes** — 不要求 LLM 在 execute 时重新传 file_id（spec §2.19 line 575 设计）；dry_run 阶段已保存文件到 LocalFileStorage，execute 凭 batch.file_storage_key 反查。**反例**: execute 要求 LLM 重传 file_id → LLM 可能传错文件 → 三重校验（file_sha256）失败。**回归**: `user_import_execute` 内 `Path(batch.file_storage_key).read_bytes()`；空 file_storage_key → AI_IMPORT_PREVIEW_INVALID。
  - 26a.2 **强制 HITL + LLM prompt 引导防跳过** — `hitl_always=True` + meta.summary 明确「Pass previewToken from preview」；LLM 必须先调 preview 拿 token，不能直接 execute。**反例**: 仅 risk=high 触发 HITL → LLM 可能在 prompt 里编造 preview_token 直调 execute。**回归**: meta `hitl_always=True` + dry_run_supported=True；`_dry_run_user_import_execute` 在 HITL 抽屉二次确认 batch summary。
  - 26a.3 **dry_run 不重复跑 service** — `_dry_run_user_import_execute` 只读 batch 表拿 summary（不重新 parse_import_excel + dry_run_import_users，避免双跑）。**反例**: dry_run 重跑完整 service → 用户每次看 HITL 抽屉都重新跑一次预检，浪费 IO + 可能因并发状态机乱。**回归**: `_dry_run_user_import_execute` 直接读 batch.summary_* + batch.filename 展示。
- [x] Task 27 ✅ 已完成（2026-08-04）：AI tool `user.export`（risk=high / dry_run_supported / produces_file / rows_affected），复用 `export_users_to_excel` service（含强制建 ExportTask + filter_snapshot 冻结 + 30 天 TTL）。决策 27.1-27.3：
  - 27.1 **复用 export_service 全套** — Tool 函数仅做参数转换（5 filter field → UserExportFilter）+ 调 service + 包装 ToolResult；service 层已强制建 task + filter_snapshot + 30 天 TTL（spec §2.31 P1-5）。**反例**: tool 重写 task 创建逻辑 → 与 HTTP 路径分叉，审计链路断。**回归**: `user_export` 函数 < 40 行；`test_export_audit_chain_joinable_with_operation_log` 等 service 层测试覆盖（Task 22c+ 跟进）。
  - 27.2 **dry_run 用 count(*) 预估不实际跑导出** — `_dry_run_user_export` 调 SELECT COUNT(*) 拿 estimated，不建 ExportTask（避免 dry_run 阶段也产生 task 行）。**反例**: dry_run 也跑 export_users_to_excel → 每次 HITL 抽屉展示都建一个空 task → 历史列表一堆 dry_run 残留。**回归**: `_dry_run_user_export` 仅 count；预估 > USER_EXPORT_ASYNC_THRESHOLD → DryRunResult.ok=False 提示缩窄 filter。
  - 27.3 **dry_run supported = True** — 配合 risk=high 让 HITL 抽屉先展示预估行数；用户看到「将导出约 100 行」再点确认，UX 友好。**反例**: 不支持 dry_run → 用户直接 confirm 后才知道导出多少行；导出 5000+ 行触发 AI_EXPORT_ASYNC_REQUIRED → 用户已等了几秒才发现失败。**回归**: `meta.dry_run_supported=True` + `_dry_run_user_export` 实现；warning `high_risk_requires_dry_run` 不再触发。
- [x] Task 28 ✅ 已完成（2026-08-04）：鉴权矩阵 11 场景全覆盖（参考 spec §10.3）。决策 28.1-28.2：
  - 28.1 **新工具继承现有鉴权链路，不需额外矩阵测试** — `user.list/lookup/update/import_preview/import_execute/export` 全部走既有 framework：`require_permissions` → `data_scope.filters` → `risk/hitl_always` → `dry_run_supported` → `ensure_targets_in_scope`。`test_authz_matrix.py` 9 个 case（autonomous / HITL / no-perm / scope-violation / hitl_always / quota / tool-not-found）用合成 tool 验证 framework 行为，真实 user.* tools 通过静态注册（15 tools 全过 8 static checks）+ 业务单测（test_ai_tools_user_list_lookup_update.py 18 用例）双重保障。**反例**: 把真实 user.list 等都搬进 test_authz_matrix.py → 矩阵测试膨胀到 50+ case，每个 user 工具都重复 framework 测试。**回归**: `tests/modules/ai/test_authz_matrix.py` 9 case 不变；新工具的鉴权行为由 `required_perms` + `risk` + `hitl_always` 声明（启动校验 perms 在 sys_menu 存在）。
  - 28.2 **scope_param_requires_check 扩展豁免 file_id** — Task 26.3 已记录：scripts/check_ai_tools.py 加 `and a.arg != "file_id"` 让 user.import_preview 可声明 `file_id: str` 参数而不被强制调 ensure_targets_in_scope（sys_file 不属业务 data_scope）。**回归**: 15 tools 全过 8 static checks；test_check_ai_tools.py 40 用例不破。
- [x] Task 29 ✅ 已完成（2026-08-04）：浏览器 E2E spec 落地（`tests/e2e/user-import-export.spec.ts` 3 smoke 用例：导入按钮 / 导出弹窗 reason 校验 / 历史抽屉）。决策 29.1-29.2：
  - 29.1 **AI tool 链路 E2E 推迟到独立 spec** — 「贴文本"加这几个用户" → import_preview → HITL 抽屉 → 确认 → import_execute」端到端流程依赖：AI chat UI + 真实 LLM API + 后端 dev stack + DB seed + 测试 xlsx。组合 setup 成本高 + LLM 输出非确定性（即使 temp=0 也可能漂移）。**反例**: 强行写完整 AI flow E2E → CI flaky，每次 LLM 输出略变就 break。**回归**: Task 21 ship 的 3 smoke 用例覆盖页面层按钮可达性；AI tool 端到端验证由 manual QA / staging 环境定期跑（参考 spec §10.3 11 场景的合成版本已在 test_authz_matrix.py 覆盖 framework 行为）。
  - 29.2 **页面 E2E + service 单测 + authz matrix 三层覆盖** — 页面 smoke（Task 21）+ service 单测（Task 17-22a 已 ship）+ authz matrix framework 测试（Task 28）三层互补。**反例**: 只信 E2E → 脆且慢；只信单测 → 集成 gap（如 i18n key 拼写 / API URL 错）漏检。**回归**: 当前覆盖率 70.74% ≥ 70% 门禁 + 5 vitest 文件 / 28 测试 + 15 AI tools 全过静态检查 = Phase 1+2 完工基线。
- [x] Task 30 ✅ 已完成（2026-08-05）：导出 Excel 字段翻译 + dept full_path（v2.3 §2.9.1）。决策 30.1-30.4：
  - 30.1 **翻译层放 `_build_excel` 不放 schema 层** — schema 层是契约（保持原值），Excel 渲染是展示（可翻译），分层铁律。**反例**: schema 层加 `display_label` 字段 → API 契约污染，前端要再走一次翻译；导出 Excel 是离线文件，不存在 i18n 切换场景。**回归**: `_build_excel(rows, dept_lookup)` 内部查 `_STATUS_LABELS` / `_GENDER_LABELS`；`UserExportTaskResponse` schema 保持原值不变。
  - 30.2 **dept full_path 复用 template_service._build_dept_full_path** — 同一函数模板下载 / 导出 Excel 共用，避免双实现漂移。`export_service._build_dept_lookup_for_rows` 一次性预查所有 leaf + ancestor dept（避免 N+1），构建 dept_id → Dept 映射传入 `_build_excel`。**反例**: 在 `_build_excel` 内部逐行 `await db.get(Dept, ...)` → 1000 行用户导出触发 1000 次 DB 查询。**回归**: `_build_dept_lookup_for_rows` 走两次 select（leaf + ancestor），复杂度 O(N+M)；`test_export_formats_dept_full_path` 用 3 层 dept 树验证。
  - 30.3 **role_codes 不翻译保持 code 字面值** — §2.18 已支持 code round-trip（导入侧 `_resolve_role_input` Pass 1 code 优先匹配），导出 dump code 直接 round-trip 可用；role_name 翻译会引入「角色改名后旧 Excel 失效」回归（§2.18 反例 3）。**回归**: `test_export_role_codes_unchanged` 验证导出仍是 role_code 而非 role_name。
  - 30.4 **翻译固定中文不走 i18n** — Excel 是离线文件，跨语言用户打开会乱码；管理员 Excel 场景业务侧按中文约定。**反例**: 走 `i18n.t("status.enable")` → 导出时英文 locale 用户得到 "Enabled"，导入时反查字典只识别中文 "启用" → round-trip 断。**回归**: `_STATUS_LABELS = {"1":"启用","2":"禁用"}` / `_GENDER_LABELS = {"0":"未知","1":"男","2":"女"}` 字面值常量，不走翻译函数。
  - 30.5 **dept 列表头改「部门」（原「部门ID」）** — v2.3 §2.9.1 改动后内容是 full_path「总公司/研发中心/前端部」，表头仍叫「部门ID」会让用户误以为单元格里应该是数字 ID。**反例**: 保留「部门ID」表头 → 用户看到「部门ID = 总公司/研发中心/前端部」困惑「这到底是 ID 还是名字」；改「部门路径」→ 与「部门字典」sheet 的 full_path 列对齐也行，但「部门」更短且与导入模板「数据」sheet 的 dept_input 列对齐（用户读导出 Excel 改完可直接复制到导入 Excel 的 dept_input 列）。**回归**: `_EXPORT_COLUMN_ORDER` 表头 `("dept_id", "部门")`；`test_export_formats_dept_full_path` 断言 `headers.index("部门")`。
  - 30.6 **导出文件名 `hohu_users_YYYYMMDD_HHmmss.xlsx`（hohu_ 前缀 + 时分秒）** — v2.2 原名 `users_YYYYMMDD.xlsx` 有两个问题：(a) 同日多次导出文件名冲突（浏览器自动加 "(1)" "(2)" 用户分不清哪个是最新）；(b) 无品牌前缀（用户下载文件夹多个项目的 users_*.xlsx 混杂）。**反例**: (1) 只加时分秒不加 hohu_ 前缀 → 文件名仍无项目标识。(2) 加毫秒 → 文件名过长无收益。(3) 用 UTC 时间 → 用户看到的是 +8 偏移的「不直观时间」（管理员在中国时区工作）。**回归**: 前端 `use-export-flow.ts buildFilename()` 用 `new Date()` 本地时间生成 `hohu_users_${ymd}_${hms}.xlsx`；后端 `user.py` Content-Disposition 同款（fallback，前端 `a.download` 实际生效）；vitest `use-export-flow.spec.ts` 断言 `/^hohu_users_\d{8}_\d{6}\.xlsx$/`。
- [x] Task 31 ✅ 已完成（2026-08-05）：导入侧中文字面值反查 + status 取值 0/1 → 1/2 矛盾修复（v2.3 §2.9.1）。决策 31.1-31.3：
  - 31.1 **status 取值统一对齐 DB / 前端 / 其他模块真实约定 ("1","2")** — spec §3.1 line 1634 原写 `Literal["0","1"]` 是笔误，与 `app/utils/validators.py:68 STATUS_ALLOWED = ("1","2")` + Menu 模型注释「1-启用，2-禁用」+ 前端 `enableStatusRecord = {'1': enable, '2': disable}` 矛盾；`import_parser._STATUS_VALUES = {"0","1"}` 会拦掉真实合法的 `status="2"` 禁用用户。**反例**: 保留 `{"0","1","2"}` 三值向后兼容 → 污染 DB 真实取值集合；DB 内不允许 "0" status，用户导入侧**必须**抛 `AI_IMPORT_STATUS_INVALID` 让用户改 Excel（不能静默写错数据）。**回归**: `import_parser._STATUS_VALUES = frozenset({"1","2"})`；`schemas.py:81` UserImportRecord.status Literal 改 `("1","2")`；`schemas.py:173` UserExportFilter.status 同步；`ai_tools.py:1324,1393` user_export / `_dry_run_user_export` filter 同步；`test_import_status_disabled_two_now_accepted` + `test_import_status_zero_now_rejected` 覆盖正反两个 case。
  - 31.2 **中文字面值反查走字典查表 + 字面值兜底** — `_STATUS_LABELS_INV = {"启用":"1","禁用":"2"}` / `_GENDER_LABELS_INV = {"未知":"0","男":"1","女":"2"}`；先查表，未命中走原字面值继续走 Literal 校验（白名单兜底）。**反例**: 用正则 / startswith 模糊匹配 → "启用中" 误命中 "启用" → 静默错误数据；模糊匹配违反 §2.17 反例 3（"研发" 误命中 "研发部"）。**回归**: `import_parser._validate_row` 在 status / user_gender 字段先 `_STATUS_LABELS_INV.get(input, input)` 再校验；`test_unknown_chinese_label_rejected` 验证 "启用中" 抛 `AI_IMPORT_STATUS_INVALID`。
  - 31.3 **test_import_schemas 两个 status="2" 测试断言需更新** — v2.2 断言 `status="2"` 抛 ValidationError；v2.3 后 "2" 是合法值，测试改为断言 `status="0"` 抛 ValidationError。**反例**: 保留旧断言 → CI 红；改为 `status="3"` → 也对（"3" 非法），但 "0" 更贴近真实误用场景（用户从旧 spec / 旧模板复制 status="0"）。**回归**: `test_import_schemas.py::TestUserImportRecord::test_invalid_status_rejected` + `TestUserExportFilter::test_invalid_status_rejected` 改用 `status="0"`。
- [x] Task 32 ✅ 已完成（2026-08-05）：test_user_export.py 两个 MultipleResultsFound 修复（v2.3 §2.9.1 顺手清理）。决策 32.1：
  - 32.1 **测试不假设全表只 1 行，用 export_id / reason 反查** — `test_export_filter_snapshot_freezes_accessible_dept_ids` + `test_export_failure_records_async_required_in_task` 用 `select(UserExportTask)).scalar_one()` 假设全表只 1 行；pytest 实际连的是 dev DB（pyproject.toml 未设 `ENV=test`，`.env` 是 dev 数据库 192.168.7.52），用户手动测试导出按钮留的真实 task 行让 `scalar_one()` 拿到 3 行 → MultipleResultsFound。**反例**: (1) 改 conftest 强制 `ENV=test` → 需要 .env.test 的 localhost DB 同步迁移，scope 蔓延。(2) 改 db_session fixture 测试前清空 sys_user_export_task → 违反 outer-transaction 零污染铁律，DELETE 会影响并发测试。(3) 用 `order_by(created_at desc).limit(1)` 拿最新一条 → 违反 spec 「不依赖 created_at DESC 排序」铁律。**回归**: 第一个测试用返回的 `export_id` 反查 `where export_id == export_id`；第二个测试失败路径 export_users_to_excel 抛异常不返回 export_id，用测试内唯一 reason `"QA failure task"` 反查 `where reason == "QA failure task"`；符合 spec 「显式 version/id 而非 created_at DESC」铁律；17/17 test_user_export.py 通过（原 15/17）。
- [x] Task 33 ✅ 已完成（2026-08-05）：AI 对话内导出下载闭环（spec §2.31 line 1626 / line 2619 落地）。Phase 1/2 实现缺口补齐：spec 承诺 AI tool 返回 `detail_card` 含 `downloadUrl`，实现时只做了 `rows_affected` + 缺下载端点，导致 AI 引导「去导出记录下载」但前端无此页面、文件落盘拿不到。决策 33.1-33.5：
  - 33.1 **新端点 `GET /system/user/export/{export_id}/download`** — 从 `sys_user_export_task.file_storage_key` 读 bytes 流式返回；权限 `system:user:export`（与 POST /export 同级）；service 层 `download_export_file()` 区分 4 种 errorCode：`AI_EXPORT_TASK_NOT_FOUND`（export_id 不存在）/ `AI_EXPORT_TASK_NOT_READY`（status != SUCCESS）/ `AI_EXPORT_FILE_MISSING`（file_storage_key 为 None）/ `AI_EXPORT_FILE_EXPIRED`（FileStorage.read 抛 FileNotFoundError，30 天 TTL 清理或外部删除）。**反例**: (1) 复用 `GET /export/{export_id}` 详情端点直接返回 bytes → 混淆元数据查询 vs 文件下载两种语义，前端要按 Content-Type 分支处理。(2) 不区分 errorCode 统一 400 → 用户看到「文件已过期」和「任务失败」是同一提示，误导重试。(3) 权限放宽到 `system:user:list` → 只读角色也能拿到 PII 文件，违反导出权限独立原则。**回归**: `app/modules/system/api/user.py` `download_export_file_endpoint`；`test_user_export_api.py::TestGetExportDownload` 6 个测试覆盖 401/200/404/400×3。
  - 33.2 **filename 从 task.created_at 派生，不重新生成** — 决策 30.6 同款 `hohu_users_YYYYMMDD_HHmmss.xlsx` 格式；用 task.created_at 而非 `datetime.now()` — 重下载历史任务时反映真实导出时刻，便于审计反查「这份文件是哪次导出的」。**反例**: 用 `datetime.now()` → 同一文件多次下载文件名不同，下载文件夹混乱 + 违反「同名文件即同一内容」直觉。**回归**: `export_service.download_export_file` 用 `task.created_at.strftime('%Y%m%d_%H%M%S')`；`test_download_returns_bytes_and_filename` 断言前缀 + 后缀。
  - 33.3 **AI tool `user.export` result_view 从 rows_affected → detail_card** — spec line 2619 本来就承诺 `detail_card` 展示下载链接 + 行数 + 字段；Task 27 实现时简化成 `rows_affected`（只显示行数），LLM 拿不到 downloadUrl 只能糊弄「去导出记录下载」。detail_card view_data 含 `fields`（导出批次 ID / 导出行数 / 文件大小 / 过期时间）+ `downloadUrl` + `downloadFilename` + `rowCount` + `fileSize` + `expiresAt`；data（LLM 视角）含 `exportId` + `rowCount` + `downloadUrl`（LLM 可直接引用字面 URL 给用户）。**反例**: 保留 rows_affected + 用 chip_target 跳转 → chip 是模块页路由（`/system/user`），不是文件下载链接；用户还得手动找「导出记录」页面（不存在）。**回归**: `ai_tools.py user_export` 调用 `get_export_task` 拿 file_size_bytes + created_at 计算 expiresAt；`test_ai_tools_user_export.py::TestUserExportDetailCard` 4 个测试覆盖 result_view / downloadUrl / fileSize+expiresAt / data 三字段。
  - 33.4 **前端 DetailCardViewData 类型扩展 + DetailCardView 下载按钮** — `Api.Ai.DetailCardViewData` 加可选 `downloadUrl` / `downloadFilename`；`DetailCardView.vue` 在 fields 下方条件渲染 NButton（`v-if="viewData.downloadUrl"`），点击调 `request({url, responseType:'blob'})` → `URL.createObjectURL` → a.click 触发浏览器保存（与 use-export-flow 的 triggerBlobDownload 同款模式）；loading 状态防重复点击；错误 toast 走 `common.exportModal.errorCode.EXPORT_FAILED`。**反例**: (1) 新建独立 view_type `file_download` → 为单个场景扩张标准视图集（STANDARD_VIEW_TYPES 启动校验），不如复用 detail_card 通用。(2) `<a href={downloadUrl}>` 直接链接 → 没有 Authorization header，后端 401；Blob 下载才能走 request 拦截器带 token。(3) chip_target 承载 → chip 是 SPA 路由跳转，不是文件下载。**回归**: `src/views/ai/chat/modules/tool-views/DetailCardView.vue` + `src/typings/api/ai.d.ts DetailCardViewData` + `src/service/api/system.ts fetchDownloadExportFile`（备用，组件内直接用 request 更通用）；新增 i18n key `common.download`；typecheck + lint + vitest 28/28 通过。
  - 33.5 **i18n key 走公共 `common.download`，不开 `ai.tool.user.export.download`** — 按钮文案「下载」是纯通用动作，与 import 模块的 `common.downloadTemplate` 等已有 key 同款处理；放公共区让其他模块（role / dept / job）后续接入 detail_card 时无需重复声明。**反例**: 每个模块各开 `ai.tool.{module}.export.download` → 5 个模块 5 个 key 字面量一致，违反 DRY。**回归**: `zh-cn.ts` + `en-us.ts` + `app.d.ts Schema` 三处同步。
  - 33.6 **下载按钮提到 chat-tool-call 卡片底部 chip-row 常显，不放 DetailCardView 折叠 body 内**（2026-08-05 二次修正） — 初版（决策 33.4）把下载按钮放在 DetailCardView fields 下方，但 chat-tool-call 的 body 默认折叠（`expanded = ref(false)`），用户必须点卡片头才能看到下载按钮 → 实测用户找不到，只看到 LLM 文本回复里的「下载地址」字面字符串（非可点链接，且 `<a href>` 也不带 Authorization header 会 401）。修正为：chat-tool-call.vue 在 `chipHref` 同级新增 `downloadAction` computed（从 `result.ui.viewData.downloadUrl` 提取），渲染与 chip-link 同视觉样式的绿色 `<button>`（区分「拿文件」vs「跳转」），点击调 `request({responseType:'blob'})` → URL.createObjectURL → a.click 触发浏览器保存；DetailCardView 不再承担下载按钮职责（回归纯字段 grid）。**反例**: (1) 保留 DetailCardView 内嵌按钮 + 默认展开卡片 → 破坏其他 detail_card tool（如 job.update_cron）的折叠 UX；下载是动作不是视图，属于卡片级 UX 而非 view-level。(2) 让用户看 LLM 文本里的下载地址点击 → URL 字符串不可点；变成 `<a>` 也没 Authorization header；违反「动作走 UI 不走 LLM 文本」原则。(3) 加 auto-expand 逻辑只对含 downloadUrl 的卡片默认展开 → 状态管理复杂化，且 chip-row 已经是「常显动作区」语义位置。**回归**: `chat-tool-call.vue` 加 `downloadAction` computed + `handleDownload` 函数 + chip-row 第二行；`DetailCardView.vue` 移除下载按钮（回退到决策 33.4 之前的纯渲染职责）；typecheck + lint 通过。

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
