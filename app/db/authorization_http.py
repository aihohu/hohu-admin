"""Fixed HTTP adapters: release request reads before starting a fresh owner."""

import asyncio
import logging
from uuid import uuid4

from anyio import CancelScope

from app.core.authorization_io import verify_password
from app.core.exceptions import (
    AuthenticationException,
    AuthorizationException,
    BusinessRuleException,
)
from app.core.security import decode_access_token
from app.db.authorization_owner import _quarantine
from app.db.authorization_plan_session import _order_error
from app.db.request_session import RequestSession
from app.db.session import engine, is_session_quarantined
from app.modules.auth.service import _DUMMY_PASSWORD_HASH, access_identity, auth_service

logger = logging.getLogger(__name__)


def get_authorization_runtime():
    # Disabled AI routes must be available without importing AI execution/SDKs.
    from app.db.authorization_runtime import AuthorizationRuntime  # noqa: PLC0415

    return AuthorizationRuntime(engine)


async def release_request_reads(db):
    if (
        not isinstance(db, RequestSession)
        or db._request_released
        or db._request_wrote
        or is_session_quarantined(db)
        or db.new
        or db.dirty
        or db.deleted
    ):
        raise _order_error()
    # Loaded response/auth objects retain their values without lazy SQL or an
    # expired ORM refresh after the independent owner's completion.
    db.expunge_all()
    db._request_released = True
    await _rollback_request_reads(db)


async def finish_request_read_phase(db):
    """End a read phase before an independent writer, keeping no old SQL locks."""
    if (
        not isinstance(db, RequestSession)
        or db._request_released
        or db._request_wrote
        or is_session_quarantined(db)
        or db.new
        or db.dirty
        or db.deleted
    ):
        raise _order_error()
    db.expunge_all()
    await _rollback_request_reads(db)


async def _rollback_request_reads(db):
    """Share the bounded ACK/quarantine boundary for both read handoffs."""
    pending = asyncio.create_task(db.rollback())
    try:
        with CancelScope(shield=True):
            done, _ = await asyncio.wait({pending}, timeout=0.25)
            if pending not in done:
                raise TimeoutError()
            pending.result()
    except BaseException as exc:
        db._request_released = True
        if not pending.done():
            pending.cancel()
        await _quarantine(db, pending)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise BusinessRuleException(
            "Request read rollback acknowledgement unavailable",
            error_code="AUTHORIZATION_ROLLBACK_UNKNOWN",
        ) from None


def release_completed_request(db):
    """Seal a completed stream prefix without accepting any active old writer."""
    if (
        not isinstance(db, RequestSession)
        or db._request_released
        or is_session_quarantined(db)
        or db.in_transaction()
        or db.new
        or db.dirty
        or db.deleted
    ):
        raise _order_error()
    db.expunge_all()
    # Keep the historical write marker. No session may resume SQL after sealing.
    db._request_released = True


async def system_write(
    db, actor, *, operation, payload=None, target_id=None, tenant=None
):
    identity = access_identity(actor)
    if tenant is not None and identity.tenant != tenant:
        raise _order_error()
    await release_request_reads(db)
    return await get_authorization_runtime().system(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        operation=operation,
        payload=payload,
        target_id=target_id,
    )


async def user_read(db, actor, *, operation, query=None, tenant=None):
    identity = access_identity(actor)
    if tenant is not None and identity.tenant != tenant:
        raise _order_error()
    await release_request_reads(db)
    return await get_authorization_runtime().user_read(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        operation=operation,
        query=query,
    )


async def business_write(db, actor, *, operation, tenant, payload=None, ids=()):
    identity = access_identity(actor)
    if identity.tenant != tenant:
        raise _order_error()
    await release_request_reads(db)
    return await get_authorization_runtime().business.execute(
        tenant=tenant,
        actor_user_version=identity.user_version,
        operation=operation,
        payload=payload,
        ids=ids,
    )


async def _login_audit(*, tenant_id, user_id, credentials, ip, user_agent, success):
    try:
        async with asyncio.timeout(2):
            await auth_service._write_login_log(
                tenant_id=tenant_id,
                audit_scope="unresolved" if tenant_id is None else "tenant",
                user_id=user_id,
                username=credentials.user_name or "",
                ip=ip,
                user_agent=user_agent,
                status="1" if success else "2",
                message="登录成功" if success else "凭据无效",
            )
    except TimeoutError:
        logger.warning("Login audit acknowledgement timed out")


async def login_http(db, credentials, *, host=None, ip=None, user_agent=None):
    try:
        async with asyncio.timeout(5):
            tenant = await auth_service.resolve_login_tenant(credentials, db, host=host)
    except AuthenticationException:
        await release_request_reads(db)
        await _login_audit(
            tenant_id=None,
            user_id=None,
            credentials=credentials,
            ip=ip,
            user_agent=user_agent,
            success=False,
        )
        if credentials.login_type == "password" and credentials.password:
            await verify_password(credentials.password, _DUMMY_PASSWORD_HASH)
        raise
    tenant_id = tenant.tenant_id
    await release_request_reads(db)
    if credentials.login_type != "password":
        raise BusinessRuleException(
            "不支持的登录方式", error_code="UNSUPPORTED_LOGIN_TYPE"
        )
    try:
        access, refresh = await get_authorization_runtime().login(
            credentials, host=host
        )
    except (AuthenticationException, AuthorizationException):
        await _login_audit(
            tenant_id=tenant_id,
            user_id=None,
            credentials=credentials,
            ip=ip,
            user_agent=user_agent,
            success=False,
        )
        raise
    # These claims were just signed by the fixed owner after its confirmed ACK.
    user_id = int(decode_access_token(access)["sub"])
    await _login_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        credentials=credentials,
        ip=ip,
        user_agent=user_agent,
        success=True,
    )
    return {"token": access, "refreshToken": refresh}


async def control_write(
    db, actor, *, operation, payload=None, target_id=None, tenant=None, request=None
):
    identity = access_identity(actor)
    if tenant is not None and identity.tenant != tenant:
        raise _order_error()
    audited = operation in {"mail.save", "mail.test"} or (
        operation == "settings.save" and payload["group"] == "security"
    )
    audit = {}
    if audited:
        if request is None:
            raise _order_error()
        audit = {
            "audit_context": {
                "reason": "Authenticated settings administration",
                "ticket_id": "system-settings-http",
                "correlation_id": uuid4().hex,
            },
            "audit_path": request.scope["route"].path,
            "audit_method": request.method,
        }
    await release_request_reads(db)
    return await get_authorization_runtime().control(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        operation=operation,
        payload=payload,
        target_id=target_id,
        **audit,
    )
