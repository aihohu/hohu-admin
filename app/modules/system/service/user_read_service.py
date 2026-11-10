"""Bounded user reads and explicit DTOs under the authorization barriers.

Only never-enforced legacy/ready states use compatibility grants. Active reads
load verified immutable policy rules; unavailable policies fail closed. The
production read owner accepts no client-selected mode, purpose or rules.
"""

from app.core.exceptions import AuthorizationException, BusinessException
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _order_error
from app.modules.auth.permission_collect import (
    collect_user_buttons,
    collect_user_identity,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.schemas.login_log import LoginLogQuery
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.modules.system.schemas.user import UserQuery
from app.modules.system.schemas.user_projection import UserMemberQuery
from app.modules.system.service.relation_user_read import read_users
from app.modules.system.service.user_copy_read_service import user_copy_read_service
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_field_context import (
    load_current_user,
    user_field_context,
)


def _legacy_rules(role_ids):
    return (
        tuple(
            FieldGrant(role_id, name, "read", "management.read")
            for role_id in role_ids
            for name, field in USER_FIELDS.items()
            if field.readable
        )
        + tuple(
            FieldGrant(role_id, name, operation, "management.query")
            for role_id in role_ids
            for name, field in USER_FIELDS.items()
            if field.query_purposes is None
            for operation in sorted(field.query_ops)
        )
        + tuple(
            FieldGrant(
                role_id,
                name,
                operation,
                "management.relations"
                if operation == "replace"
                else f"management.{operation}",
            )
            for role_id in role_ids
            for name, field in USER_FIELDS.items()
            for operation in sorted(field.writable)
        )
    )


class UserReadService:
    async def actor(self, db, *, tenant, actor_user_version):
        return await load_current_user(
            db, tenant=tenant, actor_user_version=actor_user_version
        )

    async def legacy_context(self, db, tx, *, tenant, actor_user_version, actor):
        role_ids = tuple(
            str(role.role_id) for role in actor.roles if role.status == "1"
        )
        if len(role_ids) > 1024:
            raise BusinessException(
                422,
                "字段上下文超出预算",
                error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
            )
        active = await active_policy_grants(
            db, tx, tenant_id=tenant.tenant_id, role_ids=role_ids
        )
        return await user_field_context.load(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            grants=_legacy_rules(role_ids) if active is None else active,
        )

    async def execute(
        self, db, tx, *, tenant, actor_user_version, operation, query=None
    ):
        tx.assert_session(db)
        tx.assert_planning(tenant.tenant_id)
        if operation not in {
            "list",
            "profile",
            "email",
            "auth",
            "capabilities",
            "members",
            "operation-logs",
            "login-logs",
        }:
            raise _order_error()
        if (
            (operation == "list" and type(query) is not UserQuery)
            or (
                operation not in {"list", "members", "operation-logs", "login-logs"}
                and query is not None
            )
            or (operation == "members" and type(query) is not UserMemberQuery)
            or (operation == "operation-logs" and type(query) is not OperationLogQuery)
            or (operation == "login-logs" and type(query) is not LoginLogQuery)
        ):
            raise _order_error()
        if operation == "list":
            return await self.user_list(
                db,
                tx,
                tenant=tenant,
                actor_user_version=actor_user_version,
                query=query,
            )
        if operation in {"operation-logs", "login-logs"}:
            actor = await self.actor(
                db, tenant=tenant, actor_user_version=actor_user_version
            )
            permission = (
                "monitor:operation-log:list"
                if operation == "operation-logs"
                else "monitor:login-log:list"
            )
            if not is_super_admin(actor) and permission not in collect_user_buttons(
                actor
            ):
                raise AuthorizationException(
                    "缺少操作权限", error_code="MISSING_PERMISSION"
                )
            context = await self.legacy_context(
                db,
                tx,
                tenant=tenant,
                actor_user_version=actor_user_version,
                actor=actor,
            )
            return await user_copy_read_service.audit_page(
                db, tx, tenant=tenant, context=context, operation=operation, query=query
            )
        try:
            return await read_users(
                db,
                tx,
                tenant=tenant,
                actor_user_version=actor_user_version,
                operation=operation,
                query=query,
            )
        except BusinessException as exc:
            if operation == "auth" and exc.error_code == "FIELD_POLICY_UNAVAILABLE":
                actor = await self.actor(
                    db, tenant=tenant, actor_user_version=actor_user_version
                )
                return collect_user_identity(actor)
            raise

    async def user_list(self, db, tx, *, tenant, actor_user_version, query):
        return await read_users(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation="list",
            query=query,
        )


user_read_service = UserReadService()
