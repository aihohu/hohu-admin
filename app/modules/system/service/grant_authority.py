"""Immutable authorization boundary shared by page APIs and AI tools."""

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
)
from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.rbac import is_super_admin
from app.core.tenant import resolve_tenant_id
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.utils.data_scope import resolve_data_scope


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class GrantAuthority:
    """A materialized, immutable upper bound for delegated authorization."""

    actor_user_id: int
    actor_status: str
    tenant_id: int
    super_admin: bool
    enabled_role_ids: frozenset[int]
    permission_codes: frozenset[str]
    menu_ids: frozenset[int]
    visible_agent_ids: frozenset[int]
    grantable_agent_ids: frozenset[int]
    scope_kinds: frozenset[str]
    accessible_dept_ids: frozenset[int] | None
    accessible_user_scope: frozenset[int] | None
    version_summary: str

    def allows_permission_codes(self, values: set[str] | frozenset[str]) -> bool:
        return self.super_admin or set(values) <= self.permission_codes

    def allows_menu_ids(self, values: set[int] | frozenset[int]) -> bool:
        return self.super_admin or set(values) <= self.menu_ids

    def allows_agent_ids(self, values: set[int] | frozenset[int]) -> bool:
        return self.super_admin or set(values) <= self.grantable_agent_ids

    def allows_scope_kind(
        self,
        scope_kind: str,
        custom_dept_ids: set[int] | frozenset[int],
    ) -> bool:
        """Apply the non-linear scope-template dominance table."""
        if self.super_admin or DATA_SCOPE_ALL in self.scope_kinds:
            return True
        if scope_kind == DATA_SCOPE_SELF:
            return bool(self.scope_kinds)
        if scope_kind == DATA_SCOPE_DEPT_AND_SUB:
            return DATA_SCOPE_DEPT_AND_SUB in self.scope_kinds
        if scope_kind == DATA_SCOPE_DEPT:
            return bool({DATA_SCOPE_DEPT, DATA_SCOPE_DEPT_AND_SUB} & self.scope_kinds)
        if scope_kind != DATA_SCOPE_CUSTOM:
            return False
        if not (
            {DATA_SCOPE_CUSTOM, DATA_SCOPE_DEPT, DATA_SCOPE_DEPT_AND_SUB}
            & self.scope_kinds
        ):
            return False
        return self.accessible_dept_ids is None or set(custom_dept_ids) <= set(
            self.accessible_dept_ids
        )

    def allows_materialized_scope(
        self,
        *,
        dept_ids: set[int] | frozenset[int],
        user_ids: set[int] | frozenset[int],
    ) -> bool:
        dept_allowed = self.accessible_dept_ids is None or set(dept_ids) <= set(
            self.accessible_dept_ids
        )
        user_allowed = self.accessible_user_scope is None or set(user_ids) <= set(
            self.accessible_user_scope
        )
        return dept_allowed and user_allowed

    def canonical_payload(self) -> dict[str, Any]:
        def _scope(values: frozenset[int] | None) -> dict[str, Any]:
            return (
                {"unbounded": True, "ids": []}
                if values is None
                else {"unbounded": False, "ids": sorted(values)}
            )

        return {
            "actorUserId": str(self.actor_user_id),
            "actorStatus": self.actor_status,
            "tenantId": str(self.tenant_id),
            "superAdmin": self.super_admin,
            "enabledRoleIds": sorted(self.enabled_role_ids),
            "permissionCodes": sorted(self.permission_codes),
            "menuIds": sorted(self.menu_ids),
            "visibleAgentIds": sorted(self.visible_agent_ids),
            "grantableAgentIds": sorted(self.grantable_agent_ids),
            "scopeKinds": sorted(self.scope_kinds),
            "accessibleDepartments": _scope(self.accessible_dept_ids),
            "accessibleUsers": _scope(self.accessible_user_scope),
        }


class GrantAuthorityService:
    """Build live authority from one reloaded principal without committing."""

    async def build(self, db: AsyncSession, actor_user_id: int) -> GrantAuthority:
        actor = await db.scalar(
            select(User)
            .where(User.user_id == actor_user_id)
            .options(
                selectinload(User.roles).selectinload(Role.menus),
                selectinload(User.roles).selectinload(Role.depts),
                selectinload(User.depts),
            )
            .execution_options(populate_existing=True)
        )
        if actor is None:
            raise NotFoundException("用户")
        if actor.status != STATUS_ENABLED:
            raise AuthorizationException(
                "账号已被禁用",
                error_code="ACCOUNT_DISABLED",
            )

        roles = [role for role in actor.roles if role.status == STATUS_ENABLED]
        menu_ids = {int(menu.menu_id) for role in roles for menu in role.menus}
        permission_codes = collect_user_permission_codes(actor)
        resolution = await resolve_data_scope(db, actor)
        visible_agents = await agent_authorization_service.list_agents(db, actor)
        grantable_agent_ids = await agent_authorization_service.grantable_agent_ids(
            db,
            actor,
        )
        agent_bindings = (
            await db.execute(
                select(
                    RoleAiAgent.role_id,
                    RoleAiAgent.agent_id,
                    RoleAiAgent.enabled,
                )
                .where(RoleAiAgent.role_id.in_(int(role.role_id) for role in roles))
                .order_by(RoleAiAgent.role_id, RoleAiAgent.agent_id)
            )
        ).all()
        if resolution.accessible_user_scope is None:
            accessible_user_ids = None
        else:
            accessible_user_ids = frozenset(
                int(user_id)
                for user_id in (
                    await db.execute(resolution.accessible_user_scope)
                ).scalars()
            )

        authority = GrantAuthority(
            actor_user_id=int(actor.user_id),
            actor_status=str(actor.status),
            tenant_id=resolve_tenant_id(actor),
            super_admin=is_super_admin(actor),
            enabled_role_ids=frozenset(int(role.role_id) for role in roles),
            permission_codes=frozenset(permission_codes),
            menu_ids=frozenset(menu_ids),
            visible_agent_ids=frozenset(
                int(agent.agent_id) for agent in visible_agents
            ),
            grantable_agent_ids=frozenset(grantable_agent_ids),
            scope_kinds=resolution.scope_kinds,
            accessible_dept_ids=resolution.accessible_dept_ids,
            accessible_user_scope=accessible_user_ids,
            version_summary="",
        )
        version_payload = {
            "authority": authority.canonical_payload(),
            "roleDefinitions": [
                {
                    "roleId": str(role.role_id),
                    "roleCode": role.role_code,
                    "status": role.status,
                    "dataScope": role.data_scope,
                    "menuIds": sorted(int(menu.menu_id) for menu in role.menus),
                    "customDeptIds": sorted(int(dept.dept_id) for dept in role.depts),
                }
                for role in sorted(roles, key=lambda item: item.role_id)
            ],
            "actorDeptIds": sorted(int(dept.dept_id) for dept in actor.depts),
            "agentBindings": [
                {
                    "roleId": str(role_id),
                    "agentId": str(agent_id),
                    "enabled": bool(enabled),
                }
                for role_id, agent_id, enabled in agent_bindings
            ],
        }
        return replace(
            authority,
            version_summary=_canonical_hash(version_payload),
        )


grant_authority_service = GrantAuthorityService()
