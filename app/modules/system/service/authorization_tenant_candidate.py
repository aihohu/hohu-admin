"""Planned tenant registry writers; no legacy lock or transaction fallback."""

from copy import deepcopy

from sqlalchemy import select

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import normalize_tenant_code
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_control_identity import (
    ControlAudit,
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.tenant_lifecycle_service import (
    _tenant_fingerprint,
    hash_tenant_idempotency_key,
)

TENANT_OPERATIONS = {
    "tenant.prepare": "platform:tenant:write",
    "tenant.disable": "platform:tenant:write",
    "tenant.activate": "platform:tenant:activate",
}


def _target_key(tenant_id):
    return ResourceKey(ResourceKind.TENANT, None, (tenant_id,))


def _barriers(source, target):
    return tuple(
        TenantLock(tid, LockMode.EXCLUSIVE if tid == target else LockMode.SHARED)
        for tid in sorted({source, target})
    )


async def _tenant_row(db, tenant_id):
    row = (
        (
            await db.execute(
                select(Tenant.__table__).where(Tenant.tenant_id == tenant_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")
    return dict(row)


def _prepare_payload(payload):
    if type(payload) is not dict or set(payload) != {
        "tenant_code",
        "tenant_name",
        "idempotency_key",
    }:
        raise _order_error()
    code = normalize_tenant_code(payload["tenant_code"])
    name = payload["tenant_name"].strip() if type(payload["tenant_name"]) is str else ""
    if code is None or len(code) < 2 or code == "default":
        raise BusinessRuleException(
            "租户代码无效", error_code="PLATFORM_TENANT_CODE_INVALID"
        )
    if not name or len(name) > 100 or not name.isprintable():
        raise BusinessRuleException(
            "租户名称无效", error_code="PLATFORM_TENANT_NAME_INVALID"
        )
    return {
        "tenant_code": code,
        "tenant_name": name,
        "provisioning_key_hash": hash_tenant_idempotency_key(
            payload["idempotency_key"]
        ),
        "provisioning_fingerprint": _tenant_fingerprint(
            tenant_code=code, tenant_name=name
        ),
    }


class TenantControlCandidate:
    async def execute(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        operation,
        target_tenant_id,
        payload=None,
        audit=None,
    ):
        require_candidate_session(db)
        if (
            operation not in TENANT_OPERATIONS
            or type(audit) is not ControlAudit
            or type(target_tenant_id) is not int
            or not 0 <= target_tenant_id < 2**63
        ):
            raise _order_error()
        permission = TENANT_OPERATIONS[operation]
        audit.assert_request(
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation=operation,
            permission=permission,
            target_tenant_id=target_tenant_id,
        )
        payload = deepcopy(payload)
        if target_tenant_id == 0:
            raise BusinessRuleException(
                "Default Tenant 不能通过平台 API 修改",
                error_code="PLATFORM_DEFAULT_TENANT_IMMUTABLE",
            )
        if operation == "tenant.prepare":
            values = _prepare_payload(payload)
            with bounded_authorization_reads(db):
                return await self._prepare(
                    db, tenant, actor_user_version, target_tenant_id, values, audit
                )
        if payload is not None:
            raise _order_error()
        if operation == "tenant.activate" and (
            settings.TENANT_MODE != "hosted" or not settings.TENANT_HOSTED_LOGIN_ENABLED
        ):
            raise BusinessRuleException(
                "Hosted Tenant 激活发布闸未开启",
                error_code="PLATFORM_TENANT_ACTIVATION_DISABLED",
            )
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db, tenants=_barriers(tenant.tenant_id, target_tenant_id)
            ) as tx:
                identity = await read_identity(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    permissions=(permission,),
                    system=True,
                )
                before = await _tenant_row(db, target_tenant_id)
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            set(
                                identity.keys
                                + audit.existing
                                + (_target_key(target_tenant_id),)
                            )
                        ),
                        new=audit.new,
                    )
                )
                await assert_identity(db, identity)
                current = await _tenant_row(db, target_tenant_id)
                if current != before:
                    raise BusinessRuleException(
                        "租户状态已变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
                    )
                if operation == "tenant.activate":
                    if current["bootstrap_version"] < 1:
                        raise BusinessRuleException(
                            "租户尚未完成引导",
                            error_code="PLATFORM_TENANT_NOT_BOOTSTRAPPED",
                        )
                    if (current["lifecycle_state"], current["status"]) not in {
                        ("active", "1"),
                        ("prepared", "2"),
                        ("disabled", "2"),
                    }:
                        raise BusinessRuleException(
                            "租户状态不能激活",
                            error_code="PLATFORM_TENANT_STATE_INVALID",
                        )
                    changes = {"status": "1", "lifecycle_state": "active"}
                else:
                    changes = {"status": "2", "lifecycle_state": "disabled"}
                async with tx.aggregate(target_tenant_id):
                    await tx.update_held(
                        _target_key(target_tenant_id),
                        changes,
                        expected={"row_version": current["row_version"]},
                    )
                await audit.append(tx, {"tenantId": str(target_tenant_id)})
                result = await _tenant_row(db, target_tenant_id)
        return self._result(result, replayed=False)

    async def _prepare(self, db, tenant, user_version, target, values, audit):
        async with authorization_transactions.scope_creation(db) as tx:
            identity = await read_identity(
                db,
                tenant=tenant,
                actor_user_version=user_version,
                permissions=(TENANT_OPERATIONS["tenant.prepare"],),
                system=True,
            )
            replay = (
                (
                    await db.execute(
                        select(Tenant.__table__).where(
                            Tenant.provisioning_key_hash
                            == values["provisioning_key_hash"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if replay is not None:
                if (
                    replay["provisioning_fingerprint"]
                    != values["provisioning_fingerprint"]
                ):
                    raise BusinessException(
                        409,
                        "租户开户幂等键已被不同请求使用",
                        error_code="PLATFORM_TENANT_IDEMPOTENCY_CONFLICT",
                    )
                if replay["tenant_id"] != target:
                    raise AuthorizationException(
                        "平台目标租户不匹配",
                        error_code="PLATFORM_TARGET_TENANT_MISMATCH",
                    )
                await tx.replay_tenants(_barriers(tenant.tenant_id, target))
                existing = identity.keys + audit.existing + (_target_key(target),)
            else:
                duplicate = await db.scalar(
                    select(Tenant.tenant_id)
                    .where(
                        (Tenant.tenant_code == values["tenant_code"])
                        | (Tenant.tenant_id == target)
                    )
                    .limit(1)
                )
                if duplicate is not None:
                    raise BusinessException(
                        409,
                        "租户代码或ID已存在",
                        error_code="PLATFORM_TENANT_CODE_EXISTS",
                    )
                await tx.create_tenant(
                    _target_key(target),
                    dict(
                        values,
                        tenant_id=target,
                        status="2",
                        lifecycle_state="prepared",
                        row_version=1,
                        bootstrap_version=0,
                    ),
                )
                # G-X excludes protocol writers; source User has no audit FK.
                existing = audit.existing + (_target_key(target),)
            await tx.lock(LockPlan(existing=tuple(set(existing)), new=audit.new))
            await assert_identity(db, identity)
            await audit.append(
                tx, {"tenantId": str(target), "replayed": replay is not None}
            )
            result = await _tenant_row(db, target)
        return self._result(result, replayed=replay is not None)

    @staticmethod
    def _result(row, *, replayed):
        return {
            name: row[name]
            for name in (
                "tenant_id",
                "tenant_code",
                "tenant_name",
                "status",
                "lifecycle_state",
                "row_version",
                "bootstrap_version",
            )
        } | {"replayed": replayed}


tenant_control_candidate = TenantControlCandidate()
