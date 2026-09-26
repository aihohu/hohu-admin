"""Typed tenant settings with atomic group writes and explicit secret handling."""

import hashlib
import hmac
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext, TenantLocatorContext
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from app.modules.system.settings_catalog import (
    ALLOWED_UPLOAD_EXTENSIONS,
    SETTING_GROUPS,
    SETTINGS,
    Setting,
)
from app.utils.validators import validate_password


def _decode(definition: Setting, raw: object) -> object:
    if definition.kind == "boolean":
        return str(raw).lower() in {"true", "1", "yes", "on"}
    if definition.kind == "integer":
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return definition.default
    return raw


def _validate(definition: Setting, value: object) -> str:
    invalid = BusinessRuleException(
        f"Invalid setting: {definition.key}", error_code="SETTING_VALUE_INVALID"
    )
    if definition.kind == "boolean":
        if not isinstance(value, bool):
            raise invalid
        return "true" if value else "false"
    if definition.kind == "integer":
        if type(value) is not int:
            raise invalid
        if definition.minimum is not None and value < definition.minimum:
            raise invalid
        if definition.maximum is not None and value > definition.maximum:
            raise invalid
        return str(value)
    if not isinstance(value, str) or len(value) > 100_000:
        raise invalid
    if definition.key in {"site_logo", "default_avatar"} and value:
        if not (
            value.startswith(("https://", "http://", "/uploads/"))
            and not any(char in value for char in "\r\n\\")
        ):
            raise invalid
    if definition.group not in {"agreements"} and len(value) > 2048:
        raise invalid
    if (
        definition.kind == "select"
        and definition.options
        and value not in definition.options
    ):
        raise invalid
    if definition.kind == "extensions":
        extensions = {part.strip().lower() for part in value.split(",")}
        if not extensions or not extensions <= ALLOWED_UPLOAD_EXTENSIONS:
            raise invalid
        return ",".join(sorted(extensions))
    if definition.kind == "secret" and value:
        try:
            validate_password(value)
        except ValueError:
            raise invalid from None
        if settings.ENV == "prod" and value == "Hohu123456":
            raise invalid
    return value


class SettingsService:
    async def get_value(
        self,
        db: AsyncSession,
        key: str,
        default: str | None = None,
        *,
        tenant: TenantContext | TenantLocatorContext,
    ) -> str | None:
        result = await db.execute(
            select(SystemSetting.setting_value).where(
                SystemSetting.tenant_id == tenant.tenant_id,
                SystemSetting.setting_key == key,
                SystemSetting.status == "1",
            )
        )
        value = result.scalar_one_or_none()
        return value if value is not None else default

    async def get_bool_for_update(
        self,
        db: AsyncSession,
        key: str,
        default: bool = False,
        *,
        tenant: TenantContext,
    ) -> bool:
        row = await db.scalar(
            select(SystemSetting)
            .where(
                SystemSetting.tenant_id == tenant.tenant_id,
                SystemSetting.setting_key == key,
            )
            .with_for_update()
        )
        if row is None or row.status != "1":
            return default
        return row.setting_value.strip().lower() in {"true", "1", "yes", "on"}

    async def get_public_values(
        self, db: AsyncSession, *, tenant: TenantLocatorContext
    ) -> dict[str, object]:
        values = await self.values(db, tenant_id=tenant.tenant_id)
        return {key: value for key, value in values.items() if SETTINGS[key].public}

    async def values(self, db: AsyncSession, *, tenant_id: int) -> dict[str, object]:
        rows = (
            await db.scalars(
                select(SystemSetting).where(
                    SystemSetting.tenant_id == tenant_id,
                    SystemSetting.setting_key.in_(SETTINGS),
                    SystemSetting.status == "1",
                )
            )
        ).all()
        stored = {row.setting_key: row.setting_value for row in rows}
        return {
            key: _decode(definition, stored.get(key, definition.default))
            for key, definition in SETTINGS.items()
        }

    async def get_group(
        self,
        db: AsyncSession,
        group: str,
        *,
        tenant: TenantContext | TenantLocatorContext,
    ) -> dict:
        if group not in SETTING_GROUPS:
            raise BusinessRuleException(
                "Unknown settings group", error_code="SETTING_GROUP_INVALID"
            )
        values = await self.values(db, tenant_id=tenant.tenant_id)
        definitions = [d for d in SETTINGS.values() if d.group == group]
        group_values = {d.key: values[d.key] for d in definitions}
        revision = hmac.new(
            settings.SECRET_KEY.encode(),
            json.dumps(
                [tenant.tenant_id, group, group_values], sort_keys=True
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        secrets = [d.key for d in definitions if d.kind == "secret"]
        configured_secrets = [key for key in secrets if group_values[key]]
        for key in secrets:
            group_values[key] = ""
        return {
            "group": group,
            "values": group_values,
            "revision": revision,
            "secret_keys": secrets,
            "configured_secrets": configured_secrets,
            "fields": [
                {
                    "key": d.key,
                    "kind": d.kind,
                    "minimum": d.minimum,
                    "maximum": d.maximum,
                    "options": list(d.options),
                }
                for d in definitions
            ],
        }

    async def save_group(
        self,
        db: AsyncSession,
        group: str,
        values: dict[str, object],
        *,
        revision: str,
        tenant: TenantContext,
    ) -> dict:
        definitions = {key: d for key, d in SETTINGS.items() if d.group == group}
        if not values or not values.keys() <= definitions.keys():
            raise BusinessRuleException(
                "Unknown settings fields", error_code="SETTING_VALUE_INVALID"
            )
        normalized = {
            key: _validate(definitions[key], value) for key, value in values.items()
        }
        # The existing tenant row serializes updates, including insertion of missing keys.
        await db.execute(
            select(Tenant.tenant_id)
            .where(Tenant.tenant_id == tenant.tenant_id)
            .with_for_update()
        )
        current = await self.get_group(db, group, tenant=tenant)
        if not hmac.compare_digest(revision, current["revision"]):
            error = BusinessRuleException(
                "Settings changed; reload before saving", error_code="SETTINGS_CONFLICT"
            )
            error.code = 409
            raise error
        rows = (
            await db.scalars(
                select(SystemSetting).where(
                    SystemSetting.tenant_id == tenant.tenant_id,
                    SystemSetting.setting_key.in_(normalized),
                )
            )
        ).all()
        stored = {row.setting_key: row for row in rows}
        for key, value in normalized.items():
            definition = definitions[key]
            # Empty secret input means unchanged; secrets never round-trip from GET.
            if definition.kind == "secret" and not value:
                continue
            row = stored.get(key)
            if row is None:
                row = SystemSetting(
                    tenant_id=tenant.tenant_id,
                    setting_key=key,
                )
                db.add(row)
            row.setting_value = value
            row.status = "1"
        await db.flush()
        return await self.get_group(db, group, tenant=tenant)


settings_service = SettingsService()
