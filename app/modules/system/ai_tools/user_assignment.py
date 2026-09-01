"""User department and role assignment AI tools."""

from typing import Annotated, Any

from pydantic import ConfigDict, Field
from sqlalchemy import select
from typing_extensions import TypedDict

from app.core.exceptions import (
    BusinessException,
    BusinessRuleException,
)
from app.modules.ai.agents.gateway import ensure_targets_in_scope
from app.modules.ai.agents.gateway.result import (
    ResultProjection,
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.user import User

from .common import (
    _result_projection,
)


class AiUserDepartmentAssignment(TypedDict):
    """Strict complete-set item exposed in the model-facing tool schema."""

    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)

    dept_id: Annotated[
        int,
        Field(gt=0, description="Stable department ID returned by user.dept_lookup."),
    ]
    is_primary: Annotated[
        bool,
        Field(description="Whether this is the user's single primary department."),
    ]


def _parse_ai_dept_assignments(
    dept_assignments: list[AiUserDepartmentAssignment],
) -> list[tuple[int, bool]]:
    """Validate the model-facing complete collection before shared policy use."""

    if not isinstance(dept_assignments, list):
        raise BusinessRuleException(
            "部门集合无效",
            error_code="USER_DEPT_NOT_AVAILABLE",
        )
    parsed: list[tuple[int, bool]] = []
    for item in dept_assignments:
        if (
            not isinstance(item, dict)
            or set(item) != {"dept_id", "is_primary"}
            or isinstance(item["dept_id"], bool)
            or not isinstance(item["dept_id"], int)
            or item["dept_id"] <= 0
            or not isinstance(item["is_primary"], bool)
        ):
            raise BusinessRuleException(
                "部门集合无效",
                error_code="USER_DEPT_NOT_AVAILABLE",
            )
        parsed.append((item["dept_id"], item["is_primary"]))
    return parsed


def _format_ai_dept_assignments(
    assignments: tuple[tuple[int, bool], ...],
    snapshot: dict[str, Any],
) -> str:
    """Build a locale-neutral result value from the approved department facts."""
    raw_facts = snapshot.get("departmentFacts")
    facts = raw_facts if isinstance(raw_facts, list) else []
    names = {
        item["deptId"]: item["deptName"]
        for item in facts
        if isinstance(item, dict)
        and isinstance(item.get("deptId"), str)
        and isinstance(item.get("deptName"), str)
    }
    values: list[str] = []
    for dept_id, is_primary in assignments:
        identifier = str(dept_id)
        name = names.get(identifier)
        descriptor = name or "—"
        values.append(f"★ {descriptor}" if is_primary else descriptor)
    return "; ".join(values) or "—"


@ai_tool(
    AiToolMeta(
        name="user.update_dept",
        agent="user_mgmt",
        summary="Replace one user's complete department set after HITL approval.",
        required_perms=("system:user:edit", "system:dept:list"),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("user_id", "dept_assignments"),
    )
)
async def user_update_dept(
    ctx: AiToolContext,
    *,
    user_id: int,
    dept_assignments: list[AiUserDepartmentAssignment],
) -> ToolResult:
    """Apply an approved complete department set through the shared policy."""
    from app.modules.system.service.user_department_assignment_service import (  # noqa: PLC0415
        user_department_assignment_service,
    )

    if ctx.approved_business_snapshot is None:
        raise BusinessRuleException(
            "用户部门调整缺少审批快照",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        )
    parsed = _parse_ai_dept_assignments(dept_assignments)
    try:
        await ensure_targets_in_scope(
            ctx,
            user_ids=[user_id],
            dept_ids=[dept_id for dept_id, _is_primary in parsed],
        )
        result = await user_department_assignment_service.replace_departments(
            ctx.db,
            actor_user_id=ctx.user.user_id,
            target_user_id=user_id,
            dept_assignments=parsed,
            expected_snapshot=ctx.approved_business_snapshot,
            tenant=ctx.tenant,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后授权或目标事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc

    target = await ctx.db.scalar(
        select(User).where(
            User.tenant_id == ctx.tenant_id,
            User.user_id == user_id,
        )
    )
    user_name = target.user_name if target is not None else str(user_id)
    old_items = [
        {"deptId": str(dept_id), "isPrimary": is_primary}
        for dept_id, is_primary in result.old_assignments
    ]
    new_items = [
        {"deptId": str(dept_id), "isPrimary": is_primary}
        for dept_id, is_primary in result.new_assignments
    ]
    old_display = _format_ai_dept_assignments(
        result.old_assignments,
        ctx.approved_business_snapshot,
    )
    new_display = _format_ai_dept_assignments(
        result.new_assignments,
        ctx.approved_business_snapshot,
    )
    subject_dept_ids = sorted(
        {
            dept_id
            for dept_id, _is_primary in (
                *result.old_assignments,
                *result.new_assignments,
            )
        }
    )
    return ToolResult.success(
        data={
            "updated": 1,
            "userName": user_name,
            "previousDepartments": old_display,
            "newDepartments": new_display,
        },
        projection=ResultProjection(
            subject_refs=(
                {"type": "user", "id": str(user_id)},
                *({"type": "dept", "id": str(dept_id)} for dept_id in subject_dept_ids),
            )
        ),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": user_name,
                "fields": [
                    {
                        "label": "page.ai.chat.previousDepartments",
                        "value": old_display,
                    },
                    {
                        "label": "page.ai.chat.newDepartments",
                        "value": new_display,
                    },
                ],
            },
            audit={
                "affected_user_ids": [str(user_id)],
                "old_dept_ids": [item["deptId"] for item in old_items],
                "new_dept_ids": [item["deptId"] for item in new_items],
            },
            label_key="ai.tool.user.update_dept.result",
            label_params={"userName": user_name},
        ),
    )


async def _dry_run_user_update_dept(
    ctx: AiToolContext,
    *,
    user_id: int,
    dept_assignments: list[AiUserDepartmentAssignment],
) -> Any:
    """Freeze the normalized replacement and its server-owned approval facts."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415
    from app.modules.system.service.user_department_assignment_service import (  # noqa: PLC0415
        user_department_assignment_service,
    )

    parsed = _parse_ai_dept_assignments(dept_assignments)
    preview = await user_department_assignment_service.preview_departments(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        target_user_id=user_id,
        dept_assignments=parsed,
        tenant=ctx.tenant,
    )
    canonical_assignments = [
        {"dept_id": dept_id, "is_primary": is_primary}
        for dept_id, is_primary in preview.new_assignments
    ]
    old_display = "; ".join(preview.old_display) or "—"
    new_display = "; ".join(preview.new_display) or "—"
    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将调整用户 {preview.user_name} 的完整部门集合",
        summary_key="page.ai.chat.confirmUpdateDeptSummary",
        summary_params={"userName": preview.user_name},
        examples=[f"原部门：{old_display}", f"新部门：{new_display}"],
        confirmation_fields=[
            {
                "label": "user_id",
                "value": user_id,
                "display_value": preview.user_name,
            },
            {
                "label": "dept_assignments",
                "value": dept_assignments,
                "display_value": f"{old_display} → {new_display}",
            },
        ],
        execution_args={
            "user_id": int(preview.user_id),
            "dept_assignments": canonical_assignments,
        },
        business_snapshot=preview.snapshot,
    )


# ============ user role lookup and assignment ============

_ROLE_LOOKUP_MAX_MATCHES = 20

AiUserRoleId = Annotated[
    int,
    Field(
        strict=True,
        gt=0,
        description="Stable role ID returned by user.role_lookup.",
    ),
]
AiUserRoleIds = Annotated[
    list[AiUserRoleId],
    Field(
        min_length=1,
        description="Complete replacement role ID set; incremental changes are forbidden.",
    ),
]


def _parse_ai_role_ids(role_ids: list[int]) -> list[int]:
    """Validate a strict, non-empty, duplicate-free complete role set."""

    if (
        not isinstance(role_ids, list)
        or not role_ids
        or any(
            isinstance(role_id, bool) or not isinstance(role_id, int) or role_id <= 0
            for role_id in role_ids
        )
    ):
        raise BusinessRuleException(
            "角色集合无效",
            error_code="USER_ROLE_NOT_AVAILABLE",
        )
    if len(set(role_ids)) != len(role_ids):
        raise BusinessRuleException(
            "角色集合不能包含重复项",
            error_code="USER_ROLE_SET_DUPLICATE",
        )
    return sorted(role_ids)


def _format_ai_roles(
    role_ids: tuple[int, ...],
    snapshot: dict[str, Any],
) -> str:
    """Build a locale-neutral role value from approved immutable facts."""
    raw_facts = snapshot.get("roleFacts")
    facts = raw_facts if isinstance(raw_facts, list) else []
    role_map = {
        item["roleId"]: item["roleName"]
        for item in facts
        if isinstance(item, dict)
        and isinstance(item.get("roleId"), str)
        and isinstance(item.get("roleName"), str)
    }
    values: list[str] = []
    for role_id in role_ids:
        identifier = str(role_id)
        role = role_map.get(identifier)
        values.append(role or "—")
    return "; ".join(values) or "—"


@ai_tool(
    AiToolMeta(
        name="user.role_lookup",
        agent="user_mgmt",
        summary="Find currently delegable enabled roles by code or name.",
        required_perms=(USER_ROLE_AUTH_PERMISSION,),
        risk="low",
        readonly=True,
        idempotent=True,
        result_view="data_list",
        args_summary_fields=("query", "limit"),
    )
)
async def user_role_lookup(
    ctx: AiToolContext,
    *,
    query: str,
    limit: int = _ROLE_LOOKUP_MAX_MATCHES,
) -> ToolResult:
    """Return only enabled role candidates within the delegation ceiling."""
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )

    normalized_query = query.strip()
    if not normalized_query:
        raise BusinessRuleException(
            "角色查询条件不能为空",
            error_code="AI_USER_ROLE_QUERY_REQUIRED",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise BusinessRuleException(
            "角色查询数量必须在 1 到 20 之间",
            error_code="AI_USER_ROLE_LOOKUP_LIMIT_INVALID",
        )

    page = await user_role_assignment_service.lookup_assignable_roles(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        query=normalized_query,
        limit=limit,
        tenant=ctx.tenant,
    )
    matches = [
        {
            "roleId": str(role.role_id),
            "roleCode": role.role_code,
            "roleName": role.role_name,
            "dataScope": role.data_scope,
        }
        for role in page.roles
    ]
    return ToolResult.success(
        data={
            "query": normalized_query,
            "matchCount": page.match_count,
            "matches": matches,
        },
        projection=_result_projection("delegable_role", page.matched_role_ids),
        ui=UIResult(
            view_type="data_list",
            view_data={
                "columns": [
                    {"key": "roleId", "label": "ID"},
                    {"key": "roleCode", "label": "page.system.role.roleCode"},
                    {"key": "roleName", "label": "page.system.role.roleName"},
                    {
                        "key": "dataScope",
                        "label": "page.system.role.dataScope.label",
                    },
                ],
                "rows": matches,
            },
            audit={
                "query": normalized_query,
                "match_count": page.match_count,
                "returned_count": len(matches),
            },
            label_key="ai.tool.user.role_lookup.result",
            label_params={"count": page.match_count},
        ),
    )


@ai_tool(
    AiToolMeta(
        name="user.update_roles",
        agent="user_mgmt",
        summary="Replace one user's complete role set after HITL approval.",
        required_perms=("system:user:edit", USER_ROLE_AUTH_PERMISSION),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("user_id", "role_ids"),
    )
)
async def user_update_roles(
    ctx: AiToolContext,
    *,
    user_id: int,
    role_ids: AiUserRoleIds,
) -> ToolResult:
    """Apply an approved complete role set through the shared policy."""
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )

    if ctx.approved_business_snapshot is None:
        raise BusinessRuleException(
            "用户角色调整缺少审批快照",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        )
    parsed = _parse_ai_role_ids(role_ids)
    try:
        await ensure_targets_in_scope(ctx, user_ids=[user_id])
        result = await user_role_assignment_service.replace_roles(
            ctx.db,
            actor_user_id=ctx.user.user_id,
            target_user_id=user_id,
            role_ids=parsed,
            expected_snapshot=ctx.approved_business_snapshot,
            tenant=ctx.tenant,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后授权或目标事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc

    target = await ctx.db.scalar(
        select(User).where(
            User.tenant_id == ctx.tenant_id,
            User.user_id == user_id,
        )
    )
    user_name = target.user_name if target is not None else str(user_id)
    old_display = _format_ai_roles(
        result.old_role_ids,
        ctx.approved_business_snapshot,
    )
    new_display = _format_ai_roles(
        result.new_role_ids,
        ctx.approved_business_snapshot,
    )
    subject_role_ids = sorted({*result.old_role_ids, *result.new_role_ids})
    return ToolResult.success(
        data={
            "updated": 1,
            "userName": user_name,
            "previousRoles": old_display,
            "newRoles": new_display,
        },
        projection=ResultProjection(
            subject_refs=(
                {"type": "user", "id": str(user_id)},
                {"type": "complete_user_role_assignment", "id": str(user_id)},
                *(
                    {"type": "delegable_role", "id": str(role_id)}
                    for role_id in subject_role_ids
                ),
            )
        ),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": user_name,
                "fields": [
                    {"label": "page.ai.chat.previousRoles", "value": old_display},
                    {"label": "page.ai.chat.newRoles", "value": new_display},
                ],
            },
            audit={
                "affected_user_ids": [str(user_id)],
                "old_role_ids": [str(role_id) for role_id in result.old_role_ids],
                "new_role_ids": [str(role_id) for role_id in result.new_role_ids],
            },
            label_key="ai.tool.user.update_roles.result",
            label_params={"userName": user_name},
        ),
    )


async def _dry_run_user_update_roles(
    ctx: AiToolContext,
    *,
    user_id: int,
    role_ids: AiUserRoleIds,
) -> Any:
    """Freeze the normalized role replacement and authorization snapshot."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )

    parsed = _parse_ai_role_ids(role_ids)
    preview = await user_role_assignment_service.preview_roles(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        target_user_id=user_id,
        role_ids=parsed,
        tenant=ctx.tenant,
    )
    old_display = "; ".join(preview.old_display) or "—"
    new_display = "; ".join(preview.new_display) or "—"
    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将调整用户 {preview.user_name} 的完整角色集合",
        summary_key="page.ai.chat.confirmUpdateRolesSummary",
        summary_params={"userName": preview.user_name},
        examples=[f"原角色：{old_display}", f"新角色：{new_display}"],
        confirmation_fields=[
            {
                "label": "user_id",
                "value": user_id,
                "display_value": preview.user_name,
            },
            {
                "label": "role_ids",
                "value": role_ids,
                "display_value": f"{old_display} → {new_display}",
            },
        ],
        execution_args={
            "user_id": int(preview.user_id),
            "role_ids": list(preview.new_role_ids),
        },
        business_snapshot=preview.snapshot,
    )
