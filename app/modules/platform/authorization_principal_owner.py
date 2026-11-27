"""Independent Principal owners, including explicitly fenced offline recovery."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.authorization_io import hash_password
from app.core.authorization_maintenance import require_maintenance
from app.core.authorization_plan import (
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TransactionBudget,
)
from app.core.exceptions import BusinessException, BusinessRuleException
from app.core.id_generator import next_id
from app.db.authorization_control_owner import _rollback_preserving
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.platform.authorization_principal_audit import (
    consume_principal_audit,
    issue_principal_audit,
)
from app.modules.platform.authorization_principal_candidate import (
    PrincipalIdentity,
    assert_principal,
    check_password_preparation,
    prepare_password,
    principal_authorization_candidate,
    read_principal,
    verify_preparation,
)
from app.modules.platform.constants import (
    ASSIGNABLE_PLATFORM_PERMISSIONS,
    PLATFORM_PRINCIPAL_NAME_RE,
    PLATFORM_PRINCIPAL_PERMISSION_REPLACE,
)
from app.modules.platform.models import PlatformPrincipal
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


def _permissions(values):
    if (
        type(values) not in {list, tuple}
        or not 1 <= len(values) <= 256
        or any(
            type(value) is not str or value not in ASSIGNABLE_PLATFORM_PERMISSIONS
            for value in values
        )
    ):
        raise BusinessRuleException(
            "平台权限列表无效", error_code="PLATFORM_PRINCIPAL_INVALID"
        )
    return tuple(sorted(set(values)))


def _factory(db, factory):
    if factory is not None:
        return factory
    if not isinstance(db.bind, AsyncEngine):
        raise _order_error()
    return async_sessionmaker(
        db.bind,
        class_=AuthorizationPlanSession,
        expire_on_commit=False,
        autoflush=False,
    )


class PrincipalCandidateOwner:
    async def login(self, db, *, principal_name, password):
        require_candidate_session(db)
        try:
            preparation = await prepare_password(db, principal_name=principal_name)
            await rollback_authorization_session(db)
            await verify_preparation(preparation, password)
            result = await principal_authorization_candidate.login(
                db, preparation=preparation
            )
        except BaseException as error:
            await _rollback_preserving(db, error)
            raise
        await commit_authorization_session(db)
        return result

    async def current(self, db, *, token):
        require_candidate_session(db)
        try:
            result = await principal_authorization_candidate.current(db, token=token)
        except BaseException as error:
            await _rollback_preserving(db, error)
            raise
        await commit_authorization_session(db)
        return result

    async def authorize(
        self,
        db,
        *,
        identity,
        permission,
        reason,
        ticket_id,
        correlation_id,
        target_tenant_id,
        path,
        method,
        operation,
        preparation=None,
        event_type="authorized",
        denial_code=None,
        status_code=None,
    ):
        require_candidate_session(db)
        if (
            event_type not in {"authorized", "denied"}
            or (
                event_type == "authorized"
                and (denial_code is not None or status_code is not None)
            )
            or (
                event_type == "denied"
                and (
                    preparation is not None
                    or status_code not in {400, 403}
                    or denial_code
                    not in {
                        "PLATFORM_AUDIT_CONTEXT_SENSITIVE",
                        "PLATFORM_AUDIT_CONTEXT_REQUIRED",
                        "PLATFORM_PERMISSION_DENIED",
                    }
                )
            )
        ):
            raise _order_error()
        async with asyncio.timeout(3):
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db, budget=TransactionBudget(timeout_ms=3000)
                    ) as tx:
                        if permission == PLATFORM_PRINCIPAL_PERMISSION_REPLACE:
                            if preparation is None or preparation.identity != identity:
                                raise _order_error()
                            row = await check_password_preparation(db, preparation)
                            name = row["principal_name"]
                            keys = (
                                ResourceKey(
                                    ResourceKind.PLATFORM_PRINCIPAL,
                                    None,
                                    (identity.principal_id,),
                                ),
                            )
                        else:
                            snapshot = await read_principal(
                                db,
                                identity,
                                permission if event_type == "authorized" else None,
                            )
                            name, keys = snapshot.principal_name, snapshot.keys
                        audit = issue_principal_audit(
                            identity=identity,
                            principal_name=name,
                            operation=operation,
                            permission=permission,
                            reason=reason
                            if event_type == "authorized"
                            else reason or "[not supplied]",
                            ticket_id=ticket_id
                            if event_type == "authorized"
                            else ticket_id or "[not supplied]",
                            correlation_id=correlation_id
                            if event_type == "authorized"
                            else correlation_id or "[not supplied]",
                            target_tenant_id=target_tenant_id,
                            path=path,
                            method=method,
                        )
                        intent_key = ResourceKey(
                            ResourceKind.PLATFORM_AUDIT,
                            None,
                            (audit.authorization_audit_id,),
                        )
                        await tx.lock(
                            LockPlan(existing=keys, new=(NewResource(intent_key),))
                        )
                        if preparation is not None:
                            await check_password_preparation(db, preparation)
                        else:
                            await assert_principal(db, snapshot)
                        values = audit.values()
                        if event_type == "denied":
                            values.update(
                                event_type="denied",
                                status_code=status_code,
                                denial_code=denial_code,
                                reason=reason,
                                ticket_id=ticket_id,
                                correlation_id=correlation_id,
                            )
                        await tx.append_audit(intent_key, values)
            except BaseException as error:
                await _rollback_preserving(db, error)
                raise
            await commit_authorization_session(db)
        return audit

    async def complete_failure(self, factory, audit, error):
        async with asyncio.timeout(3):
            async with factory() as db:
                try:
                    with bounded_authorization_reads(db):
                        async with authorization_transactions.scope(
                            db, budget=TransactionBudget(timeout_ms=3000)
                        ) as tx:
                            await tx.lock(
                                LockPlan(existing=audit.existing, new=audit.new)
                            )
                            status = getattr(error, "code", 500)
                            if type(status) is not int or not 400 <= status <= 599:
                                status = 500
                            await audit.append(
                                tx, {"statusCode": status}, status_code=status
                            )
                except BaseException as failure:
                    await _rollback_preserving(db, failure)
                    raise
                await commit_authorization_session(db)

    async def replace_permissions(
        self,
        db,
        *,
        principal_name,
        current_password,
        permissions,
        fence,
        reason,
        ticket_id,
        correlation_id,
        audit_factory=None,
    ):
        require_candidate_session(db)
        require_maintenance(fence, "principal.permissions")
        desired = _permissions(permissions)
        try:
            preparation = await prepare_password(db, principal_name=principal_name)
            await rollback_authorization_session(db)
            await verify_preparation(preparation, current_password)
        except BaseException as error:
            await _rollback_preserving(db, error)
            raise
        require_maintenance(fence, "principal.permissions")
        factory = _factory(db, audit_factory)
        async with factory() as audit_db:
            if audit_db is db:
                raise _order_error()
            audit = await self.authorize(
                audit_db,
                identity=preparation.identity,
                permission=PLATFORM_PRINCIPAL_PERMISSION_REPLACE,
                reason=reason,
                ticket_id=ticket_id,
                correlation_id=correlation_id,
                target_tenant_id=None,
                path="tools/ops/platform_principal.py",
                method="CLI",
                operation="principal.permissions",
                preparation=preparation,
            )
        consume_principal_audit(audit)
        try:
            require_maintenance(fence, "principal.permissions")
            with bounded_authorization_reads(db):
                async with authorization_transactions.scope_global(
                    db, budget=TransactionBudget(timeout_ms=60000)
                ) as tx:
                    await tx.lock(LockPlan(existing=audit.existing, new=audit.new))
                    await check_password_preparation(db, preparation)
                    key = audit.existing[0]
                    async with tx.aggregate_global():
                        changed = await tx.update_held(
                            key,
                            {"permissions": list(desired)},
                            expected={
                                "row_version": preparation.identity.principal_version
                            },
                        )
                    current = await db.scalar(
                        select(PlatformPrincipal.row_version).where(
                            PlatformPrincipal.principal_id
                            == preparation.identity.principal_id
                        )
                    )
                    if (
                        changed
                        and current != preparation.identity.principal_version + 1
                    ):
                        raise BusinessException(
                            503,
                            "平台安全版本未更新",
                            error_code="PLATFORM_PRINCIPAL_VERSION_NOT_BUMPED",
                        )
                    await audit.append(tx, {"affectedCount": int(changed)})
        except BaseException as error:
            await _rollback_preserving(db, error)
            try:
                await self.complete_failure(factory, audit, error)
            except BaseException:
                if isinstance(error, asyncio.CancelledError):
                    raise error
                raise BusinessException(
                    503, "平台完成审计暂不可用", error_code="PLATFORM_AUDIT_UNAVAILABLE"
                ) from error
            raise
        await commit_authorization_session(db)
        return changed

    async def create_first(
        self,
        db,
        *,
        principal_name,
        display_name,
        password,
        permissions,
        fence,
        reason,
        ticket_id,
        correlation_id,
    ):
        require_candidate_session(db)
        require_maintenance(fence, "principal.create")
        desired = _permissions(permissions)
        if (
            type(principal_name) is not str
            or PLATFORM_PRINCIPAL_NAME_RE.fullmatch(principal_name) is None
            or type(display_name) is not str
            or not display_name.strip()
            or len(display_name) > 100
            or not display_name.isprintable()
        ):
            raise _order_error()
        if (
            type(password) is not str
            or len(password) < 12
            or len(password.encode()) > 72
            or not any(character.isalpha() for character in password)
            or not any(character.isdigit() for character in password)
        ):
            raise BusinessRuleException(
                "平台密码格式无效", error_code="PLATFORM_PASSWORD_INVALID"
            )
        principal_id = next_id()
        password_hash = await hash_password(password)
        audit = issue_principal_audit(
            identity=PrincipalIdentity(principal_id, 1),
            principal_name=principal_name,
            operation="principal.create",
            permission=PLATFORM_PRINCIPAL_PERMISSION_REPLACE,
            reason=reason,
            ticket_id=ticket_id,
            correlation_id=correlation_id,
            target_tenant_id=None,
            path="tools/ops/platform_principal.py",
            method="CLI",
        )
        principal_key = ResourceKey(
            ResourceKind.PLATFORM_PRINCIPAL, None, (principal_id,)
        )
        intent_key = ResourceKey(
            ResourceKind.PLATFORM_AUDIT, None, (audit.authorization_audit_id,)
        )
        try:
            require_maintenance(fence, "principal.create")
            with bounded_authorization_reads(db):
                async with authorization_transactions.scope_global(
                    db, budget=TransactionBudget(timeout_ms=60000)
                ) as tx:
                    if (
                        await db.scalar(select(PlatformPrincipal.principal_id).limit(1))
                        is not None
                    ):
                        raise BusinessRuleException(
                            "平台主体已经存在",
                            error_code="PLATFORM_PRINCIPAL_ALREADY_EXISTS",
                        )
                    await tx.lock(
                        LockPlan(
                            new=(NewResource(principal_key), NewResource(intent_key))
                            + audit.new
                        )
                    )
                    async with tx.aggregate_global():
                        await tx.insert_reserved(
                            principal_key,
                            {
                                "principal_id": principal_id,
                                "principal_name": principal_name,
                                "display_name": display_name.strip(),
                                "hashed_password": password_hash,
                                "permissions": list(desired),
                                "status": "1",
                                "row_version": 1,
                            },
                        )
                    await tx.append_audit(intent_key, audit.values())
                    await audit.append(tx, {"affectedCount": 1})
        except BaseException as error:
            await _rollback_preserving(db, error)
            raise
        await commit_authorization_session(db)
        return {"principal_id": str(principal_id), "principal_name": principal_name}


principal_candidate_owner = PrincipalCandidateOwner()
