"""User import and export AI tools."""

from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import func, select

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
)
from app.modules.ai.agents.gateway.result import (
    PreparedActionProposal,
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.file_access import (
    IMPORT_MIME_TYPES_BY_EXTENSION,
    FileAccessPolicy,
    load_protected_file,
)
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext
from app.modules.system.models.user import User

from .common import (
    _result_projection,
)

# ============ user.import_preview / user.import_execute ============


_USER_IMPORT_FILE_POLICY = FileAccessPolicy(
    allowed_business_types=frozenset({"user-import"}),
    mime_types_by_extension=IMPORT_MIME_TYPES_BY_EXTENSION,
    max_bytes=10 * 1024 * 1024,
)


def _user_import_suffix_for_mime(mime_type: str) -> str:
    normalized = mime_type.split(";", maxsplit=1)[0].strip().lower()
    for suffix, allowed_mime_types in IMPORT_MIME_TYPES_BY_EXTENSION.items():
        if normalized in allowed_mime_types:
            return suffix

    raise BusinessRuleException(
        "导入文件类型不允许",
        error_code="AI_FILE_TYPE_NOT_ALLOWED",
    )


def _user_import_mime_for_filename(filename: str) -> str:
    suffix = (
        f".{filename.rsplit('.', maxsplit=1)[-1].lower()}" if "." in filename else ""
    )
    allowed_mime_types = IMPORT_MIME_TYPES_BY_EXTENSION.get(suffix)
    if allowed_mime_types:
        return next(iter(allowed_mime_types))

    raise BusinessRuleException(
        "预检文件类型无效，请重新 import_preview",
        error_code="AI_IMPORT_PREVIEW_INVALID",
    )


async def _load_file_bytes(ctx: AiToolContext, file_id: str) -> tuple[bytes, str, str]:
    """从受保护的 ``sys_file`` 加载用户导入文件。

    抛 BusinessRuleException:
        - AI_FILE_ID_INVALID: file_id 不是合法数字字符串
        - AI_FILE_NOT_FOUND: 不存在 / 已删除 / owner 或 tenant 不匹配
        - AI_FILE_TYPE_NOT_ALLOWED: 业务类型、扩展名、MIME 或 magic 不允许
        - AI_FILE_TOO_LARGE: DB 声明或磁盘实际大小超限
        - AI_FILE_PATH_INVALID: 路径越出私有上传根或不可安全读取

    Returns: (file_bytes, filename, mime_type)
    """
    protected = await load_protected_file(
        ctx,
        file_id,
        policy=_USER_IMPORT_FILE_POLICY,
    )
    # Use the resolved on-disk name: its suffix has already been checked against
    # the trusted DB extension and MIME.  ``record.file_name`` is a bare
    # Snowflake ID, so persisting it would lose the CSV/XLSX parser contract.
    return protected.data, protected.path.name, protected.mime_type


@ai_tool(
    AiToolMeta(
        name="user.import_preview",
        agent="user_mgmt",
        summary=(
            "Prepare user import; requested_outcome is required. Gateway owns "
            "confirmation and execution."
        ),
        required_perms=("system:user:import",),
        risk="low",
        readonly=False,
        idempotent=False,
        interaction_flow="prepared",
        prepared_execute_tool="user.import_execute",
        accepts_file=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
        ),
        result_view="detail_card",
        args_summary_fields=("file_id", "reason"),
    )
)
async def user_import_preview(
    ctx: AiToolContext,
    *,
    file_id: str,
    reason: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"] = "CREATE_ONLY",
) -> ToolResult:
    """解析用户导入文件并生成只读预览。

    流程：
    1. _load_file_bytes(file_id) → file_bytes + filename + mime_type
    2. parse_import_excel(file_bytes, mime_type) → records
    3. dry_run_import_users(records, current_user, file_bytes, filename, reason,
                             on_conflict=...) → (ImportDryRunResult, batch)
    4. ToolResult.success(
         data={batch_id, summary{new, exists, conflict, out_of_scope}},
         ui=detail_card（HITL 抽屉展示 summary 供用户确认）
       )

    **预检会写 artifact**：本 tool 不写 ``sys_user``，但每次都会新建 batch、
    cache 和预检文件，因此不是 readonly / idempotent。若模型请求执行，Gateway
    使用 PreparedActionProposal 自动进入绑定 execute 的 HITL，不再依赖第二次模型调用。

    Args:
        file_id: 文件 ID（sys_file.file_id 字符串形式）
        reason: 业务理由（1-256 字符）
        on_conflict: 'skip'（默认）/ 'overwrite' / 'fail_fast'
        sync_mode: 员工编号同步策略，在 preview 时冻结
    """
    from app.modules.system.service.user_import_parser import (  # noqa: PLC0415
        import_file_has_column,
        parse_import_excel,
    )
    from app.modules.system.service.user_import_service import (  # noqa: PLC0415
        dry_run_import_users,
    )
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )

    file_bytes, filename, mime_type = await _load_file_bytes(ctx, file_id)
    has_role_column = import_file_has_column(
        file_bytes,
        mime_type,
        "role_input",
    )
    await user_role_assignment_service.ensure_import_permissions(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        has_role_column=has_role_column,
    )
    records = parse_import_excel(file_bytes, mime_type)

    dry_run_result, batch = await dry_run_import_users(
        ctx.db,
        records,
        ctx.user,
        file_bytes,
        filename,
        reason,
        on_conflict=on_conflict,
        has_role_column=has_role_column,
    )

    # 预览阶段持久化文件；执行阶段按 storage key 读取，避免依赖客户端重复上传。
    from app.core.file_storage import get_file_storage  # noqa: PLC0415

    storage = get_file_storage()
    storage_suffix = _user_import_suffix_for_mime(mime_type)
    storage_key = await storage.save(
        file_bytes,
        mime_type=mime_type,
        namespace="import-preview",
        suffix=storage_suffix,
    )
    batch.file_storage_key = storage_key
    await ctx.db.flush()

    summary = {
        "new": dry_run_result.new_count,
        "exists": dry_run_result.exists_count,
        "conflict": dry_run_result.conflict_count,
        "outOfScope": dry_run_result.out_of_scope_count,
    }
    return ToolResult.success(
        data={
            "batchId": batch.batch_id,
            "total": dry_run_result.total,
            "summary": summary,
            "policy": {
                "onConflict": on_conflict,
                "syncMode": sync_mode,
            },
        },
        projection=_result_projection("user_import_batch", [batch.batch_id]),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "batchId": batch.batch_id,
                "total": dry_run_result.total,
                "summary": summary,
                "policy": {
                    "onConflict": on_conflict,
                    "syncMode": sync_mode,
                },
                "expiresAt": (
                    batch.created_at.isoformat() if batch.created_at else None
                ),
            },
            audit={
                "batch_id": batch.batch_id,
                "total_rows": dry_run_result.total,
            },
            label_key="ai.tool.user.import_preview.result",
            label_params={"total": dry_run_result.total},
        ),
        prepared_action=PreparedActionProposal(
            frozen_args={
                "preview_token": batch.preview_token,
                "reason": reason,
                "on_conflict": on_conflict,
                "sync_mode": sync_mode,
            },
            snapshot={
                "batch_id": str(batch.batch_id),
                "file_sha256": getattr(batch, "file_sha256", ""),
                "records_hash": getattr(batch, "records_hash", ""),
                "operator_id": getattr(batch, "operator_id", ctx.user.user_id),
                "total": dry_run_result.total,
                "summary": summary,
            },
            subject_ref={
                "type": "user_import_batch",
                "id": str(batch.batch_id),
            },
            presentation={
                "title": "确认导入用户",
                "fields": [
                    {"label": "total", "value": dry_run_result.total},
                    {"label": "new", "value": dry_run_result.new_count},
                    {"label": "exists", "value": dry_run_result.exists_count},
                    {"label": "conflict", "value": dry_run_result.conflict_count},
                    {
                        "label": "outOfScope",
                        "value": dry_run_result.out_of_scope_count,
                    },
                    {"label": "onConflict", "value": on_conflict},
                    {"label": "syncMode", "value": sync_mode},
                ],
                "warnings": [],
            },
            expires_at=batch.created_at + timedelta(minutes=10),
        ),
    )


@ai_tool(
    AiToolMeta(
        name="user.import_execute",
        agent="user_mgmt",
        summary=("Gateway-only execution for an approved user import preview."),
        required_perms=("system:user:import",),
        risk="high",
        readonly=False,
        idempotent=True,
        hitl_always=True,
        llm_visible=False,
        dry_run_supported=False,
        result_view="rows_affected",
        args_summary_fields=("reason", "on_conflict", "sync_mode"),
    )
)
async def user_import_execute(
    ctx: AiToolContext,
    *,
    preview_token: str,
    reason: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"] = "CREATE_ONLY",
) -> ToolResult:
    """执行已经预览并确认的用户导入。

    **强制 HITL**（hitl_always=True）：用户必须在抽屉确认 preview summary 后才执行。
    模型不能跳过预览直接执行。

    流程：
    1. 凭 preview_token 反查 batch（含 file_sha256 + records_hash + operator_id）
    2. 从 sys_file 的存储引用重新加载文件
    3. parse_import_excel → records（与 preview 时 hash 一致）
    4. batch_create_users_from_records(...) → ImportResult
    5. ToolResult.rows_affected（successCount + skippedCount + ...）

    Args:
        preview_token: 来自 user.import_preview 返回值，10min TTL
        reason: 必须与预览时的业务理由一致
        on_conflict: 必须与预览时一致
        sync_mode: 'CREATE_ONLY'（默认）/ 'UPDATE_PROFILE' / 'FULL_SYNC'
    """
    from app.modules.system.constants import EmployeeNoSyncMode  # noqa: PLC0415
    from app.modules.system.service.user_import_parser import (  # noqa: PLC0415
        import_file_has_column,
        parse_import_excel,
    )
    from app.modules.system.service.user_import_service import (  # noqa: PLC0415
        batch_create_users_from_records,
        get_batch_by_preview_token,
    )
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )

    # 1. 反查 batch 拿 file 信息
    batch = await get_batch_by_preview_token(ctx.db, preview_token)
    if batch is None:
        from app.core.exceptions import UnprocessableEntityException  # noqa: PLC0415

        raise UnprocessableEntityException(
            "preview_token 无效或已过期",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    # 2. 凭 batch.file_storage_key 从 FileStorage 读 file_bytes
    # 执行阶段通过 FileStorage 抽象读取，不直接拼接文件系统路径。
    from app.core.file_storage import get_file_storage  # noqa: PLC0415

    if not batch.file_storage_key:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            "批次未关联上传文件，无法 execute（请重新 import_preview）",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    storage = get_file_storage()
    try:
        file_bytes = await storage.read(batch.file_storage_key)
    except FileNotFoundError:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            f"预检文件已丢失（{batch.filename}），请重新 import_preview",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        ) from None
    filename = batch.filename or ""

    # 3. parse + execute
    mime_type = _user_import_mime_for_filename(filename)
    has_role_column = import_file_has_column(
        file_bytes,
        mime_type,
        "role_input",
    )
    await user_role_assignment_service.ensure_import_permissions(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        has_role_column=has_role_column,
    )
    records = parse_import_excel(
        file_bytes,
        mime_type,
    )

    result = await batch_create_users_from_records(
        ctx.db,
        records,
        preview_token=preview_token,
        file_bytes=file_bytes,
        filename=filename,
        reason=reason,
        current_user=ctx.user,
        on_conflict=on_conflict,
        sync_mode=EmployeeNoSyncMode(sync_mode),
        has_role_column=has_role_column,
    )

    return ToolResult.success(
        data={
            "successCount": result.success_count,
            "skippedCount": result.skipped_count,
            "overwrittenCount": result.overwritten_count,
            "failedCount": result.failed_count,
            "batchId": result.batch_id,
        },
        projection=_result_projection("user_import_batch", [result.batch_id]),
        ui=UIResult(
            view_type="rows_affected",
            view_data={
                "count": result.success_count,
                "ids": [result.batch_id],  # batch_id 而非 user_ids（避免大量 ID 进 UI）
            },
            audit={
                "batch_id": result.batch_id,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
            },
            label_key="ai.tool.user.import_execute.result",
            label_params={"count": result.success_count},
        ),
    )


# ============ user.export ============


@ai_tool(
    AiToolMeta(
        name="user.export",
        agent="user_mgmt",
        summary=(
            "Export xlsx → {exportId,rowCount,downloadReady}. "
            "Reason required; filters: name/email/status."
        ),
        required_perms=("system:user:export",),
        risk="high",
        readonly=False,  # 写 ExportTask 表 + 生成 xlsx 文件
        idempotent=False,
        projection_kind="scope_bound",
        produces_file=True,
        dry_run_supported=True,
        # 导出结果使用详情卡，并提供鉴权下载地址。
        result_view="detail_card",
        args_summary_fields=("reason",),
    )
)
async def user_export(
    ctx: AiToolContext,
    *,
    reason: str,
    user_name: str | None = None,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    status: Literal["1", "2"] | None = None,
) -> ToolResult:
    """导出用户列表到 Excel。

    始终创建 ExportTask、冻结筛选快照，并设置 30 天文件有效期。
    行数 > USER_EXPORT_ASYNC_THRESHOLD（5000）抛 AI_EXPORT_ASYNC_REQUIRED，
    用户必须缩窄筛选条件或拆分请求；当前不会自动入队。

    Args:
        reason: 业务理由（必填，1-256 字符）
        user_name / nickname / user_email / user_phone: filter（可选）
        status: '1' (启用) / '0' (禁用)，None=不过滤
    """
    from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
        result_projection_service,
    )
    from app.modules.system.schemas.user_transfer import (  # noqa: PLC0415
        UserExportFilter,
    )
    from app.modules.system.service.user_export_service import (  # noqa: PLC0415
        export_users_to_excel,
        get_export_task,
        get_file_storage,
    )

    filter_ = UserExportFilter(
        user_name=user_name,
        nickname=nickname,
        user_email=user_email,
        user_phone=user_phone,
        status=status,
    )

    # Authorize before creating the database task or writing an external file.
    preflight_lineage = result_projection_service.freeze_lineage(
        tenant_id=ctx.tenant_id,
        agent_code=ctx.tool_meta.agent,
        tool_codes=[ctx.tool_meta.name],
        subject_refs=[],
        data_scope_hash=ctx.data_scope_hash,
        projection_dependency_message_ids=(ctx.projection_dependency_message_ids),
    )
    if not await result_projection_service.authorize_result_projection(
        ctx.db,
        ctx.user,
        owner_user_id=ctx.user.user_id,
        lineage=preflight_lineage,
    ):
        raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")

    _xlsx_bytes, row_count, export_id = await export_users_to_excel(
        ctx.db,
        filter_,
        ctx.user,
        reason=reason,
    )

    # 从持久化任务读取文件元数据和到期时间，避免前端自行推断。
    task = await get_export_task(
        ctx.db,
        export_id,
        operator_id=ctx.user.user_id,
    )
    file_size = task.file_size_bytes if task else None
    expires_at = (task.created_at + timedelta(days=30)).isoformat() if task else None
    projection = _result_projection("user_export_task", [export_id], scope_bound=True)

    lineage = result_projection_service.freeze_lineage(
        tenant_id=ctx.tenant_id,
        agent_code=ctx.tool_meta.agent,
        tool_codes=[ctx.tool_meta.name],
        subject_refs=projection.subject_refs,
        data_scope_hash=ctx.data_scope_hash,
        projection_dependency_message_ids=(ctx.projection_dependency_message_ids),
    )
    download_token = await result_projection_service.issue_download_token(
        ctx.db,
        ctx.user,
        resource_type="user_export",
        resource_id=export_id,
        lineage=lineage,
    )
    if download_token is None:
        if task is not None and task.file_storage_key:
            await get_file_storage().delete(task.file_storage_key)
        raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")
    download_url = f"/ai/download/user-export/{export_id}?token={download_token}"

    return ToolResult.success(
        data={
            "exportId": export_id,
            "rowCount": row_count,
            "downloadReady": True,
        },
        projection=projection,
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": "用户导出",
                "fields": [
                    {"label": "ai.tool.field.exportId", "value": export_id},
                    {"label": "ai.tool.field.exportRows", "value": str(row_count)},
                    {
                        "label": "ai.tool.field.fileSize",
                        "value": f"{file_size} B" if file_size is not None else "—",
                    },
                    {"label": "ai.tool.field.expiresAt", "value": expires_at or "—"},
                ],
                "downloadUrl": download_url,
                "downloadFilename": (
                    f"hohu_users_{task.created_at.strftime('%Y%m%d_%H%M%S')}.xlsx"
                    if task
                    else "hohu_users.xlsx"
                ),
                "rowCount": row_count,
                "fileSize": file_size,
                "expiresAt": expires_at,
            },
            audit={
                "export_id": export_id,
                "row_count": row_count,
                "filter": {
                    "user_name": user_name,
                    "nickname": nickname,
                    "user_email": user_email,
                    "user_phone": user_phone,
                    "status": status,
                },
            },
            label_key="ai.tool.user.export.result",
            label_params={"count": row_count},
        ),
    )


async def _dry_run_user_export(
    ctx: AiToolContext,
    *,
    reason: str,  # noqa: ARG001  与 execute 签名对齐；dry_run 阶段不重复校验 reason
    user_name: str | None = None,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    status: Literal["1", "2"] | None = None,
) -> Any:
    """预估导出行数供确认界面展示。

    用 User.count(*) + filter 估算行数，不实际跑导出（避免重复建 task）。
    行数 > USER_EXPORT_ASYNC_THRESHOLD → 提示用户缩窄 filter；行数为 0 → 警告。
    """
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    base = select(User).where(*ctx.data_scope.filters)
    if user_name:
        base = base.where(User.user_name.ilike(f"%{user_name}%"))
    if nickname:
        base = base.where(User.nickname.ilike(f"%{nickname}%"))
    if user_email:
        base = base.where(User.user_email == user_email)
    if user_phone:
        base = base.where(User.user_phone == user_phone)
    if status is not None:
        base = base.where(User.status == status)

    estimated = int(
        await ctx.db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    if estimated == 0:
        return DryRunResult(
            ok=False,
            count=0,
            reason="筛选条件下无用户匹配，导出会生成空文件",
        )

    from app.modules.system.constants import (  # noqa: PLC0415
        USER_EXPORT_ASYNC_THRESHOLD,
    )

    if estimated > USER_EXPORT_ASYNC_THRESHOLD:
        return DryRunResult(
            ok=False,
            count=estimated,
            reason=(
                f"预计导出 {estimated} 行，超过同步阈值 {USER_EXPORT_ASYNC_THRESHOLD}，"
                "请缩窄 filter 后重试"
            ),
        )

    return DryRunResult(
        ok=True,
        count=estimated,
        reason=f"将导出约 {estimated} 行用户数据到 xlsx 文件（30 天后过期清理）",
        examples=[
            f"filter: user_name={user_name or '*'}, status={status or '*'}",
        ],
    )
