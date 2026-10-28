"""Explicit builtin read adapters under current identity and source authority."""

import json
from copy import deepcopy
from types import MappingProxyType

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.core.context import AiToolContext
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.system.ai_tools import (
    analytics,
    dept,
    role,
    user_assignment,
    user_management,
    user_transfer,
)

READ_HANDLERS = MappingProxyType(
    {
        "user.list": user_management.user_list,
        "user.lookup": user_management.user_lookup,
        "user.count": analytics.user_count,
        "user.stats": analytics.user_stats,
        "user.distinct": analytics.user_distinct,
        "user.role_lookup": user_assignment.user_role_lookup,
        "user.dept_lookup": dept.user_dept_lookup,
        "role.list": role.role_list,
        "role.lookup": role.role_lookup,
        "role.menu_lookup": role.role_menu_lookup,
        "role.agent_lookup": role.role_agent_lookup,
        "role.count": analytics.role_count,
        "dept.list": dept.dept_list,
        "dept.lookup": dept.dept_lookup,
        "dept.count": analytics.dept_count,
    }
)

PREFLIGHT_HANDLERS = MappingProxyType(
    {
        "user.create": user_management._dry_run_user_create,
        "user.update": user_management._dry_run_user_update,
        "user.reset_password": user_management._dry_run_user_reset_password,
        "user.batch_delete": user_management._dry_run_user_batch_delete,
        "user.update_dept": user_assignment._dry_run_user_update_dept,
        "user.update_roles": user_assignment._dry_run_user_update_roles,
        "role.create": role._dry_run_role_create,
        "role.update": role._dry_run_role_update,
        "role.update_menus": role._dry_run_role_update_menus,
        "role.update_agents": role._dry_run_role_update_agents,
        "dept.create": dept._dry_run_dept_create,
        "dept.update": dept._dry_run_dept_update,
        "dept.move": dept._dry_run_dept_move,
        "user.export": user_transfer._dry_run_user_export,
    }
)


class GatewayReadCandidate:
    async def read_tool(self, db, tx, **request):
        return await self._read(db, tx, preflight=False, **request)

    async def preflight(self, db, tx, **request):
        return await self._read(db, tx, preflight=True, **request)

    async def _read(
        self,
        db,
        tx,
        *,
        preflight,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        agent_code,
        scope_hash,
        tool,
        args,
    ):
        handler = (PREFLIGHT_HANDLERS if preflight else READ_HANDLERS).get(tool)
        registered = ToolRegistry.get().find(tool)
        if (
            handler is None
            or registered is None
            or registered.meta.readonly is preflight
            or registered.meta.agent != agent_code
            or type(args) is not dict
            or len(json.dumps(args, ensure_ascii=False).encode()) > 256 * 1024
            or type(source_id) is not int
            or source_id <= 0
            or type(trace_id) is not str
            or not 1 <= len(trace_id) <= 64
            or type(scope_hash) is not str
            or len(scope_hash) != 64
        ):
            raise _order_error()
        args = deepcopy(args)
        agent_id = await db.scalar(
            select(AiAgent.agent_id).where(AiAgent.code == agent_code)
        )
        if agent_id is None:
            raise AuthorizationException(error_code="AI_AGENT_FORBIDDEN")
        _, actor = await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            extra=(ResourceKey(K.AGENT, None, agent_id),),
        )
        source = await db.get(AiMessage, source_id, populate_existing=True)
        if (
            source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.trace_id != trace_id
            or source.role != "user"
            or not source.is_active
        ):
            raise _order_error()
        permissions = agent_authorization_service.tool_permissions(actor)
        allowed = await agent_authorization_service.list_agents(
            db, actor, force_refresh=True
        )
        enabled = await get_ai_config_str_list(
            db,
            "ai:enabled_tools",
            default=[],
            tenant=tenant,
            force_refresh=True,
        )
        meta = registered.meta
        if (
            agent_id not in {item.agent_id for item in allowed}
            or not set(meta.required_perms) <= permissions
            or (meta.super_admin_only and not is_super_admin(actor))
            or (not meta.default_enabled and tool not in enabled)
            or scope_hash
            != await result_projection_service.compute_data_scope_hash(db, actor)
        ):
            raise AuthorizationException(error_code="AI_TOOL_PERM_DENIED")
        dependencies = result_projection_service.projection_dependency_message_ids(
            source
        )
        if (
            dependencies is None
            or not await result_projection_service._authorize_projection_dependencies(
                db,
                actor,
                owner_user_id=tenant.actor_user_id,
                dependency_message_ids=dependencies,
            )
        ):
            raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")
        ctx = AiToolContext(
            user=actor,
            perms=permissions,
            db=db,
            data_scope=await build_data_scope_context(db, actor),
            trace_id=trace_id,
            tenant=tenant,
            tool_meta=meta,
            data_scope_hash=scope_hash,
            projection_dependency_message_ids=dependencies,
        )
        return await handler(ctx, **args)


gateway_read_candidate = GatewayReadCandidate()
