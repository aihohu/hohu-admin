"""Current user projection for audited identity copies with stable source IDs."""

from sqlalchemy import false, literal, select

from app.db.authorization_plan_session import _order_error
from app.modules.system.field_policy.lineage import (
    UserFieldSource,
    project_identity_copy,
)
from app.modules.system.field_policy.query_plan import (
    QueryDependency,
    query_denied,
    query_permission_predicate,
)
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User
from app.modules.system.schemas.login_log import LoginLogOut
from app.modules.system.schemas.operation_log import OperationLogOut
from app.modules.system.service.login_log_service import login_log_service
from app.modules.system.service.operation_log_service import operation_log_service
from app.modules.system.service.user_field_compatibility import (
    require_legacy_field_policy,
)


class UserCopyReadService:
    async def audit_page(self, db, tx, *, tenant, context, operation, query):
        """Preserve legacy/ready audit facts; no candidate policy changes delivery."""
        await require_legacy_field_policy(db, tx, tenant_id=tenant.tenant_id)
        if context.transaction is not tx or context.tenant_id != tenant.tenant_id:
            raise _order_error()
        if operation not in {"operation-logs", "login-logs"}:
            raise _order_error()
        schema, service = (
            (LoginLogOut, login_log_service)
            if operation == "login-logs"
            else (OperationLogOut, operation_log_service)
        )
        page = await service.get_list(db, query, tenant=tenant)
        return {
            "records": [
                schema.model_validate(row).model_dump(mode="json", by_alias=True)
                for row in page.records
            ],
            "total": page.total,
            "current": page.current,
            "size": page.size,
        }

    async def projected_audit_page(self, db, tx, *, tenant, context, operation, query):
        """Strict source-aware projection, not a production activation entrypoint."""
        tx.assert_session(db)
        # Audit appends hold B shared. Require B exclusive from the beginning:
        # no new parent row may enter between source coverage and filtered SQL.
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        if (
            context.transaction is not tx
            or context.tenant_id != tenant.tenant_id
            or operation not in {"operation-logs", "login-logs"}
        ):
            raise _order_error()
        login = operation == "login-logs"
        model, schema, service = (
            (SysLoginLog, LoginLogOut, login_log_service)
            if login
            else (SysOperationLog, OperationLogOut, operation_log_service)
        )
        if query.username:
            permitted = query_permission_predicate(
                QueryDependency("user_name", "contains", "filter"),
                grants=context.grants,
                role_scopes=context.role_scopes,
            )
            source = (
                select(User.user_id)
                .where(
                    User.tenant_id == tenant.tenant_id,
                    *context.base_predicates,
                    User.user_id == model.user_id,
                    permitted,
                )
                .correlate(model)
                .exists()
            )
            base = (model.tenant_id == tenant.tenant_id,)
            if login:
                base += (model.audit_scope == "tenant",)
                # Historical login input may have been a username or an email.
                # A user ID alone does not prove which canonical field it copied.
                source = false()
            # A nullable source ID cannot itself be used as the sentinel.
            missing_row = await db.scalar(
                select(literal(1)).select_from(model).where(*base, ~source).limit(1)
            )
            if missing_row is not None:
                raise query_denied()
        page = await service.get_list(db, query, tenant=tenant)
        user_ids = tuple(
            sorted(
                {row.user_id for row in page.records if row.user_id and row.user_id > 0}
            )
        )
        decisions = await context.decisions(
            db,
            tx,
            user_ids=user_ids,
            operation="read",
            purpose="management.read",
            entrypoint="user-list",
        )
        records = []
        for row in page.records:
            result = schema.model_validate(row).model_dump(mode="json", by_alias=True)
            result.pop("username", None)
            # Neither free-text column has trustworthy per-field lineage.
            result.pop("message" if login else "requestParams", None)
            source = (
                UserFieldSource(
                    tenant.tenant_id, row.user_id, "user_name", "management.read"
                )
                if not login and row.user_id in decisions
                else None
            )
            copied = project_identity_copy(
                source=source,
                current_identity=(tenant.tenant_id, row.user_id),
                current_values={"user_name": row.username}
                if row.user_id in decisions
                else {},
                decisions=decisions.get(row.user_id),
            )
            if copied.visible:
                result["username"] = copied.value
            records.append(result)
        return {
            "records": records,
            "total": page.total,
            "current": page.current,
            "size": page.size,
        }


user_copy_read_service = UserCopyReadService()
