"""Exact-key management settings and fixed mail projections under a live G/B."""

import hashlib

from pydantic import ValidationError
from sqlalchemy import String, and_, case, func, or_, select

from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.rbac import is_super_admin, is_system_admin
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.field_policy.control_catalog import READ, UPDATE
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.setting import SystemSetting
from app.modules.system.schemas.relation_policy import RelationFieldPolicy
from app.modules.system.service.authorization_read_budget import (
    read_phase,
)
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.mail_service import MailSettings
from app.modules.system.service.parameter_context import load_parameter_context
from app.modules.system.service.parameter_public_read import require_public_locator
from app.modules.system.service.parameter_receipt import (
    issue_receipt,
    observed_form_facts,
)
from app.modules.system.service.parameter_registration import registration_options
from app.modules.system.service.parameter_setting_facts import setting_snapshot
from app.modules.system.service.settings_service import _decode
from app.modules.system.service.user_field_context import load_current_user
from app.modules.system.settings_catalog import SETTING_GROUPS, SETTINGS

_ACCOUNT = frozenset(
    {
        "register_enabled",
        "password_reset_enabled",
        "registration_role_id",
        "user_require_primary_dept",
    }
)
_FILES = frozenset(
    {
        "upload:max_bytes",
        "upload:image_max_bytes",
        "upload:import_max_bytes",
        "upload:allowed_extensions",
    }
)


def setting_action(key):
    return (
        "settings.account"
        if key in _ACCOUNT
        else "settings.files"
        if key in _FILES
        else "settings.password"
        if key == "auth:default_password"
        else "settings.save"
    )


@read_phase("projection")
async def describe_mail(db):
    names = tuple(key for key in MailSettings.model_fields if key != "password")
    columns = []
    limits = {
        "host": 253,
        "username": 254,
        "from_email": 254,
        "from_name": 100,
        "security": 8,
    }
    for name in names:
        value = MailConfig.values[name]
        kind = func.pg_catalog.jsonb_typeof(value, type_=String())
        valid = and_(
            kind
            == (
                "boolean"
                if name == "enabled"
                else "number"
                if name == "port"
                else "string"
            ),
            func.pg_catalog.octet_length(value.astext) <= limits.get(name, 16),
        )
        if name == "from_email":
            valid = or_(valid, kind == "null")
        columns.append(case((valid, value), else_=None).label(name))
        columns.append(
            case((value.is_not(None) & ~valid, True), else_=False).label(
                "_invalid_" + name
            )
        )
    row = (
        (
            await db.execute(
                select(
                    *columns,
                    (
                        func.pg_catalog.jsonb_typeof(MailConfig.values, type_=String())
                        == "object"
                    ).label("_shape"),
                    MailConfig.version,
                    (MailConfig.password_ciphertext != "").label("configured"),
                    MailConfig.last_test_at,
                    MailConfig.last_test_status,
                ).where(MailConfig.config_id == 1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row and (not row["_shape"] or any(row["_invalid_" + name] for name in names)):
        raise BusinessRuleException(
            "Mail metadata unavailable", error_code="MAIL_CONFIGURATION_UNAVAILABLE"
        )
    safe = {
        name: row[name] for name in names if row is not None and row[name] is not None
    }
    try:
        config = MailSettings.model_validate(safe, strict=True)
    except (ValidationError, TypeError, ValueError):
        raise BusinessRuleException(
            "Mail metadata unavailable", error_code="MAIL_CONFIGURATION_UNAVAILABLE"
        ) from None
    configured = bool(row and row["configured"])
    ready = bool(
        config.enabled
        and config.host
        and config.from_email
        and (not config.username or configured)
    )
    return {
        "values": {**config.model_dump(by_alias=True), "password": ""},
        "revision": str(row["version"]) if row else "0",
        "configuredPassword": configured,
        "ready": ready,
        "lastTestAt": row["last_test_at"].isoformat()
        if row and row["last_test_at"]
        else None,
        "lastTestStatus": row["last_test_status"] if row else "untested",
    }


@read_phase("identity")
async def fixed_actor(db, tenant, version, permission, *, system=False):
    actor = await load_current_user(db, tenant=tenant, actor_user_version=version)
    if system and not is_system_admin(actor):
        raise AuthorizationException(
            "System administrator required", error_code="SYSTEM_ADMIN_REQUIRED"
        )
    if (
        permission is not None
        and permission not in collect_user_buttons(actor)
        and not is_super_admin(actor)
    ):
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    return actor


async def read_group(db, tx, tenant, version, group, *, context=None):
    if group not in SETTING_GROUPS:
        raise BusinessRuleException(
            "Unknown settings group", error_code="SETTING_GROUP_INVALID"
        )
    definitions = {key: d for key, d in SETTINGS.items() if d.group == group}
    if group == "security":
        await fixed_actor(db, tenant, version, "system:setting:list", system=True)
        reads = updates = set(definitions)
    else:
        context = context or await load_parameter_context(
            db, tx, tenant=tenant, actor_user_version=version, action="settings.group"
        )
        record = context.record("system.setting", "settings", context.scopes)
        reads = (
            set(context.allowed(record, "settings.group", READ)) & definitions.keys()
        )
        updates = {
            key
            for key in definitions
            if key
            in context.allowed(
                record,
                "settings.registration-options"
                if key == "registration_role_id"
                else setting_action(key),
                UPDATE,
            )
        }
    rows, revision = await setting_snapshot(db, tenant.tenant_id, group, readable=reads)
    stored = {row["setting_key"]: row for row in rows}
    values = {
        key: _decode(definitions[key], stored[key]["setting_value"])
        if key in stored and stored[key]["status"] == "1"
        else _decode(definitions[key], definitions[key].default)
        for key in reads
        if key not in stored
        or stored[key]["status"] != "1"
        or stored[key]["setting_value"] is not None
    }
    secrets = [key for key in updates if definitions[key].kind == "secret"]
    configured = [
        key
        for key in secrets
        if key in stored
        and stored[key]["status"] == "1"
        and stored[key]["value_digest"] != hashlib.sha256(b"").digest()
    ]
    values.update(dict.fromkeys(secrets, ""))
    policy = {
        "read": sorted(reads),
        "read_levels": {
            key: "plain" if key in reads else "hidden" for key in definitions
        },
        "query": [],
        "create": [],
        "update": sorted(
            key for key in updates if setting_action(key) == "settings.save"
        ),
        "special_create": [],
        "special_update": sorted(
            key for key in updates if setting_action(key) != "settings.save"
        ),
        "can_create": False,
        "complete": {},
    }
    result = {
        "group": group,
        "values": values,
        "revision": revision,
        "fields": [
            {
                "key": key,
                "kind": d.kind,
                "minimum": d.minimum,
                "maximum": d.maximum,
                "options": list(d.options),
            }
            for key, d in definitions.items()
            if key in reads | updates
        ],
        "secretKeys": secrets,
        "configuredSecrets": configured,
        "fieldPolicy": RelationFieldPolicy.model_validate(policy).model_dump(
            by_alias=True
        ),
    }
    if context is not None:
        facts = await observed_form_facts(db, tx, context, mode="group", group=group)
        result["formReceipt"] = issue_receipt(facts)
    if group == "account":
        result["mailReady"] = (await describe_mail(db))["ready"]
        complete, options = await registration_options(db, tx, context, version)
        result["roleOptionsComplete"] = complete
        result["fieldPolicy"]["complete"]["registration_role_id"] = complete
        if complete:
            result["roleOptions"] = options
    return result


@read_phase("projection")
async def fixed_setting_values(db, tenant_id, keys):
    keys = tuple(keys)
    if any(
        key not in SETTINGS
        or SETTINGS[key].kind == "secret"
        or key.startswith(("security:", "ai:"))
        for key in keys
    ):
        raise BusinessRuleException(
            "Invalid fixed settings projection", error_code="SETTING_GROUP_INVALID"
        )
    size = func.pg_catalog.octet_length(SystemSetting.setting_value)
    rows = (
        await db.execute(
            select(SystemSetting.setting_key, SystemSetting.setting_value)
            .where(
                SystemSetting.tenant_id == tenant_id,
                SystemSetting.setting_key.in_(keys),
                SystemSetting.status == "1",
                or_(
                    and_(
                        SystemSetting.setting_key.in_(
                            ("user_agreement", "privacy_policy")
                        ),
                        size <= 400 * 1024,
                    ),
                    size <= 8192,
                ),
            )
            .limit(len(keys) + 1)
        )
    ).all()
    stored = dict(rows)
    return {
        key: _decode(SETTINGS[key], stored.get(key, SETTINGS[key].default))
        for key in keys
    }


@read_phase("projection")
async def read_fixed_settings(db, tx, tenant, version, operation):
    if operation == "settings.public":
        if version is not None:
            raise AuthorizationException("Invalid public settings identity")
        await require_public_locator(db, tx, tenant)
        return await fixed_setting_values(
            db, tenant.tenant_id, tuple(key for key, d in SETTINGS.items() if d.public)
        )
    actor = await fixed_actor(
        db,
        tenant,
        version,
        "system:setting:list" if operation == "settings.groups" else None,
    )
    if operation == "settings.groups":
        return [
            group
            for group in SETTING_GROUPS
            if group != "security" or is_system_admin(actor)
        ] + (["mail"] if is_system_admin(actor) else [])
    values = await fixed_setting_values(
        db,
        tenant.tenant_id,
        (
            "default_locale",
            "user_require_primary_dept",
            "default_avatar",
            *(key for key in SETTINGS if key.startswith("site_")),
        ),
    )
    return {
        "defaultLocale": values["default_locale"],
        "requirePrimaryDept": values["user_require_primary_dept"],
        "brand": {
            key: value for key, value in values.items() if key.startswith("site_")
        },
        "defaultAvatar": values["default_avatar"],
        "uploads": await file_policy_service.describe(db, tenant=tenant),
    }
