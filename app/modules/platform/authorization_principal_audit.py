"""Principal-specific audit lineage; no System user ID can enter its FK."""

import time
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from app.core.authorization_plan import NewResource, ResourceKey, ResourceKind
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.platform.authorization_principal_candidate import PrincipalIdentity

_ISSUED = object()
_CLAIMS = WeakKeyDictionary()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class PrincipalAudit:
    identity: PrincipalIdentity
    principal_name: str
    operation: str
    permission: str
    reason: str
    ticket_id: str
    correlation_id: str
    target_tenant_id: int | None
    path: str
    method: str
    authorization_audit_id: int
    completion_id: int
    _started: float = field(repr=False)
    _issued: object = field(repr=False)

    def __post_init__(self):
        if self._issued is not _ISSUED or type(self.identity) is not PrincipalIdentity:
            raise _order_error()
        _CLAIMS[self] = False

    @property
    def existing(self):
        return (
            ResourceKey(
                ResourceKind.PLATFORM_PRINCIPAL, None, (self.identity.principal_id,)
            ),
            ResourceKey(
                ResourceKind.PLATFORM_AUDIT, None, (self.authorization_audit_id,)
            ),
        )

    @property
    def new(self):
        return (
            NewResource(
                ResourceKey(ResourceKind.PLATFORM_AUDIT, None, (self.completion_id,))
            ),
        )

    def assert_request(self, *, identity, permission, target_tenant_id, operation):
        if (
            type(identity) is not PrincipalIdentity
            or identity != self.identity
            or permission != self.permission
            or target_tenant_id != self.target_tenant_id
            or type(target_tenant_id) is not type(self.target_tenant_id)
            or operation != self.operation
        ):
            raise _order_error()

    def values(self, *, completed=False, status_code=200, result_summary=None):
        return {
            "audit_id": self.completion_id
            if completed
            else self.authorization_audit_id,
            "authorization_audit_id": self.authorization_audit_id
            if completed
            else None,
            "actor_principal_id": self.identity.principal_id,
            "actor_name": self.principal_name,
            "permission": self.permission,
            "event_type": "completed" if completed else "authorized",
            "method": self.method,
            "path": self.path,
            "target_tenant_id": self.target_tenant_id,
            "reason": self.reason,
            "ticket_id": self.ticket_id,
            "correlation_id": self.correlation_id,
            "request_summary": {"operation": self.operation},
            "result_summary": (
                {**(result_summary or {}), "statusCode": status_code}
                if completed
                else None
            ),
            "status_code": status_code if completed else None,
            "duration_ms": max(0, int((time.monotonic() - self._started) * 1000))
            if completed
            else None,
        }

    async def append(self, tx, result_summary=None, *, status_code=200):
        for key in self.existing:
            tx.assert_held(key)
        return await tx.append_audit(
            self.new[0].key,
            self.values(
                completed=True, status_code=status_code, result_summary=result_summary
            ),
        )


def consume_principal_audit(audit):
    if type(audit) is not PrincipalAudit or _CLAIMS.get(audit) is not False:
        raise _order_error()
    _CLAIMS[audit] = True


def principal_audit_consumed(audit):
    if type(audit) is not PrincipalAudit or audit not in _CLAIMS:
        raise _order_error()
    return _CLAIMS[audit]


def issue_principal_audit(
    *,
    identity,
    principal_name,
    operation,
    permission,
    reason,
    ticket_id,
    correlation_id,
    target_tenant_id,
    path,
    method,
):
    for value, limit in (
        (principal_name, 64),
        (operation, 64),
        (permission, 96),
        (reason, 256),
        (ticket_id, 128),
        (correlation_id, 128),
        (path, 256),
    ):
        if (
            type(value) is not str
            or not value.strip()
            or len(value) > limit
            or not value.isprintable()
        ):
            raise _order_error()
    if method not in {"GET", "POST", "PUT", "DELETE", "CLI"} or (
        target_tenant_id is not None
        and (type(target_tenant_id) is not int or not 0 <= target_tenant_id < 2**63)
    ):
        raise _order_error()
    return PrincipalAudit(
        identity,
        principal_name,
        operation,
        permission,
        reason,
        ticket_id,
        correlation_id,
        target_tenant_id,
        path,
        method,
        next_id(),
        next_id(),
        time.monotonic(),
        _ISSUED,
    )
