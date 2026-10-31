"""Transaction-bound field decisions over individual, server-resolved role scopes.

The caller supplies trusted rules from a verified policy snapshot. This service
does not activate candidate rules or accept rules from an HTTP request.
"""

from dataclasses import dataclass, field
from types import MappingProxyType

from sqlalchemy import and_, case, select, true
from sqlalchemy.orm import selectinload

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _order_error
from app.modules.system.field_policy.evaluator import FieldGrant, resolve_record_fields
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)
from app.utils.data_scope import (
    get_user_filters_from_resolution,
    resolve_data_scope_for_roles,
)


def _limit():
    return BusinessException(
        422, "字段上下文超出预算", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    )


async def load_current_user(db, *, tenant, actor_user_version):
    await system_authorization_candidate._identity(db, tenant, actor_user_version)
    return await db.scalar(
        select(User)
        .where(
            User.tenant_id == tenant.tenant_id,
            User.user_id == tenant.actor_user_id,
        )
        .options(
            selectinload(User.roles).selectinload(Role.menus), selectinload(User.depts)
        )
        .execution_options(populate_existing=True)
    )


@dataclass(frozen=True, slots=True)
class UserFieldContext:
    transaction: object
    tenant_id: int
    grants: tuple
    base_predicates: tuple
    role_scopes: object
    _applicable: dict = field(default_factory=dict, repr=False, compare=False)

    async def decisions(self, db, tx, *, user_ids, operation, purpose, entrypoint):
        if tx is not self.transaction:
            raise _order_error()
        tx.assert_session(db)
        tx.assert_planning(self.tenant_id)
        if (
            type(user_ids) is not tuple
            or len(user_ids) > 100
            or any(type(uid) is not int or uid <= 0 for uid in user_ids)
            or len(set(user_ids)) != len(user_ids)
        ):
            raise _limit()
        if not user_ids:
            return MappingProxyType({})
        if len(set(user_ids) | self._applicable.keys()) > 100:
            raise _limit()
        missing = tuple(uid for uid in user_ids if uid not in self._applicable)
        roles = tuple(self.role_scopes)
        columns = [
            case((self.role_scopes[rid], True), else_=False).label(f"role_{index}")
            for index, rid in enumerate(roles)
        ]
        rows = (
            (
                await db.execute(
                    select(User.user_id, *columns)
                    .where(
                        User.tenant_id == self.tenant_id,
                        *self.base_predicates,
                        User.user_id.in_(missing),
                    )
                    .order_by(User.user_id)
                )
            ).all()
            if missing
            else ()
        )
        self._applicable.update(dict.fromkeys(missing))
        for row in rows:
            self._applicable[int(row[0])] = frozenset(
                rid for rid, applies in zip(roles, row[1:], strict=True) if applies
            )
        return MappingProxyType(
            {
                uid: resolve_record_fields(
                    catalog=USER_FIELDS,
                    trusted_grants=self.grants,
                    applicable_role_ids=self._applicable[uid],
                    operation=operation,
                    purpose=purpose,
                    entrypoint=entrypoint,
                )
                for uid in user_ids
                if self._applicable[uid] is not None
            }
        )


class UserFieldContextService:
    async def load(self, db, tx, *, tenant, actor_user_version, grants):
        tx.assert_session(db)
        tx.assert_planning(tenant.tenant_id)
        if type(grants) is not tuple or any(
            type(rule) is not FieldGrant for rule in grants
        ):
            raise _order_error()
        if len(grants) > 1024 * 256:
            raise _limit()
        actor = await load_current_user(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        enabled = tuple(role for role in actor.roles if role.status == "1")
        if len(enabled) > 1024:
            raise _limit()
        combined = await resolve_data_scope_for_roles(
            db, user=actor, roles=enabled, tenant=tenant
        )
        base = tuple(get_user_filters_from_resolution(combined))
        scopes = {}
        for role in enabled:
            individual = await resolve_data_scope_for_roles(
                db, user=actor, roles=(role,), tenant=tenant
            )
            scopes[str(role.role_id)] = and_(
                true(), *get_user_filters_from_resolution(individual)
            )
        return UserFieldContext(
            tx, tenant.tenant_id, grants, base, MappingProxyType(scopes)
        )


user_field_context = UserFieldContextService()
