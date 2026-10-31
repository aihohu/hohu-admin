"""Bounded HTTP audit facts do not grant business authorization."""

from sqlalchemy import select

from app.core.authorization_plan import (
    LockPlan,
    NewResource,
    ResourceKey,
    TenantLock,
    TransactionBudget,
)
from app.core.authorization_plan import ResourceKind as K
from app.core.id_generator import next_id
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class HttpAuditOwner:
    def __init__(self, factory):
        self.factory = factory

    async def username(self, *, tenant_id, user_id):
        return await self._run(tenant_id, user_id, None)

    async def login(
        self,
        *,
        tenant_id,
        audit_scope,
        user_id,
        username,
        ip,
        user_agent,
        status,
        message,
    ):
        unresolved = tenant_id is None
        if (
            (
                not unresolved
                and (type(tenant_id) is not int or not 0 <= tenant_id < 2**63)
            )
            or audit_scope != ("unresolved" if unresolved else "tenant")
            or (
                user_id is not None
                and (type(user_id) is not int or not 0 < user_id < 2**63)
            )
            or status not in ({"2", "3"} if unresolved else {"1", "2", "3"})
            or (unresolved and user_id is not None)
        ):
            raise _order_error()
        values = {
            "audit_scope": audit_scope,
            "user_id": user_id,
            "username": username,
            "ip": ip,
            "user_agent": user_agent,
            "status": status,
            "message": message,
        }
        for name, limit in {
            "username": 50,
            "ip": 50,
            "user_agent": 500,
            "message": 200,
        }.items():
            value = values[name]
            if value is None and name != "username":
                continue
            if type(value) is not str or len(value) > limit:
                raise _order_error()
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=() if unresolved else (TenantLock(tenant_id),),
                        budget=TransactionBudget(
                            timeout_ms=2000,
                            lock_timeout_ms=500,
                            statement_timeout_ms=500,
                        ),
                    ) as tx:
                        key = ResourceKey(
                            K.UNRESOLVED_LOGIN_LOG if unresolved else K.LOGIN_LOG,
                            tenant_id,
                            next_id(),
                        )
                        await tx.lock(
                            LockPlan(
                                existing=()
                                if unresolved
                                else (ResourceKey(K.TENANT, None, tenant_id),),
                                new=(NewResource(key),),
                            )
                        )
                        if unresolved:
                            await tx.append_audit(key, values | {"tenant_id": None})
                        else:
                            await tx.append_audit(key, values)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)

    async def operation(
        self,
        *,
        tenant_id,
        user_id,
        username,
        module,
        action,
        method,
        path,
        request_params,
        status_code,
        ip,
        duration,
    ):
        values = {
            "username": username,
            "module": module,
            "action": action,
            "method": method,
            "path": path,
            "request_params": request_params,
            "status_code": status_code,
            "ip": ip,
            "duration": duration,
            "user_id": user_id,
            "audit_scope": "tenant",
        }
        for name, limit in {
            "username": 50,
            "module": 50,
            "action": 20,
            "method": 10,
            "path": 200,
            "request_params": 2020,
            "ip": 50,
        }.items():
            value = values[name]
            if value is None and name in {"request_params", "ip"}:
                continue
            if type(value) is not str or len(value) > limit:
                raise _order_error()
        if (
            type(status_code) is not int
            or not 100 <= status_code <= 599
            or type(duration) is not int
            or not 0 <= duration < 2**31
        ):
            raise _order_error()
        return await self._run(tenant_id, user_id, values)

    async def _run(self, tenant_id, user_id, values):
        if (
            type(tenant_id) is not int
            or not 0 <= tenant_id < 2**63
            or type(user_id) is not int
            or not 0 < user_id < 2**63
        ):
            raise _order_error()
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant_id),),
                        budget=TransactionBudget(
                            timeout_ms=1000 if values is None else 2000,
                            lock_timeout_ms=500,
                            statement_timeout_ms=500,
                        ),
                    ) as tx:
                        tenant = await db.get(Tenant, tenant_id)
                        if tenant is None:
                            raise _order_error()
                        user = await db.scalar(
                            select(User).where(
                                User.tenant_id == tenant_id, User.user_id == user_id
                            )
                        )
                        keys = [ResourceKey(K.TENANT, None, tenant_id)]
                        if user is not None:
                            keys.append(ResourceKey(K.USER, tenant_id, user_id))
                        key = (
                            ResourceKey(K.SYSTEM_OPERATION_LOG, tenant_id, next_id())
                            if values is not None
                            else None
                        )
                        await tx.lock(
                            LockPlan(
                                existing=tuple(keys),
                                new=(NewResource(key),) if key else (),
                            )
                        )
                        if values is not None:
                            await tx.append_audit(key, values)
                            result = None
                        else:
                            current = (
                                await db.get(User, user_id, populate_existing=True)
                                if user is not None
                                else None
                            )
                            result = (
                                current.user_name
                                if current is not None
                                and current.tenant_id == tenant_id
                                else None
                            )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if values is None:
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
