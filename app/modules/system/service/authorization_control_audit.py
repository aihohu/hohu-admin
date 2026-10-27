"""Frozen System-control intent lineage; completion is appended inside the plan."""

import json
import re
import time
from dataclasses import dataclass, field

from app.core.authorization_plan import NewResource, ResourceKey, ResourceKind
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.db.authorization_plan_session import _order_error

_ISSUED = object()


@dataclass(frozen=True, slots=True)
class ControlAudit:
    tenant: TenantContext
    actor_user_version: int
    actor_name: str
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
    _issued: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issued is not _ISSUED:
            raise _order_error()

    @property
    def existing(self):
        return (
            ResourceKey(ResourceKind.TENANT, None, (self.tenant.tenant_id,)),
            ResourceKey(
                ResourceKind.SYSTEM_OPERATION_LOG,
                self.tenant.tenant_id,
                (self.authorization_audit_id,),
            ),
        )

    @property
    def new(self):
        return (
            NewResource(
                ResourceKey(
                    ResourceKind.SYSTEM_OPERATION_LOG,
                    self.tenant.tenant_id,
                    (self.completion_id,),
                )
            ),
        )

    def assert_request(
        self, *, tenant, actor_user_version, permission, target_tenant_id, operation
    ):
        if (
            self._issued is not _ISSUED
            or type(tenant) is not TenantContext
            or tenant != self.tenant
            or type(actor_user_version) is not int
            or actor_user_version != self.actor_user_version
            or permission != self.permission
            or operation != self.operation
            or type(target_tenant_id) is not type(self.target_tenant_id)
            or target_tenant_id != self.target_tenant_id
        ):
            raise _order_error()

    def values(
        self, *, event_type, status_code=None, result_summary=None, changes=None
    ):
        if event_type not in {"authorized", "completed", "denied"}:
            raise _order_error()
        if changes is not None:
            allowed = {
                "name",
                "description",
                "enabled",
                "display_order",
                "system_prompt",
                "model_preference",
                "daily_quota_per_user",
                "risk_appetite",
            }
            if type(changes) is not dict or not set(changes) <= allowed:
                raise _order_error()
            for pair in changes.values():
                if type(pair) is not dict or set(pair) != {"before", "after"}:
                    raise _order_error()
                for value in pair.values():
                    if type(value) in {bool, int, type(None)}:
                        continue
                    if (
                        type(value) is not dict
                        or set(value) != {"sha256"}
                        or type(value["sha256"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
                    ):
                        raise _order_error()
        if result_summary is not None and (
            type(result_summary) is not dict
            or len(result_summary) > 16
            or any(
                type(key) is not str
                or len(key) > 64
                or type(value) not in {str, int, bool, type(None)}
                or (isinstance(value, str) and len(value) > 128)
                for key, value in result_summary.items()
            )
        ):
            raise _order_error()
        return {
            "operation_log_id": self.authorization_audit_id
            if event_type == "authorized"
            else self.completion_id,
            "tenant_id": self.tenant.tenant_id,
            "audit_scope": "platform",
            "user_id": self.tenant.actor_user_id,
            "username": self.actor_name,
            "module": "Platform control",
            "action": event_type,
            "method": self.method,
            "path": self.path,
            "status_code": status_code,
            "duration": None
            if event_type == "authorized"
            else max(0, int((time.monotonic() - self._started) * 1000)),
            "request_params": json.dumps(
                {
                    "operation": self.operation,
                    "permission": self.permission,
                    "reason": self.reason,
                    "ticket_id": self.ticket_id,
                    "correlation_id": self.correlation_id,
                    "target_tenant_id": self.target_tenant_id,
                    "authorization_audit_id": None
                    if event_type == "authorized"
                    else self.authorization_audit_id,
                    "result_summary": result_summary,
                    "changes": changes,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    async def append(self, tx, result_summary=None, *, changes=None):
        if self._issued is not _ISSUED:
            raise _order_error()
        for key in self.existing:
            tx.assert_held(key)
        return await tx.append_audit(
            self.new[0].key,
            self.values(
                event_type="completed",
                status_code=200,
                result_summary=result_summary,
                changes=changes,
            ),
        )


def issue_control_audit(
    *,
    tenant,
    actor_user_version,
    actor_name,
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
        (operation, 64),
        (permission, 96),
        (reason, 256),
        (ticket_id, 128),
        (correlation_id, 128),
        (path, 200),
    ):
        if (
            type(value) is not str
            or not value.strip()
            or len(value) > limit
            or not value.isprintable()
        ):
            raise _order_error()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or (
        target_tenant_id is not None
        and (type(target_tenant_id) is not int or not 0 <= target_tenant_id < 2**63)
    ):
        raise _order_error()
    return ControlAudit(
        tenant,
        actor_user_version,
        actor_name,
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
