"""Lock-free credential preparation and live policy checks for fixed AI tools."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.db.authorization_owner import rollback_authorization_session
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.core.context import AiToolContext
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.auth.service import load_live_user_authority
from app.modules.system.ai_tools.common import (
    _model_validate_for_ai,
    _validate_enable_status,
)
from app.modules.system.ai_tools.user_management import (
    AiUserRoleAssignment,
    _build_ai_user_create_schema,
    _get_ai_default_password,
    _load_ai_create_policy,
    _load_ai_reset_target,
    _normalize_user_role_assignment,
)
from app.modules.system.models.user import User
from app.modules.system.schemas.user import ResetPassword, UserToolUpdate
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)

USER_TOOLS = frozenset({"user.create", "user.reset_password", "user.update"})


@dataclass(frozen=True, slots=True, repr=False)
class UserPolicy:
    password: str | None
    role_assignment: str | None
    primary_dept_id: int | None


def invalid():
    return BusinessRuleException(
        "工具参数或策略已变化", error_code="AI_PREPARED_ACTION_BINDING_INVALID"
    )


async def ensure_targets_in_scope(ctx, *, user_ids=(), dept_ids=()):
    if user_ids:
        visible = set(
            await ctx.db.scalars(
                select(User.user_id).where(
                    User.user_id.in_(user_ids), *ctx.data_scope.filters
                )
            )
        )
        if visible != set(user_ids):
            raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")
    if (
        dept_ids
        and ctx.data_scope.accessible_dept_ids is not None
        and not set(dept_ids) <= ctx.data_scope.accessible_dept_ids
    ):
        raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")


async def context(db, tenant, tool):
    user = await load_live_user_authority(db, tenant=tenant)
    return AiToolContext(
        user=user,
        perms=collect_user_permission_codes(user),
        db=db,
        data_scope=await build_data_scope_context(db, user),
        trace_id="planned",
        tool_meta=ToolRegistry.get().find(tool).meta,
        tenant=tenant,
    )


async def prepare_user(db, *, tool, args, tenant):
    """Return schemas and private policy scalars; no hashing while a DB tx is open."""
    values = dict(args)
    target = values.pop("user_id", None) if tool != "user.create" else None
    if tool != "user.create" and (type(target) is not int or not 0 < target < 2**63):
        raise invalid()
    if tool == "user.update":
        if set(values) - {
            "nickname",
            "user_email",
            "user_phone",
            "user_gender",
            "status",
        }:
            raise invalid()
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            raise invalid()
        if "status" in values:
            values["status"] = _validate_enable_status(values["status"])
        return (
            target,
            _model_validate_for_ai(
                UserToolUpdate,
                values,
                message="用户资料无效",
                error_code="AI_USER_UPDATE_INVALID",
            ),
            UserPolicy(None, None, None),
        )
    if tool == "user.reset_password" and values:
        raise invalid()
    if tool == "user.create" and (
        "user_name" not in values
        or set(values)
        - {
            "user_name",
            "nickname",
            "user_email",
            "user_phone",
            "user_gender",
            "status",
            "primary_dept_id",
            "role_assignment",
        }
    ):
        raise invalid()
    primary = values.get("primary_dept_id")
    if primary is not None and (type(primary) is not int or not 0 < primary < 2**63):
        raise invalid()
    with bounded_authorization_reads(db):
        try:
            async with asyncio.timeout(5):
                ctx = await context(db, tenant, tool)
                if tool == "user.create":
                    assignment = _normalize_user_role_assignment(
                        values.pop("role_assignment", "DEFAULT")
                    )
                    if primary is not None:
                        await ensure_targets_in_scope(ctx, dept_ids=[primary])
                    _, _, password = await _load_ai_create_policy(
                        ctx, primary_dept_id=primary, role_assignment=assignment
                    )
                    payload = _build_ai_user_create_schema(
                        **(
                            {
                                "nickname": None,
                                "user_email": None,
                                "user_phone": None,
                                "user_gender": "0",
                                "status": "1",
                            }
                            | values
                        ),
                        default_password=password,
                    )
                    if assignment is AiUserRoleAssignment.NONE:
                        payload.role_ids = []
                else:
                    assignment = None
                    await ensure_targets_in_scope(ctx, user_ids=[target])
                    await _load_ai_reset_target(ctx, user_id=target)
                    password = await _get_ai_default_password(ctx)
                    payload = ResetPassword(new_password=password)
        finally:
            await rollback_authorization_session(db)
    return target, payload, UserPolicy(password, assignment, primary)


async def recheck_user(db, command):
    tool, tenant = command.tool, command.tenant
    ctx = await context(db, tenant, tool)
    policy = command.policy
    if tool == "user.create":
        if policy.primary_dept_id is not None:
            await ensure_targets_in_scope(ctx, dept_ids=[policy.primary_dept_id])
        dept, role, password = await _load_ai_create_policy(
            ctx,
            primary_dept_id=policy.primary_dept_id,
            role_assignment=policy.role_assignment,
        )
        if password != policy.password:
            raise invalid()
        return dept, role
    target = command.command.values[4]
    await ensure_targets_in_scope(ctx, user_ids=[target])
    if tool == "user.reset_password":
        await _load_ai_reset_target(ctx, user_id=target)
        if await _get_ai_default_password(ctx) != policy.password:
            raise invalid()
    return None, None
