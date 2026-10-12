"""Public account capabilities, registration/recovery and authenticated email binding."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.db.session import get_db
from app.modules.auth.email_verification import email_verification
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.schemas.self_service import (
    EmailBinding,
    EmailCodeRequest,
    Registration,
)
from app.modules.auth.service import auth_service, get_current_user
from app.modules.system.service.account_service import account_service
from app.modules.system.service.mail_service import mail_service, ready
from app.modules.system.service.settings_service import settings_service
from app.utils.ip_util import get_client_ip

router = APIRouter()
logger = logging.getLogger(__name__)


async def resolve_tenant(request, db, code):
    host = request.headers.get("Host")
    if (
        settings.TENANT_MODE == "hosted"
        and not code
        and not auth_service._host_tenant_code(host)
    ):
        raise BusinessRuleException(
            "Select a tenant first", error_code="TENANT_REQUIRED"
        )
    return await auth_service.resolve_login_tenant(
        LoginCredentials(tenant_code=code), db, host=host
    )


async def policy(db, tenant_id, purpose):
    values = await settings_service.values(db, tenant_id=tenant_id)
    config = await mail_service.config(db)
    key = "register_enabled" if purpose == "register" else "password_reset_enabled"
    if not values[key]:
        raise BusinessRuleException(
            "This account service is disabled", error_code="ACCOUNT_SERVICE_DISABLED"
        )
    if not ready(config):
        raise BusinessRuleException(
            "Mail is unavailable", error_code="MAIL_NOT_CONFIGURED"
        )
    return values, config


async def notify(config, email, subject, body):
    try:
        await mail_service.send(config, email, subject, body)
    except BusinessRuleException:
        # Recovery intentionally has a uniform public response. Never log secrets.
        logger.warning("Account notification delivery failed")


def code_message(code, minutes):
    return (
        f"验证码 / Verification code: {code}\n"
        f"有效期 {minutes} 分钟。请勿向他人提供验证码。\n"
        f"Expires in {minutes} minutes. Do not share this code.\n"
        "如非本人操作，请忽略此邮件。 If you did not request this, ignore this email."
    )


@router.get("/capabilities")
async def capabilities(
    request: Request,
    tenant_code: str | None = Query(None, alias="tenantCode"),
    db=Depends(get_db),
):
    tenant = await resolve_tenant(request, db, tenant_code)
    values = await settings_service.values(db, tenant_id=tenant.tenant_id)
    available = ready(await mail_service.config(db))
    return ResponseModel.success(
        data={
            "registrationEnabled": available and bool(values["register_enabled"]),
            "passwordResetEnabled": available
            and bool(values["password_reset_enabled"]),
            "emailEnabled": available,
            "codeMinutes": values["email_code_minutes"],
        }
    )


@router.post("/email-code")
async def send_code(
    payload: EmailCodeRequest,
    request: Request,
    tasks: BackgroundTasks,
    db=Depends(get_db),
):
    tenant = await resolve_tenant(request, db, payload.tenant_code)
    values, config = await policy(db, tenant.tenant_id, payload.purpose)
    await email_verification.throttle(
        tenant.tenant_id, str(payload.email), get_client_ip(request)
    )
    minutes = int(values["email_code_minutes"])
    identity = f"{payload.user_name}:{payload.email}"
    eligible = True
    if payload.purpose == "reset":
        user = await account_service.find(db, tenant.tenant_id, payload.user_name)
        recovery = account_service.recovery_identity(user, str(payload.email))
        eligible = recovery is not None
        identity = recovery or "unknown:" + identity
    code = await email_verification.issue(
        tenant.tenant_id, payload.purpose, identity, minutes
    )
    if payload.purpose == "reset":
        if eligible:
            tasks.add_task(
                notify,
                config,
                str(payload.email),
                "HoHu 密码找回 / Password recovery",
                code_message(code, minutes),
            )
    else:
        await mail_service.send(
            config,
            str(payload.email),
            "HoHu 注册验证 / Registration",
            code_message(code, minutes),
        )
    return ResponseModel.success(data={"expiresIn": minutes * 60, "retryAfter": 60})


@router.post("/register")
async def register(payload: Registration, request: Request, db=Depends(get_db)):
    tenant = await resolve_tenant(request, db, payload.tenant_code)
    values, _ = await policy(db, tenant.tenant_id, "register")
    await account_service.registration_role(
        db, tenant.tenant_id, values["registration_role_id"]
    )
    await email_verification.consume(
        tenant.tenant_id,
        "register",
        f"{payload.user_name}:{payload.email}",
        payload.code,
    )
    user = await account_service.register(
        db, tenant.tenant_id, payload, values["registration_role_id"]
    )
    await db.commit()
    return ResponseModel.success(data={"userId": str(user.user_id)})


@router.post("/password-reset")
async def reset_password(
    payload: Registration, request: Request, tasks: BackgroundTasks, db=Depends(get_db)
):
    tenant = await resolve_tenant(request, db, payload.tenant_code)
    _, config = await policy(db, tenant.tenant_id, "reset")
    user = await account_service.find(
        db, tenant.tenant_id, payload.user_name, lock=True
    )
    identity = account_service.recovery_identity(user, str(payload.email))
    if identity is None:
        raise BusinessRuleException(
            "Invalid or expired email code", error_code="EMAIL_CODE_INVALID"
        )
    await email_verification.consume(tenant.tenant_id, "reset", identity, payload.code)
    await account_service.reset(db, user, payload.password)
    await db.commit()
    tasks.add_task(
        notify,
        config,
        str(payload.email),
        "HoHu 密码已重置 / Password changed",
        "您的密码已重置。如非本人操作，请联系管理员。\nYour password was reset. Contact your administrator if this was not you.",
    )
    return ResponseModel.success()


@router.get("/email")
async def email_status(user=Depends(get_current_user), db=Depends(get_db)):
    values = await settings_service.values(db, tenant_id=user.tenant_id)
    return ResponseModel.success(
        data={
            "email": user.user_email or "",
            "verified": bool(user.email_verified_at),
            "enabled": ready(await mail_service.config(db)),
            "codeMinutes": values["email_code_minutes"],
        }
    )


async def binding_context(db, user, payload):
    expected_version = user.auth_version
    locked = await account_service.find(db, user.tenant_id, user.user_name, lock=True)
    account_service.verify_current_password(locked, payload.password)
    if locked.auth_version != expected_version:
        raise BusinessRuleException(
            "Account security changed; sign in again", error_code="INVALID_CREDENTIALS"
        )
    config = await mail_service.config(db)
    if not ready(config):
        raise BusinessRuleException(
            "Mail is unavailable", error_code="MAIL_NOT_CONFIGURED"
        )
    identity = f"{locked.user_id}:{locked.auth_version}:{locked.user_email}:{locked.email_verified_at}:{payload.email}"
    return locked, config, identity


@router.post("/email/code")
async def binding_code(
    payload: EmailBinding,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    locked, config, identity = await binding_context(db, user, payload)
    await email_verification.throttle(
        locked.tenant_id, str(payload.email), get_client_ip(request)
    )
    values = await settings_service.values(db, tenant_id=locked.tenant_id)
    minutes = int(values["email_code_minutes"])
    code = await email_verification.issue(locked.tenant_id, "bind", identity, minutes)
    await mail_service.send(
        config,
        str(payload.email),
        "HoHu 邮箱验证 / Verify email",
        code_message(code, minutes),
    )
    return ResponseModel.success(data={"expiresIn": minutes * 60, "retryAfter": 60})


@router.put("/email")
async def bind_email(
    payload: EmailBinding,
    tasks: BackgroundTasks,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    locked, config, identity = await binding_context(db, user, payload)
    await email_verification.consume(
        locked.tenant_id, "bind", identity, payload.code or ""
    )
    old = await account_service.bind(db, locked, str(payload.email))
    await db.commit()
    if old and old != str(payload.email):
        tasks.add_task(
            notify,
            config,
            old,
            "HoHu 恢复邮箱已变更 / Recovery email changed",
            "您的恢复邮箱已变更。如非本人操作，请联系管理员。\nYour recovery email changed. Contact your administrator if this was not you.",
        )
    return ResponseModel.success()
