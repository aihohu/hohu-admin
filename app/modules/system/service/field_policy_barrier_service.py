"""Internal G/B primitives; existing authorization writers are not yet wired here.

Acquire the complete lock set in the outer transaction before domain locks and
before SAVEPOINTs. These locks do not authenticate callers or enable policies.
"""

from asyncio import CancelledError
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import SessionTransaction

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.core.tenant_scope import require_tenant_context
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.user_fields import CATALOG_REVISION
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)

LockMode = Literal["shared", "exclusive"]
_TRACKER_KEY = "user_field_policy_barriers"


def unavailable() -> BusinessException:
    return BusinessException(
        503, "用户字段策略状态不可用", error_code="FIELD_POLICY_UNAVAILABLE"
    )


def lock_order_error() -> BusinessException:
    return BusinessException(
        409, "用户字段策略锁顺序无效", error_code="FIELD_POLICY_LOCK_ORDER"
    )


@dataclass
class _HeldLocks:
    transaction: SessionTransaction
    global_mode: LockMode | None = None
    tenants: dict[int, LockMode] = field(default_factory=dict)


class FieldPolicyBarrierService:
    async def acquire(
        self,
        db: AsyncSession,
        *,
        tenants: tuple[TenantContext, ...],
        mode: LockMode = "shared",
        global_mode: LockMode = "shared",
        timeout_ms: int = 2000,
    ) -> None:
        if (
            mode not in {"shared", "exclusive"}
            or global_mode not in {"shared", "exclusive"}
            or type(timeout_ms) is not int
            or not 1 <= timeout_ms <= 2000
            or not isinstance(tenants, tuple)
            or len(tenants) > 32
            or db.in_nested_transaction()
        ):
            raise lock_order_error()
        ids = tuple(require_tenant_context(tenant).tenant_id for tenant in tenants)
        if tuple(sorted(set(ids))) != ids:
            raise lock_order_error()
        await db.connection()  # Autobegin without flushing pending domain writes.
        transaction = db.sync_session.get_transaction()
        assert transaction is not None
        held = db.info.get(_TRACKER_KEY)
        if not isinstance(held, _HeldLocks) or held.transaction is not transaction:
            held = _HeldLocks(transaction)
            db.info[_TRACKER_KEY] = held
        if held.global_mode == "shared" and global_mode == "exclusive":
            raise lock_order_error()
        for tenant_id in ids:
            previous = held.tenants.get(tenant_id)
            if previous == "shared" and mode == "exclusive":
                raise lock_order_error()
            # The complete B set must precede every role/user/resource lock.
            # Even adding a larger tenant later could follow a domain lock.
            if previous is None and held.tenants:
                raise lock_order_error()
        if held.global_mode is None and (db.new or db.dirty or db.deleted):
            raise lock_order_error()
        prior_timeout = await db.scalar(text("SHOW lock_timeout"))
        await db.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{timeout_ms}ms"},
        )
        database_failed = False
        try:
            with db.no_autoflush:
                if held.global_mode is None:
                    guard = await db.scalar(
                        select(UserFieldPolicyGlobalGuard)
                        .where(UserFieldPolicyGlobalGuard.guard_id == 1)
                        .with_for_update(read=global_mode == "shared")
                        .execution_options(populate_existing=True)
                    )
                    if guard is None or guard.epoch < 1:
                        raise unavailable()
                    held.global_mode = global_mode
                for tenant_id in ids:
                    statement = select(UserFieldPolicyState).where(
                        UserFieldPolicyState.tenant_id == tenant_id
                    )
                    if tenant_id not in held.tenants:
                        statement = statement.with_for_update(read=mode == "shared")
                    state = await db.scalar(
                        statement.execution_options(populate_existing=True)
                    )
                    if (
                        state is None
                        or state.object_type != "user"
                        or state.mode not in {"legacy", "ready"}
                        or state.ever_enforced
                        or state.active_manifest_digest is not None
                        or state.catalog_revision != CATALOG_REVISION
                        or state.catalog_digest != catalog_digest()
                    ):
                        raise unavailable()
                    held.tenants[tenant_id] = held.tenants.get(tenant_id, mode)
        except CancelledError:
            # Cancellation can invalidate the connection. Preserve it and let
            # the transaction owner roll back instead of issuing cleanup SQL.
            database_failed = True
            raise
        except DBAPIError as exc:
            database_failed = True
            code = getattr(exc.orig, "sqlstate", None)
            if code in {"55P03", "57014"}:
                raise unavailable() from None
            raise
        finally:
            if not database_failed:
                await db.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": prior_timeout},
                )


field_policy_barrier_service = FieldPolicyBarrierService()
