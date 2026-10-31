"""Protected Auth candidate application; commits belong to candidate_owner."""

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.modules.system.service.auth_candidate_account_service import (
    auth_candidate_accounts,
)
from app.modules.system.service.authorization_transaction_service import _Transaction


class AuthCandidateService:
    async def apply(
        self,
        db,
        tx,
        prepared,
        operation,
        *,
        payload=None,
        password_hash=None,
        new_user_id=None,
    ):
        if (
            not isinstance(db, AuthorizationPlanSession)
            or type(tx) is not _Transaction
            or tx._db is not db
        ):
            raise _order_error()
        tx.assert_held()
        current = await auth_candidate_accounts.read(
            db,
            prepared.tenant_id,
            user_id=prepared.account.user_id if prepared.account else None,
            user_name=payload.user_name
            if prepared.account is None
            and payload is not None
            and hasattr(payload, "user_name")
            else None,
            policy=prepared.mail is not None,
        )
        if current != prepared:
            raise BusinessException(
                409,
                "Account authorization changed",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        if operation in {"login", "refresh", "current", "email"}:
            return current
        return await auth_candidate_accounts.apply(
            tx,
            current,
            operation,
            payload=payload,
            password_hash=password_hash,
            new_user_id=new_user_id,
        )


auth_candidate_service = AuthCandidateService()
