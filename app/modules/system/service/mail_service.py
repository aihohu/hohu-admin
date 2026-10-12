"""SMTP transport and versioned global configuration, without transaction commits."""

import asyncio
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.core.security import decrypt_value, encrypt_value
from app.modules.system.models.mail_config import MailConfig


class MailSettings(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    enabled: bool = False
    host: str = Field(default="", max_length=253)
    port: int = Field(default=465, ge=1, le=65535)
    security: Literal["TLS", "STARTTLS"] = "TLS"
    username: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=2048)
    from_email: EmailStr | None = None
    from_name: str = Field(default="HoHu", max_length=100)

    @field_validator("host", "username", "from_name")
    @classmethod
    def no_header_injection(cls, value):
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Control characters are not allowed")
        return value.strip()

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        if any(c in value for c in "/\\:@ "):
            raise ValueError("Use a SMTP hostname without a URL or port")
        return value


class MailWrite(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    values: MailSettings
    revision: str
    clear_password: bool = False


def ready(config: MailSettings) -> bool:
    return bool(
        config.enabled
        and config.host
        and config.from_email
        and (not config.username or config.password)
    )


def _send(config: MailSettings, recipient: str, subject: str, body: str):
    message = EmailMessage()
    message["From"] = formataddr((config.from_name, str(config.from_email)))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if config.security == "TLS":
        connection = smtplib.SMTP_SSL(
            config.host, config.port, timeout=10, context=context
        )
    else:
        connection = smtplib.SMTP(config.host, config.port, timeout=10)
    with connection as smtp:
        if config.security == "STARTTLS":
            smtp.starttls(context=context)
        if config.username:
            smtp.login(config.username, config.password)
        if smtp.send_message(message):
            raise smtplib.SMTPException("Recipient rejected")


class MailService:
    async def row(self, db, *, lock=False):
        stmt = select(MailConfig).where(MailConfig.config_id == 1)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await db.scalar(stmt)

    async def config(self, db):
        row = await self.row(db)
        if row is None:
            return MailSettings()
        values = dict(row.values)
        values["password"] = (
            decrypt_value(row.password_ciphertext) if row.password_ciphertext else ""
        )
        return MailSettings(**values)

    async def describe(self, db):
        row = await self.row(db)
        config = await self.config(db)
        return {
            "values": {**config.model_dump(by_alias=True), "password": ""},
            "revision": str(row.version) if row else "0",
            "configuredPassword": bool(row and row.password_ciphertext),
            "ready": ready(config),
            "lastTestAt": row.last_test_at.isoformat()
            if row and row.last_test_at
            else None,
            "lastTestStatus": row.last_test_status if row else "untested",
        }

    async def save(self, db, payload: MailWrite):
        row = await self.row(db, lock=True)
        if row is None or payload.revision != str(row.version):
            error = BusinessRuleException(
                "Reload mail settings before saving", error_code="SETTINGS_CONFLICT"
            )
            error.code = 409
            raise error
        config = payload.values.model_copy()
        if payload.clear_password and config.password:
            raise BusinessRuleException(
                "Cannot clear and replace the password together",
                error_code="SETTING_VALUE_INVALID",
            )
        if (
            not payload.clear_password
            and not config.password
            and row.password_ciphertext
        ):
            config.password = decrypt_value(row.password_ciphertext)
        if config.enabled and not ready(config):
            raise BusinessRuleException(
                "Complete the SMTP configuration first",
                error_code="MAIL_NOT_CONFIGURED",
            )
        row.values = config.model_dump(exclude={"password"})
        row.password_ciphertext = (
            encrypt_value(config.password) if config.password else ""
        )
        row.version += 1
        row.last_test_at = None
        row.last_test_status = "untested"
        await db.flush()
        return await self.describe(db)

    async def send(self, config, recipient, subject, body, *, testing=False):
        if not ready(
            config.model_copy(update={"enabled": True}) if testing else config
        ):
            raise BusinessRuleException(
                "Mail is not configured", error_code="MAIL_NOT_CONFIGURED"
            )
        try:
            await asyncio.to_thread(_send, config, recipient, subject, body)
        except (smtplib.SMTPException, OSError, ValueError):
            raise BusinessRuleException(
                "Unable to send email; try again later", error_code="MAIL_SEND_FAILED"
            ) from None

    async def test(self, db, recipient):
        row = await self.row(db, lock=True)
        if row is None:
            raise BusinessRuleException(
                "Save mail settings first", error_code="MAIL_NOT_CONFIGURED"
            )
        config = await self.config(db)
        accepted = True
        try:
            await self.send(
                config,
                recipient,
                "HoHu SMTP test / 邮件测试",
                "HoHu test email. SMTP acceptance does not guarantee inbox delivery.\nHoHu 测试邮件：SMTP 接受不代表已进入收件箱。",
                testing=True,
            )
        except BusinessRuleException:
            accepted = False
        row.last_test_at = datetime.now(UTC)
        row.last_test_status = "accepted" if accepted else "failed"
        return await self.describe(db)


mail_service = MailService()
