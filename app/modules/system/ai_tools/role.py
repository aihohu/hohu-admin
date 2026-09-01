"""Role management AI tools."""

import enum
from typing import Annotated, Any

from pydantic import AfterValidator, Field
from pydantic.experimental.missing_sentinel import MISSING

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    EnableStatus,
)
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
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.service.role_management_service import role_management_service

from .common import (
    _bound_confirmation_fields,
    _coerce_list_limit,
    _confirmation_display,
    _enable_status_label_key,
    _enable_status_semantic,
    _model_validate_for_ai,
    _result_projection,
    _validate_enable_status,
    _validate_enable_status_filter,
)


class AiRoleDataScope(enum.StrEnum):
    """Stable model-facing names for Role data-scope choices."""

    ALL = "ALL"
    CUSTOM = "CUSTOM"
    DEPT = "DEPT"
    DEPT_AND_SUB = "DEPT_AND_SUB"
    SELF = "SELF"


_ROLE_DATA_SCOPE_CODES = {
    AiRoleDataScope.ALL: DATA_SCOPE_ALL,
    AiRoleDataScope.CUSTOM: DATA_SCOPE_CUSTOM,
    AiRoleDataScope.DEPT: DATA_SCOPE_DEPT,
    AiRoleDataScope.DEPT_AND_SUB: DATA_SCOPE_DEPT_AND_SUB,
    AiRoleDataScope.SELF: DATA_SCOPE_SELF,
}
_ROLE_DATA_SCOPE_NAMES = {
    code: scope.value for scope, code in _ROLE_DATA_SCOPE_CODES.items()
}
_ROLE_DATA_SCOPE_LABEL_KEYS = {
    AiRoleDataScope.ALL.value: "page.system.role.dataScope.all",
    AiRoleDataScope.CUSTOM.value: "page.system.role.dataScope.custom",
    AiRoleDataScope.DEPT.value: "page.system.role.dataScope.dept",
    AiRoleDataScope.DEPT_AND_SUB.value: "page.system.role.dataScope.deptAndSub",
    AiRoleDataScope.SELF.value: "page.system.role.dataScope.self",
}


def _role_data_scope_code(value: AiRoleDataScope | str) -> str:
    """Map a model name or a frozen canonical code to the storage code."""
    if isinstance(value, AiRoleDataScope):
        return _ROLE_DATA_SCOPE_CODES[value]
    if value in _ROLE_DATA_SCOPE_NAMES:
        return value
    try:
        return _ROLE_DATA_SCOPE_CODES[AiRoleDataScope(value)]
    except ValueError as exc:
        raise BusinessRuleException(
            "角色数据权限范围无效",
            error_code="AI_ROLE_DATA_SCOPE_INVALID",
        ) from exc


def _role_data_scope_name(code: str) -> str:
    """Return the unambiguous model-facing name for a storage code."""
    try:
        return _ROLE_DATA_SCOPE_NAMES[code]
    except KeyError as exc:
        raise BusinessRuleException(
            "角色数据权限范围无效",
            error_code="AI_ROLE_DATA_SCOPE_INVALID",
        ) from exc


def _role_scope_confirmation_field(code: str) -> dict[str, str]:
    """Present the semantic name while retaining the bound storage code."""
    return {
        "label": "data_scope",
        "value": code,
        "display_value": _role_data_scope_name(code),
    }


def _role_data_scope_label_key(code: str) -> str:
    """Return a locale key for a canonical Role data-scope code."""
    return _ROLE_DATA_SCOPE_LABEL_KEYS[_role_data_scope_name(code)]


@ai_tool(
    AiToolMeta(
        name="role.list",
        agent="role_mgmt",
        summary=(
            "List roles → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use role.count for count-only."
        ),
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        allowed_filters=("status",),
        chip_target="/system/role",
        result_view="data_list",
    )
)
async def role_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """列出角色，返回前 N 条精简字段

    LLM 看 data.{total, limit, sample[3]}（精简，进 prompt cache）；
    前端看 ui.view_data.{columns, rows}（全量 limit 条，渲染 table）。

    filters:
        status: '1' (启用) / '2' (禁用)
    limit:
        None / 0 / 负数 = 默认 20；正整数按 min(limit, 50) 截断
    """
    filters = _validate_enable_status_filter(
        validate_filters_in_whitelist(ctx.tool_meta, filters)
    )
    safe_limit = _coerce_list_limit(limit)

    summaries, total, contributor_ids = await role_management_service.summarize_roles(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        tenant=ctx.tenant,
        status=str(filters["status"]) if "status" in filters else None,
        limit=safe_limit,
    )

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "ai.tool.field.name"},
        {"key": "code", "label": "ai.tool.field.code"},
        {"key": "status", "label": "ai.tool.field.status"},
        {"key": "delegable", "label": "ai.tool.field.delegable"},
        {"key": "blockedReasonCode", "label": "ai.tool.field.blockedReasonCode"},
    ]
    records = [
        {
            "id": str(role.role_id),
            "name": role.role_name,
            "code": role.role_code,
            "status": role.status,
            "dataScope": _role_data_scope_name(role.data_scope),
            "dataScopeCode": role.data_scope,
            "delegable": role.delegable,
            "blockedReasonCode": role.blocked_reason_code,
        }
        for role in summaries
    ]
    return ToolResult.success(
        data={
            "total": total,
            "limit": safe_limit,
            "sample": records[:3],  # 给 LLM 看前 3 条（prompt cache 友好）
        },
        projection=_result_projection(
            "role",
            contributor_ids,
            scope_bound=True,
        ),
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": total},
            label_key="ai.tool.role.list.result",
            label_params={"count": total},
        ),
    )


AiRoleId = Annotated[int, Field(strict=True, gt=0)]
AiRoleRelatedId = Annotated[int, Field(strict=True, gt=0)]


def _require_unique_role_related_ids(values: list[int]) -> list[int]:
    """Reject ambiguous complete sets before preview or execution."""
    if len(values) != len(set(values)):
        raise ValueError("Role related ID sets must not contain duplicates")
    return values


AiRoleRelatedIds = Annotated[
    list[AiRoleRelatedId],
    AfterValidator(_require_unique_role_related_ids),
]


def _role_result(*, action: str, role: Role) -> ToolResult:
    """Build a locale-neutral result for one approved Role mutation."""
    role_id = int(role.role_id)
    return ToolResult.success(
        data={
            "action": action,
            "roleCode": role.role_code,
            "roleName": role.role_name,
            "status": _enable_status_semantic(role.status),
            "dataScope": _role_data_scope_name(role.data_scope),
        },
        projection=_result_projection("managed_role", [role_id], scope_bound=True),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": role.role_name,
                "fields": [
                    {"label": "ai.tool.field.roleCode", "value": role.role_code},
                    {
                        "label": "ai.tool.field.status",
                        "value": _enable_status_label_key(role.status),
                    },
                    {
                        "label": "page.system.role.dataScope.label",
                        "value": _role_data_scope_label_key(role.data_scope),
                    },
                ],
            },
            audit={
                "role_id": str(role_id),
                "status": role.status,
                "data_scope": role.data_scope,
                "action": action,
            },
            label_key=f"ai.tool.role.{action}.result",
            label_params={"roleName": role.role_name},
        ),
    )


def _require_role_snapshot(ctx: AiToolContext) -> dict[str, Any]:
    """Require the server-owned Role snapshot attached by dry-run."""
    if ctx.approved_business_snapshot is None:
        raise BusinessRuleException(
            "角色操作缺少审批快照",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        )
    return ctx.approved_business_snapshot


@ai_tool(
    AiToolMeta(
        name="role.lookup",
        agent="role_mgmt",
        summary="Find tenant roles by code or name with delegation status.",
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        result_view="data_list",
        args_summary_fields=("query", "limit"),
    )
)
async def role_lookup(
    ctx: AiToolContext,
    *,
    query: str,
    limit: int = 20,
) -> ToolResult:
    """Return minimal Role matches without treating read state as authority."""
    normalized = query.strip()
    if not normalized:
        raise BusinessRuleException(
            "角色查询条件不能为空",
            error_code="AI_ROLE_QUERY_REQUIRED",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise BusinessRuleException(
            "角色查询数量必须在 1 到 20 之间",
            error_code="AI_ROLE_LOOKUP_LIMIT_INVALID",
        )
    summaries, total, contributor_ids = await role_management_service.summarize_roles(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        tenant=ctx.tenant,
        query=normalized,
        limit=limit,
    )
    rows = [
        {
            "roleId": str(role.role_id),
            "roleCode": role.role_code,
            "roleName": role.role_name,
            "status": role.status,
            "dataScope": _role_data_scope_name(role.data_scope),
            "dataScopeCode": role.data_scope,
            "delegable": role.delegable,
            "blockedReasonCode": role.blocked_reason_code,
        }
        for role in summaries
    ]
    return ToolResult.success(
        data={"query": normalized, "matchCount": total, "matches": rows},
        projection=_result_projection(
            "role",
            contributor_ids,
            scope_bound=True,
        ),
        ui=UIResult(
            view_type="data_list",
            view_data={
                "columns": [
                    {"key": "roleCode", "label": "ai.tool.field.code"},
                    {"key": "roleName", "label": "ai.tool.field.name"},
                    {"key": "delegable", "label": "ai.tool.field.delegable"},
                    {
                        "key": "blockedReasonCode",
                        "label": "ai.tool.field.blockedReasonCode",
                    },
                ],
                "rows": rows,
            },
            audit={"query": normalized, "match_count": total},
            label_key="ai.tool.role.lookup.result",
            label_params={"count": total},
        ),
    )


@ai_tool(
    AiToolMeta(
        name="role.create",
        agent="role_mgmt",
        summary="Create one delegated Role after explicit approval.",
        required_perms=("system:role:add",),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("role_code", "role_name", "data_scope"),
    )
)
async def role_create(
    ctx: AiToolContext,
    *,
    role_name: str,
    role_code: str,
    data_scope: AiRoleDataScope,
    status: EnableStatus,
    role_desc: str | None = None,
    dept_ids: AiRoleRelatedIds | None = None,
) -> ToolResult:
    """Execute an approved Role create through the shared policy."""
    snapshot = _require_role_snapshot(ctx)
    data_scope_code = _role_data_scope_code(data_scope)
    canonical_status = _validate_enable_status(status)
    payload = _model_validate_for_ai(
        RoleCreate,
        {
            "role_name": role_name,
            "role_code": role_code,
            "role_desc": role_desc,
            "data_scope": data_scope_code,
            "status": canonical_status,
            "dept_ids": dept_ids,
        },
        message="角色参数格式不合法",
        error_code="AI_ROLE_INPUT_INVALID",
    )
    try:
        await ensure_targets_in_scope(ctx, dept_ids=dept_ids or [])
        role = await role_management_service.create(
            ctx.db,
            payload,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
            tenant=ctx.tenant,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后角色事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    return _role_result(action="create", role=role)


async def _dry_run_role_create(
    ctx: AiToolContext,
    *,
    role_name: str,
    role_code: str,
    data_scope: AiRoleDataScope,
    status: EnableStatus,
    role_desc: str | None = None,
    dept_ids: AiRoleRelatedIds | None = None,
) -> Any:
    """Freeze a normalized Role create and its delegated authority facts."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    data_scope_code = _role_data_scope_code(data_scope)
    canonical_status = _validate_enable_status(status)
    payload = _model_validate_for_ai(
        RoleCreate,
        {
            "role_name": role_name,
            "role_code": role_code,
            "role_desc": role_desc,
            "data_scope": data_scope_code,
            "status": canonical_status,
            "dept_ids": dept_ids,
        },
        message="角色参数格式不合法",
        error_code="AI_ROLE_INPUT_INVALID",
    )
    preview = await role_management_service.preview_create(
        ctx.db,
        payload,
        actor_user_id=ctx.user.user_id,
        tenant=ctx.tenant,
    )
    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将创建角色 {role_name}",
        summary_key="page.ai.chat.confirmRoleCreateSummary",
        summary_params={"roleName": role_name},
        confirmation_fields=[
            {"label": "role_code", "value": role_code},
            {"label": "role_name", "value": role_name},
            _role_scope_confirmation_field(data_scope_code),
            {"label": "status", "value": canonical_status},
        ],
        execution_args={
            "role_name": role_name,
            "role_code": role_code,
            "role_desc": role_desc,
            "data_scope": data_scope_code,
            "status": canonical_status,
            "dept_ids": dept_ids,
        },
        business_snapshot=preview.snapshot,
    )


@ai_tool(
    AiToolMeta(
        name="role.update",
        agent="role_mgmt",
        summary="Update one delegated Role definition after approval.",
        required_perms=("system:role:edit",),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=(
            "role_id",
            "role_name",
            "role_desc",
            "data_scope",
            "status",
            "dept_ids",
        ),
    )
)
async def role_update(
    ctx: AiToolContext,
    *,
    role_id: AiRoleId,
    role_name: str | MISSING = MISSING,
    role_desc: str | None | MISSING = MISSING,
    data_scope: AiRoleDataScope | MISSING = MISSING,
    status: EnableStatus | MISSING = MISSING,
    dept_ids: AiRoleRelatedIds | MISSING = MISSING,
) -> ToolResult:
    """Execute an approved Role definition update through the shared policy."""
    snapshot = _require_role_snapshot(ctx)
    data_scope_code = (
        MISSING if data_scope is MISSING else _role_data_scope_code(data_scope)
    )
    canonical_status = MISSING if status is MISSING else _validate_enable_status(status)
    values = {
        key: value
        for key, value in {
            "role_name": role_name,
            "role_desc": role_desc,
            "data_scope": data_scope_code,
            "status": canonical_status,
            "dept_ids": dept_ids,
        }.items()
        if value is not MISSING
    }
    payload = _model_validate_for_ai(
        RoleUpdate,
        values,
        message="角色参数格式不合法",
        error_code="AI_ROLE_INPUT_INVALID",
    )
    try:
        await ensure_targets_in_scope(
            ctx,
            dept_ids=[] if dept_ids is MISSING else dept_ids,
        )
        role = await role_management_service.update(
            ctx.db,
            role_id,
            payload,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
            tenant=ctx.tenant,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后角色事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    return _role_result(action="update", role=role)


async def _dry_run_role_update(
    ctx: AiToolContext,
    *,
    role_id: AiRoleId,
    role_name: str | MISSING = MISSING,
    role_desc: str | None | MISSING = MISSING,
    data_scope: AiRoleDataScope | MISSING = MISSING,
    status: EnableStatus | MISSING = MISSING,
    dept_ids: AiRoleRelatedIds | MISSING = MISSING,
) -> Any:
    """Freeze a normalized Role definition update and all member impacts."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    data_scope_code = (
        MISSING if data_scope is MISSING else _role_data_scope_code(data_scope)
    )
    canonical_status = MISSING if status is MISSING else _validate_enable_status(status)
    execution_args = {
        key: value
        for key, value in {
            "role_id": role_id,
            "role_name": role_name,
            "role_desc": role_desc,
            "data_scope": data_scope_code,
            "status": canonical_status,
            "dept_ids": dept_ids,
        }.items()
        if key == "role_id" or value is not MISSING
    }
    payload = _model_validate_for_ai(
        RoleUpdate,
        {key: value for key, value in execution_args.items() if key != "role_id"},
        message="角色参数格式不合法",
        error_code="AI_ROLE_INPUT_INVALID",
    )
    preview = await role_management_service.preview_update(
        ctx.db,
        role_id,
        payload,
        actor_user_id=ctx.user.user_id,
        tenant=ctx.tenant,
    )
    target_role_name = getattr(preview, "target_role_name", None)
    return DryRunResult(
        ok=True,
        count=len(preview.member_user_ids),
        reason=f"将更新角色 {target_role_name or role_id}",
        summary_key="page.ai.chat.confirmRoleUpdateSummary",
        summary_params={"roleName": target_role_name or str(role_id)},
        confirmation_fields=[
            (
                _role_scope_confirmation_field(str(field["value"]))
                if field["label"] == "data_scope"
                else {
                    **field,
                    "display_value": target_role_name,
                }
                if field["label"] == "role_id" and target_role_name is not None
                else field
            )
            for field in _bound_confirmation_fields(
                execution_args,
                role_update.__ai_tool_meta__.args_summary_fields,
            )
        ],
        execution_args=execution_args,
        business_snapshot=preview.snapshot,
    )


@ai_tool(
    AiToolMeta(
        name="role.update_menus",
        agent="role_mgmt",
        summary="Replace one Role's complete menu set after approval.",
        required_perms=("system:role:menu-auth",),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("role_id", "menu_ids"),
    )
)
async def role_update_menus(
    ctx: AiToolContext,
    *,
    role_id: AiRoleId,
    menu_ids: AiRoleRelatedIds,
) -> ToolResult:
    """Execute an approved complete Role menu replacement."""
    snapshot = _require_role_snapshot(ctx)
    try:
        role = await role_management_service.update_menus(
            ctx.db,
            role_id,
            menu_ids,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
            tenant=ctx.tenant,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后角色事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    return _role_result(action="update_menus", role=role)


async def _dry_run_role_update_menus(
    ctx: AiToolContext,
    *,
    role_id: AiRoleId,
    menu_ids: AiRoleRelatedIds,
) -> Any:
    """Freeze a complete normalized menu set and member-wide impact."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    preview = await role_management_service.preview_update_menus(
        ctx.db,
        role_id,
        menu_ids,
        actor_user_id=ctx.user.user_id,
        tenant=ctx.tenant,
    )
    target_role_name = getattr(preview, "target_role_name", None)
    return DryRunResult(
        ok=True,
        count=len(preview.member_user_ids),
        reason=f"将更新角色 {target_role_name or role_id} 的完整菜单集合",
        summary_key="page.ai.chat.confirmRoleMenusSummary",
        summary_params={"roleName": target_role_name or str(role_id)},
        confirmation_fields=[
            {
                "label": "role_id",
                "value": role_id,
                "display_value": target_role_name or str(role_id),
            },
            {
                "label": "menu_ids",
                "value": menu_ids,
                "display_value": _confirmation_display(menu_ids),
            },
        ],
        execution_args={"role_id": role_id, "menu_ids": menu_ids},
        business_snapshot=preview.snapshot,
    )


@ai_tool(
    AiToolMeta(
        name="role.update_agents",
        agent="role_mgmt",
        summary="Replace one Role's complete Agent set after approval.",
        required_perms=("system:role:ai-agent-auth",),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
        args_summary_fields=("role_id", "agent_ids"),
    )
)
async def role_update_agents(
    ctx: AiToolContext,
    *,
    role_id: AiRoleId,
    agent_ids: AiRoleRelatedIds,
) -> ToolResult:
    """Execute an approved complete Role Agent replacement."""
    snapshot = _require_role_snapshot(ctx)
    try:
        role = await role_management_service.update_agents(
            ctx.db,
            role_id,
            agent_ids,
            actor_user_id=ctx.user.user_id,
            expected_snapshot=snapshot,
            tenant=ctx.tenant,
        )
    except BusinessException as exc:
        raise BusinessRuleException(
            "审批后角色事实已变化，请重新确认",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        ) from exc
    return _role_result(action="update_agents", role=role)


async def _dry_run_role_update_agents(
    ctx: AiToolContext,
    *,
    role_id: AiRoleId,
    agent_ids: AiRoleRelatedIds,
) -> Any:
    """Freeze a complete normalized Agent set and member-wide impact."""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    preview = await role_management_service.preview_update_agents(
        ctx.db,
        role_id,
        agent_ids,
        actor_user_id=ctx.user.user_id,
        tenant=ctx.tenant,
    )
    target_role_name = getattr(preview, "target_role_name", None)
    return DryRunResult(
        ok=True,
        count=len(preview.member_user_ids),
        reason=f"将更新角色 {target_role_name or role_id} 的完整 Agent 集合",
        summary_key="page.ai.chat.confirmRoleAgentsSummary",
        summary_params={"roleName": target_role_name or str(role_id)},
        confirmation_fields=[
            {
                "label": "role_id",
                "value": role_id,
                "display_value": target_role_name or str(role_id),
            },
            {
                "label": "agent_ids",
                "value": agent_ids,
                "display_value": _confirmation_display(agent_ids),
            },
        ],
        execution_args={"role_id": role_id, "agent_ids": agent_ids},
        business_snapshot=preview.snapshot,
    )
