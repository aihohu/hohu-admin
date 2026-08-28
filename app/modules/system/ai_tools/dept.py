"""Department management AI tools."""

from typing import Annotated, Any

from pydantic import Field
from pydantic.experimental.missing_sentinel import MISSING
from sqlalchemy import Select

from app.constants import STATUS_ENABLED
from app.core.exceptions import (
    BusinessException,
    BusinessRuleException,
)
from app.modules.ai.agents.gateway import ensure_targets_in_scope
from app.modules.ai.agents.gateway.result import (
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.stats_validator import (
    validate_filters_in_whitelist,
)
from app.modules.ai.core.context import AiToolContext
from app.modules.system.models.dept import Dept
from app.modules.system.schemas.dept import DeptCreate, DeptUpdate
from app.modules.system.service.dept_selector import department_selector
from app.modules.system.service.dept_service import dept_service

from .common import (
    _bound_confirmation_fields,
    _coerce_list_limit,
    _confirmation_display,
    _result_projection,
)


@ai_tool(
    AiToolMeta(
        name="dept.list",
        agent="dept_mgmt",
        summary=(
            "List depts → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use dept.count for count-only."
        ),
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        allowed_filters=("status",),
        chip_target="/system/dept",
        result_view="data_list",
    )
)
async def dept_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """List departments and return a bounded compact projection.

    The LLM receives ``data.{total, limit, sample[3]}`` for prompt caching.
    The UI receives all bounded rows in ``ui.view_data.{columns, rows}``.

    Filters:
        status: ``"1"`` for enabled or ``"0"`` for disabled.
    Limit:
        Missing or non-positive values use 20; positive values are capped at 50.
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
    safe_limit = _coerce_list_limit(limit)

    scoped_filters = []
    for key, value in filters.items():
        scoped_filters.append(getattr(Dept, key) == str(value))

    page = await department_selector.page(
        ctx.db,
        scope=ctx.data_scope,
        current=1,
        size=safe_limit,
        filters=scoped_filters,
    )
    total = page.total
    rows = page.records

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "ai.tool.field.name"},
        {"key": "parent_id", "label": "ai.tool.field.parentDeptId"},
        {"key": "status", "label": "ai.tool.field.status"},
    ]
    accessible_dept_ids = ctx.data_scope.accessible_dept_ids
    records = [
        {
            "id": str(d.dept_id),
            "name": d.dept_name,
            "parent_id": (
                str(d.parent_id)
                if d.parent_id is not None
                and (
                    accessible_dept_ids is None
                    or int(d.parent_id) in accessible_dept_ids
                )
                else None
            ),
            "status": d.status,
        }
        for d in rows
    ]
    return ToolResult.success(
        data={
            "total": total,
            "limit": safe_limit,
            "sample": records[:3],
        },
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": total},
            label_key="ai.tool.dept.list.result",
            label_params={"count": total},
        ),
    )


# ============ user department lookup and assignment ============

_DEPT_LOOKUP_MAX_MATCHES = 20


def _build_scoped_dept_lookup_stmt(
    *,
    accessible_dept_ids: set[int] | None,
    normalized_query: str,
    limit: int,
) -> Select[Any]:
    """Compatibility wrapper around the shared department selector."""
    return department_selector.build_lookup_statement(
        accessible_dept_ids=accessible_dept_ids,
        normalized_query=normalized_query,
        limit=limit,
    )


async def _lookup_departments(
    ctx: AiToolContext,
    *,
    query: str,
    limit: int,
    query_error_code: str,
    limit_error_code: str,
    label_key: str,
    enabled_only: bool,
) -> ToolResult:
    """Build one scoped lookup result for both Department-facing agents."""
    normalized_query = query.strip()
    path_parts = [part.strip() for part in normalized_query.split("/")]
    if not normalized_query or any(not part for part in path_parts):
        raise BusinessRuleException(
            "部门查询条件不能为空",
            error_code=query_error_code,
        )
    normalized_query = " / ".join(path_parts)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise BusinessRuleException(
            "部门查询数量必须在 1 到 20 之间",
            error_code=limit_error_code,
        )

    result = await department_selector.lookup(
        ctx.db,
        scope=ctx.data_scope,
        normalized_query=normalized_query,
        limit=limit,
        enabled_only=enabled_only,
    )
    matches = [
        {
            "deptId": str(match.dept_id),
            "deptName": match.dept_name,
            "path": match.path,
        }
        for match in result.matches
    ]
    return ToolResult.success(
        data={
            "query": normalized_query,
            "matchCount": result.match_count,
            "matches": matches,
        },
        projection=_result_projection(
            "dept",
            result.matched_dept_ids,
            scope_bound=True,
        ),
        ui=UIResult(
            view_type="data_list",
            view_data={
                "columns": [
                    {"key": "deptId", "label": "ID"},
                    {"key": "deptName", "label": "page.system.dept.deptName"},
                    {"key": "path", "label": "page.ai.chat.departmentPath"},
                ],
                "rows": matches,
            },
            audit={
                "query": normalized_query,
                "match_count": result.match_count,
                "returned_count": len(matches),
            },
            label_key=label_key,
            label_params={"count": result.match_count},
        ),
    )


@ai_tool(
    AiToolMeta(
        name="user.dept_lookup",
        agent="user_mgmt",
        summary=(
            "Find visible departments by name or scoped path before user create/update."
        ),
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        result_view="data_list",
        args_summary_fields=("query", "limit"),
    )
)
async def user_dept_lookup(
    ctx: AiToolContext,
    *,
    query: str,
    limit: int = _DEPT_LOOKUP_MAX_MATCHES,
) -> ToolResult:
    """Return enabled scoped department candidates without leaking ancestors."""
    return await _lookup_departments(
        ctx,
        query=query,
        limit=limit,
        query_error_code="AI_USER_DEPT_QUERY_REQUIRED",
        limit_error_code="AI_USER_DEPT_LOOKUP_LIMIT_INVALID",
        label_key="ai.tool.user.dept_lookup.result",
        enabled_only=True,
    )


@ai_tool(
    AiToolMeta(
        name="dept.lookup",
        agent="dept_mgmt",
        summary="Find visible departments by name or scoped path.",
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        result_view="data_list",
        args_summary_fields=("query", "limit"),
    )
)
async def dept_lookup(
    ctx: AiToolContext,
    *,
    query: str,
    limit: int = _DEPT_LOOKUP_MAX_MATCHES,
) -> ToolResult:
    """Return visible Department Agent management targets in any status."""
    return await _lookup_departments(
        ctx,
        query=query,
        limit=limit,
        query_error_code="AI_DEPT_QUERY_REQUIRED",
        limit_error_code="AI_DEPT_LOOKUP_LIMIT_INVALID",
        label_key="ai.tool.user.dept_lookup.result",
        enabled_only=False,
    )


AiDepartmentId = Annotated[int, Field(strict=True, gt=0)]


def _department_result(
    *,
    action: str,
    department: Dept,
    affected_user_ids: tuple[int, ...] = (),
) -> ToolResult:
    """Build one locale-neutral result for an approved department write."""
    dept_id = int(department.dept_id)
    return ToolResult.success(
        data={
            "action": action,
            "deptId": str(dept_id),
            "deptName": department.dept_name,
            "parentId": (
                str(department.parent_id) if department.parent_id is not None else None
            ),
            "status": department.status,
            "affectedUserCount": len(affected_user_ids),
        },
        projection=_result_projection("dept", [dept_id], scope_bound=True),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": department.dept_name,
                "fields": [
                    {"label": "ai.tool.field.deptId", "value": str(dept_id)},
                    {"label": "ai.tool.field.action", "value": action},
                    {
                        "label": "ai.tool.field.affectedUserCount",
                        "value": len(affected_user_ids),
                    },
                ],
            },
            audit={
                "dept_id": str(dept_id),
                "action": action,
                "affected_user_ids": [str(value) for value in affected_user_ids],
            },
            label_key=f"ai.tool.dept.{action}.result",
            label_params={"deptName": department.dept_name},
        ),
    )


def _require_department_snapshot(ctx: AiToolContext) -> dict[str, Any]:
    """Require the server-owned snapshot attached to an approved action."""
    if ctx.approved_business_snapshot is None:
        raise BusinessRuleException(
            "部门操作缺少审批快照",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        )
    return ctx.approved_business_snapshot


@ai_tool(
    AiToolMeta(
        name="dept.create",
        agent="dept_mgmt",
        summary="Create one scoped department after explicit approval.",
        required_perms=("system:dept:add", "system:dept:list"),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("parent_id", "dept_name", "leader", "status"),
    )
)
async def dept_create(
    ctx: AiToolContext,
    *,
    parent_id: AiDepartmentId | None,
    dept_name: str,
    order_num: int = 0,
    leader: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    status: str = STATUS_ENABLED,
) -> ToolResult:
    """Execute an approved department create through the shared service."""
    snapshot = _require_department_snapshot(ctx)
    payload = DeptCreate(
        parent_id=parent_id,
        dept_name=dept_name,
        order_num=order_num,
        leader=leader,
        phone=phone,
        email=email,
        status=status,
    )
    try:
        await ensure_targets_in_scope(
            ctx,
            dept_ids=[parent_id] if parent_id is not None else [],
        )
        department = await dept_service.create(
            ctx.db,
            payload,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后部门事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    return _department_result(action="create", department=department)


async def _dry_run_dept_create(
    ctx: AiToolContext,
    *,
    parent_id: AiDepartmentId | None,
    dept_name: str,
    order_num: int = 0,
    leader: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    status: str = STATUS_ENABLED,
) -> Any:
    """Freeze a normalized department create and its authorization facts."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    payload = DeptCreate(
        parent_id=parent_id,
        dept_name=dept_name,
        order_num=order_num,
        leader=leader,
        phone=phone,
        email=email,
        status=status,
    )
    preview = await dept_service.preview_create(
        ctx.db,
        payload,
        actor_user_id=ctx.user.user_id,
    )
    execution_args = {
        "parent_id": parent_id,
        "dept_name": dept_name,
        "order_num": order_num,
        "leader": leader,
        "phone": phone,
        "email": email,
        "status": status,
    }
    confirmation_fields = _bound_confirmation_fields(
        execution_args,
        dept_create.__ai_tool_meta__.args_summary_fields,
    )
    leader_fact = preview.snapshot.get("facts", {}).get("leader")
    if isinstance(leader_fact, dict):
        for field in confirmation_fields:
            if field["label"] == "leader":
                field["display_value"] = leader_fact["display"]
    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将创建部门 {dept_name}",
        summary_key="page.ai.chat.confirmDeptCreateSummary",
        summary_params={"deptName": dept_name},
        confirmation_fields=confirmation_fields,
        execution_args=execution_args,
        business_snapshot=preview.snapshot,
    )


@ai_tool(
    AiToolMeta(
        name="dept.update",
        agent="dept_mgmt",
        summary="Update scoped non-structural department fields after approval.",
        required_perms=("system:dept:edit", "system:dept:list"),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=(
            "dept_id",
            "dept_name",
            "order_num",
            "leader",
            "phone",
            "email",
            "status",
        ),
    )
)
async def dept_update(
    ctx: AiToolContext,
    *,
    dept_id: AiDepartmentId,
    dept_name: str | MISSING = MISSING,
    order_num: int | MISSING = MISSING,
    leader: str | None | MISSING = MISSING,
    phone: str | None | MISSING = MISSING,
    email: str | None | MISSING = MISSING,
    status: str | MISSING = MISSING,
) -> ToolResult:
    """Execute an approved department update through the shared service."""
    snapshot = _require_department_snapshot(ctx)
    values = {
        key: value
        for key, value in {
            "dept_name": dept_name,
            "order_num": order_num,
            "leader": leader,
            "phone": phone,
            "email": email,
            "status": status,
        }.items()
        if value is not MISSING
    }
    payload = DeptUpdate.model_validate(values)
    try:
        await ensure_targets_in_scope(ctx, dept_ids=[dept_id])
        department = await dept_service.update(
            ctx.db,
            dept_id,
            payload,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后部门事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    affected = tuple(
        int(value)
        for value in snapshot.get("facts", {}).get("userIds", [])
        if int(value) != int(ctx.user.user_id)
    )
    return _department_result(
        action="update",
        department=department,
        affected_user_ids=affected,
    )


async def _dry_run_dept_update(
    ctx: AiToolContext,
    *,
    dept_id: AiDepartmentId,
    dept_name: str | MISSING = MISSING,
    order_num: int | MISSING = MISSING,
    leader: str | None | MISSING = MISSING,
    phone: str | None | MISSING = MISSING,
    email: str | None | MISSING = MISSING,
    status: str | MISSING = MISSING,
) -> Any:
    """Freeze a normalized department update and its authorization facts."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    execution_args = {
        key: value
        for key, value in {
            "dept_id": dept_id,
            "dept_name": dept_name,
            "order_num": order_num,
            "leader": leader,
            "phone": phone,
            "email": email,
            "status": status,
        }.items()
        if key == "dept_id" or value is not MISSING
    }
    payload = DeptUpdate.model_validate(
        {key: value for key, value in execution_args.items() if key != "dept_id"}
    )
    preview = await dept_service.preview_update(
        ctx.db,
        dept_id,
        payload,
        actor_user_id=ctx.user.user_id,
    )
    confirmation_fields = _bound_confirmation_fields(
        execution_args,
        dept_update.__ai_tool_meta__.args_summary_fields,
    )
    leader_fact = preview.snapshot.get("facts", {}).get("leader")
    if isinstance(leader_fact, dict):
        for field in confirmation_fields:
            if field["label"] == "leader":
                field["display_value"] = leader_fact["display"]
    return DryRunResult(
        ok=True,
        count=len(preview.affected_user_ids),
        reason=f"将更新部门 {dept_id}",
        summary_key="page.ai.chat.confirmDeptUpdateSummary",
        summary_params={"deptId": str(dept_id)},
        confirmation_fields=confirmation_fields,
        execution_args=execution_args,
        business_snapshot=preview.snapshot,
    )


@ai_tool(
    AiToolMeta(
        name="dept.move",
        agent="dept_mgmt",
        summary="Move one scoped department subtree after explicit approval.",
        required_perms=("system:dept:move", "system:dept:list"),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("dept_id", "new_parent_id"),
    )
)
async def dept_move(
    ctx: AiToolContext,
    *,
    dept_id: AiDepartmentId,
    new_parent_id: AiDepartmentId | None,
) -> ToolResult:
    """Execute an approved department move through the shared service."""
    snapshot = _require_department_snapshot(ctx)
    try:
        await ensure_targets_in_scope(
            ctx,
            dept_ids=[
                dept_id,
                *([new_parent_id] if new_parent_id is not None else []),
            ],
        )
        department = await dept_service.move(
            ctx.db,
            dept_id=dept_id,
            new_parent_id=new_parent_id,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后部门事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    affected = tuple(
        int(value)
        for value in snapshot.get("facts", {}).get("userIds", [])
        if int(value) != int(ctx.user.user_id)
    )
    return _department_result(
        action="move",
        department=department,
        affected_user_ids=affected,
    )


async def _dry_run_dept_move(
    ctx: AiToolContext,
    *,
    dept_id: AiDepartmentId,
    new_parent_id: AiDepartmentId | None,
) -> Any:
    """Freeze a normalized department move and its authorization facts."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    preview = await dept_service.preview_move(
        ctx.db,
        dept_id=dept_id,
        new_parent_id=new_parent_id,
        actor_user_id=ctx.user.user_id,
    )
    return DryRunResult(
        ok=True,
        count=len(preview.affected_user_ids),
        reason=f"将移动部门 {dept_id}",
        summary_key="page.ai.chat.confirmDeptMoveSummary",
        summary_params={"deptId": str(dept_id)},
        confirmation_fields=[
            {"label": "dept_id", "value": dept_id},
            {
                "label": "new_parent_id",
                "value": new_parent_id,
                "display_value": _confirmation_display(new_parent_id),
            },
        ],
        execution_args={
            "dept_id": dept_id,
            "new_parent_id": new_parent_id,
        },
        business_snapshot=preview.snapshot,
    )
