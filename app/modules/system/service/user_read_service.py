"""Bounded user reads and explicit DTOs under the authorization barriers.

The current database schema supports legacy/ready only. Compatibility grants
below preserve that behavior; they are never active field-policy rules. There
is no mode, purpose or rule parameter on the production read owner.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import AuthorizationException, BusinessException
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _order_error
from app.db.base import user_depts
from app.modules.auth.permission_collect import (
    collect_user_buttons,
    collect_user_identity,
)
from app.modules.system.field_policy.dto import (
    USER_OUTPUT_SOURCES,
    project_user_dto,
    resolve_own_account_fields,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.query_plan import UserQueryPlan
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.login_log import LoginLogQuery
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.modules.system.schemas.user import UserQuery
from app.modules.system.schemas.user_projection import UserMemberQuery
from app.modules.system.service.mail_service import mail_service, ready
from app.modules.system.service.settings_service import settings_service
from app.modules.system.service.user_copy_read_service import user_copy_read_service
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from app.modules.system.service.user_field_compatibility import (
    require_legacy_field_policy,
)
from app.modules.system.service.user_field_context import (
    load_current_user,
    user_field_context,
)
from app.modules.system.service.user_query_coverage import assert_user_query_coverage
from app.utils.pagination import build_filters


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


async def _capability_decisions(db, tx, context, ids):
    result = {uid: [] for uid in ids}
    for operation, purpose in (
        ("create", "management.create"),
        ("update", "management.update"),
        ("replace", "management.relations"),
        ("contains", "management.query"),
        ("eq", "management.query"),
    ):
        decisions = await context.decisions(
            db,
            tx,
            user_ids=ids,
            operation=operation,
            purpose=purpose,
            entrypoint="user-list",
        )
        for uid, decision in decisions.items():
            result[uid].append(decision)
    return {uid: tuple(items) for uid, items in result.items()}


def _scalar_values(user, view):
    return {
        source.field_id: getattr(user, source.field_id)
        for source in USER_OUTPUT_SOURCES[view].values()
        if source.field_id not in {"roles", "departments", "email_verified"}
    }


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
        return await user_field_context.load(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            grants=_legacy_rules(role_ids),
        )

    async def execute(
        self, db, tx, *, tenant, actor_user_version, operation, query=None
    ):
        tx.assert_session(db)
        tx.assert_planning(tenant.tenant_id)
        await require_legacy_field_policy(db, tx, tenant_id=tenant.tenant_id)
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
        actor = await self.actor(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        permissions = frozenset(collect_user_buttons(actor)) | (
            frozenset({"*"}) if is_super_admin(actor) else frozenset()
        )
        if operation in {"operation-logs", "login-logs"}:
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
        if operation == "members":
            required = {"system:user:list", "system:dept:list", "system:dept:edit"}
            if not is_super_admin(actor) and not required <= set(
                collect_user_buttons(actor)
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
            await assert_user_query_coverage(
                db,
                tx,
                tenant_id=tenant.tenant_id,
                base_predicates=context.base_predicates,
                plan=UserQueryPlan.for_members(
                    search=bool((query.query or "").strip())
                ),
                grants=context.grants,
                role_scopes=context.role_scopes,
            )
            page = await user_department_assignment_service.list_department_members(
                db,
                actor_user_id=tenant.actor_user_id,
                tenant=tenant,
                **query.model_dump(),
            )
            decisions = await context.decisions(
                db,
                tx,
                user_ids=tuple(record.user_id for record in page.records),
                operation="read",
                purpose="management.read",
                entrypoint="user-list",
            )
            capabilities = await _capability_decisions(
                db, tx, context, tuple(record.user_id for record in page.records)
            )
            records = [
                project_user_dto(
                    view="members",
                    decisions=decisions[record.user_id],
                    capability_decisions=capabilities[record.user_id],
                    function_permissions=permissions,
                    values={
                        "user_id": record.user_id,
                        "user_name": record.user_name,
                        "nickname": record.nickname,
                        "status": record.status,
                        "departments": {
                            "is_member": record.is_member,
                            "is_primary": record.is_primary,
                        },
                    },
                )
                for record in page.records
            ]
            return {
                "records": records,
                "total": page.total,
                "current": page.current,
                "size": page.size,
            }
        if operation == "list":
            if not is_super_admin(
                actor
            ) and "system:user:list" not in collect_user_buttons(actor):
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
            return await self.user_list(
                db,
                tx,
                tenant=tenant,
                context=context,
                query=query,
                permissions=permissions,
            )
        decisions = resolve_own_account_fields(
            actor=(tenant.tenant_id, tenant.actor_user_id),
            target=(int(actor.tenant_id), int(actor.user_id)),
            grants=(),
            applicable_role_ids=frozenset(),
            operation="read",
        )
        writes = resolve_own_account_fields(
            actor=(tenant.tenant_id, tenant.actor_user_id),
            target=(int(actor.tenant_id), int(actor.user_id)),
            grants=(),
            applicable_role_ids=frozenset(),
            operation="update",
        )
        if operation == "profile":
            values = _scalar_values(actor, "profile")
            values["roles"] = [role.role_name for role in actor.roles]
            values["create_time"] = (
                actor.create_time.strftime(settings.DATETIME_FORMAT)
                if actor.create_time
                else None
            )
            return project_user_dto(
                view="profile",
                values=values,
                decisions=decisions,
                capability_decisions=(writes,),
            )
        if operation == "email":
            result = project_user_dto(
                view="email",
                values={
                    "user_email": actor.user_email,
                    "email_verified": actor.email_verified_at is not None,
                },
                decisions=decisions,
                capability_decisions=(writes,),
            )
            values = await settings_service.values(db, tenant_id=tenant.tenant_id)
            result.update(
                enabled=ready(await mail_service.config(db)),
                codeMinutes=values["email_code_minutes"],
            )
            return result
        if operation == "capabilities":
            return project_user_dto(
                view="profile",
                values={},
                decisions=decisions,
                capability_decisions=(writes,),
            )
        return {
            **collect_user_identity(actor),
            **project_user_dto(
                view="avatar",
                values={"user_avatar": actor.user_avatar},
                decisions=decisions,
                capability_decisions=(writes,),
            ),
        }

    async def user_list(self, db, tx, *, tenant, context, query, permissions):
        base = context.base_predicates
        supplied = frozenset(
            name
            for name, value in query.model_dump().items()
            if name not in {"current", "size"} and value not in (None, "")
        )
        plan = UserQueryPlan.for_list(supplied)
        await assert_user_query_coverage(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            base_predicates=base,
            plan=plan,
            grants=context.grants,
            role_scopes=context.role_scopes,
        )
        mapping = {
            "user_name": ("user_name", "contains"),
            "nickname": ("nickname", "contains"),
            "user_gender": ("user_gender", "=="),
            "user_phone": ("user_phone", "contains"),
            "user_email": ("user_email", "contains"),
            "status": ("status", "=="),
            "role_code": lambda model, value: model.roles.any(
                (Role.tenant_id == tenant.tenant_id) & (Role.role_code == value)
            ),
        }
        filters = (
            User.tenant_id == tenant.tenant_id,
            *base,
            *build_filters(User, mapping, **query.model_dump()),
        )
        total = int(
            await db.scalar(
                select(func.pg_catalog.count(1)).select_from(User).where(*filters)
            )
            or 0
        )
        users = list(
            (
                await db.scalars(
                    select(User)
                    .where(*filters)
                    .options(
                        selectinload(User.roles).noload("*"),
                        selectinload(User.depts).noload("*"),
                    )
                    .order_by(User.create_time.desc(), User.user_id.desc())
                    .offset((query.current - 1) * query.size)
                    .limit(query.size)
                )
            ).all()
        )
        memberships = {}
        if users:
            for uid, did, primary in await db.execute(
                select(
                    user_depts.c.user_id,
                    user_depts.c.dept_id,
                    user_depts.c.is_primary,
                ).where(
                    user_depts.c.tenant_id == tenant.tenant_id,
                    user_depts.c.user_id.in_([user.user_id for user in users]),
                )
            ):
                memberships.setdefault(uid, []).append((did, primary == "Y"))
        decisions = await context.decisions(
            db,
            tx,
            user_ids=tuple(int(user.user_id) for user in users),
            operation="read",
            purpose="management.read",
            entrypoint="user-list",
        )
        capabilities = await _capability_decisions(
            db, tx, context, tuple(int(user.user_id) for user in users)
        )
        records = []
        for user in users:
            values = _scalar_values(user, "list")
            values["create_time"] = (
                user.create_time.strftime(settings.DATETIME_FORMAT)
                if user.create_time
                else None
            )
            values["roles"] = {
                "roles": [role.role_code for role in user.roles],
                "role_names": [role.role_name for role in user.roles],
                "role_name_keys": [
                    (role.i18n_keys or {}).get("roleName") for role in user.roles
                ],
            }
            pairs = sorted(memberships.get(user.user_id, ()))
            values["departments"] = {
                "dept_ids": [str(dept.dept_id) for dept in user.depts],
                "dept_names": ", ".join(dept.dept_name for dept in user.depts),
                "primary_dept": next(
                    (str(did) for did, primary in pairs if primary), None
                ),
                "user_depts": [
                    {"dept_id": str(did), "is_primary": primary}
                    for did, primary in pairs
                ],
            }
            records.append(
                project_user_dto(
                    view="list",
                    values=values,
                    decisions=decisions[int(user.user_id)],
                    capability_decisions=capabilities[int(user.user_id)],
                    function_permissions=permissions,
                )
            )
        return {
            "records": records,
            "total": total,
            "current": query.current,
            "size": query.size,
        }


user_read_service = UserReadService()
