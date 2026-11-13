"""Configuration writers with complete plans and fixed production owners.

The caller owns commit/rollback and all cache effects. SMTP preparation and
completion are short transactions separated by a DB-free bounded worker.
"""

import hashlib
import hmac
import smtplib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from weakref import WeakKeyDictionary, ref

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.core.authorization_io import smtp_worker
from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.field_policy import resolve_fields
from app.core.field_policy.contracts import denied
from app.core.id_generator import next_id
from app.core.security import decrypt_value, encrypt_value
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.db.base import role_menus
from app.modules.system.field_policy.control_catalog import (
    CREATE,
    IMPORT_CREATE,
    IMPORT_MATCH,
    READ,
    UPDATE,
)
from app.modules.system.field_policy.control_values import (
    PUBLIC_TEXT_SCHEMA,
    ConfigContent,
    prepare_value,
    publication_digest,
)
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.menu import Menu
from app.modules.system.models.setting import SystemSetting
from app.modules.system.service import mail_service as mail_transport
from app.modules.system.service.account_service import account_service
from app.modules.system.service.authorization_configuration_input import (
    ConfigImportPreparation,
    freeze_input,
    invalid,
)
from app.modules.system.service.authorization_configuration_input import (
    parse_import as prepare_config_import,
)
from app.modules.system.service.authorization_control_identity import (
    ControlAudit,
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.config_service import (
    MASKED_CONFIG_VALUE,
    _is_sensitive_config_key,
    _require_custom_key,
)
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.mail_service import MailSettings, ready
from app.modules.system.service.parameter_context import load_parameter_context
from app.modules.system.service.parameter_facts import parameter_rows
from app.modules.system.service.parameter_receipt import (
    observed_form_facts,
    require_publication_revision,
    require_receipt,
)
from app.modules.system.service.parameter_registration import (
    require_registration_choice,
)
from app.modules.system.service.parameter_setting_facts import setting_snapshot
from app.modules.system.service.parameter_settings import (
    describe_mail,
    read_group,
    setting_action,
)
from app.modules.system.service.relation_authorization_safety import (
    menu_after,
    require_manager_preserved,
    validate_menu_delegation,
)
from app.modules.system.service.relation_field_write_guard import validate_menu_fields
from app.modules.system.service.settings_service import _decode
from app.modules.system.settings_catalog import SETTINGS, is_builtin_key

CONFIGURATION_PERMISSIONS = MappingProxyType(
    {
        "menu.create": "system:menu:add",
        "menu.update": "system:menu:edit",
        "menu.delete": "system:menu:delete",
        "menu.batch_delete": "system:menu:batch-delete",
        "settings.save": "system:setting:edit",
        "mail.save": "system:setting:edit",
        "config.create": "system:config:add",
        "config.update": "system:config:edit",
        "config.delete": "system:config:delete",
        "config.batch_delete": "system:config:batch-delete",
        "config.import": "system:config:import",
        "config.publish": "system:config:publish",
        "config.revoke": "system:config:publish",
    }
)
_MAIL_ISSUED = object()
_MAIL_STATES = WeakKeyDictionary()
_MAIL_KEY = ResourceKey(ResourceKind.MAIL_CONFIG, None, (1,))


def _freeze(value):
    if isinstance(value, dict):
        return tuple((key, _freeze(item)) for key, item in sorted(value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _conflict():
    error = BusinessRuleException(
        "Reload settings before saving", error_code="SETTINGS_CONFLICT"
    )
    error.code = 409
    return error


def _stale():
    return BusinessRuleException(
        "Configuration snapshot changed", error_code="AUTHORIZATION_SNAPSHOT_STALE"
    )


def _key(kind, tenant_id, identity):
    return ResourceKey(kind, tenant_id, identity)


def _public(row):
    result = dict(row)
    for name, value in result.items():
        if name.endswith("_id") and value is not None:
            result[name] = str(value)
        elif isinstance(value, datetime):
            result[name] = value.isoformat()
    if "config_value" in result and _is_sensitive_config_key(result.get("config_key")):
        result["config_value"] = MASKED_CONFIG_VALUE
    return result


def _mail_config(row):
    if row is None:
        return MailSettings()
    return MailSettings(
        **{
            **row["values"],
            "password": decrypt_value(row["password_ciphertext"])
            if row["password_ciphertext"]
            else "",
        }
    )


@dataclass(frozen=True, slots=True)
class ConfigurationExecution:
    value: object
    cache_keys: tuple[str, ...] = ()
    cache_complete: bool = True


@dataclass(frozen=True, slots=True)
class _Change:
    action: str
    key: ResourceKey
    values: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class MailTestPreparation:
    tenant: object
    actor_user_version: int
    authorization_audit_id: int
    recipient: str = field(repr=False)
    _row: tuple = field(repr=False)
    _config_json: str = field(repr=False)
    _issued: object = field(repr=False, compare=False)
    _session: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MailTestOutcome:
    accepted: bool
    tested_at: datetime
    _preparation: MailTestPreparation = field(repr=False, compare=False)
    _issued: object = field(repr=False, compare=False)
    _token: object = field(default_factory=object, repr=False, compare=False)


class ConfigurationAuthorizationCandidate:
    async def parse_import(self, payload: bytes) -> ConfigImportPreparation:
        return await prepare_config_import(payload)

    async def _rows(self, db, tenant_id, operation, payload):
        if operation.startswith("menu."):
            table = Menu.__table__
        elif operation.startswith("config."):
            table = Config.__table__
        elif operation == "settings.save":
            table = SystemSetting.__table__
        else:
            table = None
        rows = (
            await parameter_rows(db, table, tenant_id)
            if operation.startswith("config.") or operation == "settings.save"
            else (
                []
                if table is None
                else [
                    dict(row)
                    for row in (
                        await db.execute(
                            select(table)
                            .where(table.c.tenant_id == tenant_id)
                            .order_by(*table.primary_key.columns)
                        )
                    ).mappings()
                ]
            )
        )
        links = []
        if operation.startswith("menu."):
            links = [
                dict(row)
                for row in (
                    await db.execute(
                        select(role_menus)
                        .where(role_menus.c.tenant_id == tenant_id)
                        .order_by(role_menus.c.role_id, role_menus.c.menu_id)
                    )
                ).mappings()
            ]
        mail = None
        if operation.startswith("mail.") or (
            operation == "settings.save" and payload["group"] == "account"
        ):
            result = (
                (
                    await db.execute(
                        select(MailConfig.__table__).where(MailConfig.config_id == 1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            mail = dict(result) if result else None
        publications = (
            await parameter_rows(db, ConfigPublication.__table__, tenant_id)
            if operation.startswith("config.")
            else []
        )
        return rows, links, mail, publications

    def _audit(
        self,
        audit,
        *,
        required,
        tenant,
        actor_user_version,
        operation,
        target_tenant_id,
    ):
        if audit is None:
            if required:
                raise _order_error()
            return
        if type(audit) is not ControlAudit:
            raise _order_error()
        audit.assert_request(
            tenant=tenant,
            actor_user_version=actor_user_version,
            permission=CONFIGURATION_PERMISSIONS.get(operation, "system:setting:edit"),
            target_tenant_id=target_tenant_id,
            operation=operation,
        )

    async def execute(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        operation,
        payload=None,
        target_id=None,
        target_tenant_id=None,
        expected_snapshot=None,
        audit=None,
        http_protocol=False,
        protocol_receipt=None,
    ):
        require_candidate_session(db)
        if type(http_protocol) is not bool:
            raise _order_error()
        if type(operation) is not str or operation not in CONFIGURATION_PERMISSIONS:
            raise _order_error()
        payload, target_id = freeze_input(operation, payload, target_id)
        global_write = operation == "mail.save" or (
            operation == "settings.save" and payload["group"] == "security"
        )
        if target_tenant_id is not None and (
            global_write
            or type(target_tenant_id) is not int
            or target_tenant_id != tenant.tenant_id
        ):
            raise _order_error()
        self._audit(
            audit,
            required=global_write,
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation=operation,
            target_tenant_id=target_tenant_id,
        )
        timeout = 60000 if global_write or operation == "config.import" else 15000
        scope = (
            authorization_transactions.scope_global(
                db, budget=TransactionBudget(timeout_ms=timeout)
            )
            if global_write
            else authorization_transactions.scope(
                db,
                tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                budget=TransactionBudget(timeout_ms=timeout),
            )
        )
        with bounded_authorization_reads(
            db, AuthorizationReadBudget(max_statements=8192)
        ):
            async with scope as tx:
                identity = await read_identity(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    permissions=(CONFIGURATION_PERMISSIONS[operation],),
                    system=global_write,
                )
                facts = await self._rows(db, tenant.tenant_id, operation, payload)
                settings_context = None
                if operation == "settings.save" and payload["group"] != "security":
                    settings_context = await load_parameter_context(
                        db,
                        tx,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        action="settings.save",
                    )
                    if http_protocol:
                        observed = await observed_form_facts(
                            db,
                            tx,
                            settings_context,
                            mode="group",
                            group=payload["group"],
                        )
                        require_receipt(protocol_receipt, observed)
                    for name in payload["values"]:
                        settings_context.write(
                            "system.setting",
                            "settings",
                            setting_action(name),
                            UPDATE,
                            (name,),
                        )
                    if "registration_role_id" in payload["values"]:
                        await require_registration_choice(
                            db,
                            tx,
                            settings_context,
                            actor_user_version,
                            payload["values"]["registration_role_id"],
                        )
                if expected_snapshot is not None:
                    digest = hashlib.sha256(repr(_freeze(facts)).encode()).hexdigest()
                    if type(expected_snapshot) is not str or not hmac.compare_digest(
                        expected_snapshot, digest
                    ):
                        raise _stale()
                if operation in {"config.publish", "config.revoke"}:
                    changes, result_id, immediate = await self._publication(
                        db,
                        tx,
                        tenant,
                        actor_user_version,
                        operation,
                        payload,
                        target_id,
                        facts,
                    )
                else:
                    changes, result_id, immediate = await self._prepare(
                        db, tenant, operation, payload, target_id, facts
                    )
                if operation.startswith("config."):
                    action = (
                        "config.unpublish"
                        if operation == "config.revoke"
                        else operation.replace("_", "-")
                    )
                    context = await load_parameter_context(
                        db,
                        tx,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        action=action,
                    )
                    if http_protocol and operation in {
                        "config.create",
                        "config.update",
                    }:
                        observed = await observed_form_facts(
                            db,
                            tx,
                            context,
                            mode=operation.split(".")[1],
                            target_id=target_id,
                        )
                        require_receipt(protocol_receipt, observed)
                    if operation == "config.import":
                        await context.prove(
                            db,
                            "system.config",
                            Config.__table__,
                            (("config_key", IMPORT_MATCH),),
                            readable=("config_key",),
                        )
                    for change in changes:
                        if operation in {"config.publish", "config.revoke"}:
                            continue
                        if change.action not in {"insert", "update"}:
                            continue
                        access = (
                            IMPORT_CREATE
                            if operation == "config.import"
                            else CREATE
                            if change.action == "insert"
                            else UPDATE
                        )
                        fields = tuple(
                            n
                            for n in change.values
                            if n
                            not in {
                                "tenant_id",
                                "config_id",
                                "value_sensitivity",
                                "create_by",
                                "update_by",
                            }
                            and not (n == "is_public" and change.action == "insert")
                        )
                        context.write(
                            "system.config",
                            change.key.identity[0],
                            action,
                            access,
                            fields,
                        )
                    if operation == "config.update":
                        old = next(
                            row for row in facts[0] if row["config_id"] == target_id
                        )
                        changed = any(
                            name
                            in {
                                "config_key",
                                "config_value",
                                "config_type",
                                "status",
                                "value_sensitivity",
                            }
                            and value != old[name]
                            for change in changes
                            for name, value in change.values.items()
                        )
                        if changed:
                            changes = [
                                replace(
                                    change, values={**change.values, "is_public": False}
                                )
                                for change in changes
                            ]
                            if any(row["config_id"] == target_id for row in facts[3]):
                                changes.insert(
                                    0,
                                    _Change(
                                        "delete",
                                        _key(
                                            ResourceKind.CONFIG_PUBLICATION,
                                            tenant.tenant_id,
                                            target_id,
                                        ),
                                    ),
                                )
                    if operation in {"config.delete", "config.batch_delete"}:
                        targets = {change.key.identity[0] for change in changes}
                        changes = [
                            _Change(
                                "delete",
                                _key(
                                    ResourceKind.CONFIG_PUBLICATION,
                                    tenant.tenant_id,
                                    row["config_id"],
                                ),
                            )
                            for row in facts[3]
                            if row["config_id"] in targets
                        ] + changes
                if operation.startswith("menu."):
                    all_facts = {
                        ResourceKind(kind): [dict(row) for row in rows]
                        for kind, rows in identity._facts
                    }
                    after = menu_after(all_facts, changes)
                    require_manager_preserved(all_facts, after)
                    authority = await grant_authority_service.build(
                        db, tenant.actor_user_id, tenant=tenant
                    )
                    validate_menu_delegation(all_facts, after, changes, authority)
                    await validate_menu_fields(
                        db,
                        tx,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        facts=all_facts,
                        changes=changes,
                    )
                if operation in {"menu.create", "menu.update"}:
                    audit_field = (
                        "create_by" if operation == "menu.create" else "update_by"
                    )
                    changes = [
                        replace(
                            change,
                            values={**change.values, audit_field: identity.user_name},
                        )
                        if change.key.kind == ResourceKind.MENU
                        and change.key.identity == (result_id,)
                        and change.action in {"insert", "update"}
                        else change
                        for change in changes
                    ]
                existing = set(identity.keys)
                kind = (
                    ResourceKind.MENU
                    if operation.startswith("menu.")
                    else ResourceKind.CONFIG
                    if operation.startswith("config.")
                    else ResourceKind.SETTING
                )
                id_column = {
                    ResourceKind.MENU: "menu_id",
                    ResourceKind.CONFIG: "config_id",
                    ResourceKind.SETTING: "setting_key",
                }[kind]
                existing.update(
                    _key(kind, tenant.tenant_id, (row[id_column],)) for row in facts[0]
                )
                existing.update(
                    _key(
                        ResourceKind.CONFIG_PUBLICATION,
                        tenant.tenant_id,
                        row["config_id"],
                    )
                    for row in facts[3]
                )
                existing.update(
                    _key(
                        ResourceKind.ROLE_MENUS,
                        tenant.tenant_id,
                        (row["role_id"], row["menu_id"]),
                    )
                    for row in facts[1]
                )
                if facts[2] is not None:
                    existing.add(_MAIL_KEY)
                new = tuple(
                    NewResource(change.key)
                    for change in changes
                    if change.action == "insert"
                )
                if audit is not None:
                    existing.update(audit.existing)
                    new += audit.new
                cache_keys, cache_complete = self._cache(
                    tenant.tenant_id, operation, facts[0], changes
                )
                await tx.lock(LockPlan(existing=tuple(existing), new=new))
                await assert_identity(db, identity)
                if _freeze(
                    await self._rows(db, tenant.tenant_id, operation, payload)
                ) != _freeze(facts):
                    raise _stale()
                if (
                    operation == "mail.save"
                    and changes
                    and changes[0].action == "metadata"
                ):
                    await tx.update_metadata(
                        _MAIL_KEY,
                        changes[0].values,
                        expected={"version": facts[2]["version"]},
                    )
                else:
                    aggregate = (
                        tx.aggregate_global()
                        if global_write
                        else tx.aggregate(tenant.tenant_id)
                    )
                    async with aggregate:
                        for change in changes:
                            if change.action == "insert":
                                await tx.insert_reserved(change.key, change.values)
                            elif change.action == "update":
                                await tx.update_held(change.key, change.values)
                            else:
                                await tx.delete_held(change.key)
                if audit is not None:
                    await audit.append(
                        tx, {"operation": operation, "changed_rows": len(changes)}
                    )
                if operation == "settings.save":
                    result = await read_group(
                        db,
                        tx,
                        tenant,
                        actor_user_version,
                        payload["group"],
                        context=settings_context,
                    )
                else:
                    result = await self._result(
                        db, tenant, operation, payload, result_id, immediate
                    )
                return ConfigurationExecution(result, cache_keys, cache_complete)

    async def _prepare(self, db, tenant, operation, payload, target_id, facts):
        rows, links, mail, _publications = facts
        if operation.startswith("menu."):
            return self._menu(
                tenant.tenant_id, operation, payload, target_id, rows, links
            )
        if operation.startswith("config."):
            return self._config(tenant.tenant_id, operation, payload, target_id, rows)
        if operation == "settings.save":
            return await self._settings(db, tenant, payload, rows, mail)
        if mail is None or payload["revision"] != str(mail["version"]):
            raise _conflict()
        old = _mail_config(mail)
        config = MailSettings(**payload["values"])
        if payload["clear_password"] and config.password:
            raise invalid("SETTING_VALUE_INVALID")
        if not payload["clear_password"] and not config.password:
            config = config.model_copy(update={"password": old.password})
        if config.enabled and not ready(config):
            raise invalid("MAIL_NOT_CONFIGURED")
        values = {
            "version": mail["version"] + 1,
            "last_test_at": None,
            "last_test_status": "untested",
        }
        if config == old:
            return [_Change("metadata", _MAIL_KEY, values)], 1, None
        values.update(
            values=config.model_dump(exclude={"password"}),
            password_ciphertext=encrypt_value(config.password)
            if config.password
            else "",
        )
        return [_Change("update", _MAIL_KEY, values)], 1, None

    async def _publication(
        self, db, tx, tenant, actor_version, operation, payload, target_id, facts
    ):
        action = (
            "config.unpublish" if operation == "config.revoke" else "config.publish"
        )
        context = await load_parameter_context(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_version,
            action=action,
        )
        current = next((row for row in facts[0] if row["config_id"] == target_id), None)
        if current is None:
            raise NotFoundException("配置", target_id)
        _require_custom_key(current["config_key"])
        old = next((row for row in facts[3] if row["config_id"] == target_id), None)
        observed = await observed_form_facts(
            db, tx, context, mode="update", target_id=target_id
        )
        record = context.record("system.config", target_id, context.scopes)
        for access in (READ,) if operation == "config.revoke" else (READ, UPDATE):
            decisions = resolve_fields(
                context.catalog, context.rules, record, action, access
            )
            required = {
                use.ref.field_id
                for use in context.catalog.actions[action].uses
                if use.access == access
            }
            if any(not decisions[name].allowed for name in required):
                raise denied()
        require_publication_revision(
            payload["expected_revision"],
            observed,
            current,
            old,
            kind="revoke" if operation == "config.revoke" else "publish",
        )
        key = _key(ResourceKind.CONFIG_PUBLICATION, tenant.tenant_id, target_id)
        config_key = _key(ResourceKind.CONFIG, tenant.tenant_id, target_id)
        if operation == "config.revoke":
            changes = [_Change("delete", key)] if old else []
            if current["is_public"]:
                changes.append(_Change("update", config_key, {"is_public": False}))
            return changes, target_id, {"published": False}
        if current["value_sensitivity"] != "private" or current["config_value"] is None:
            raise invalid("CONFIG_PUBLICATION_UNSAFE")
        content = ConfigContent(
            **{name: current[name] for name in ConfigContent.model_fields}
        )
        values = {
            "schema_revision": PUBLIC_TEXT_SCHEMA,
            "content_digest": publication_digest(content),
            "publication_version": old["publication_version"] + 1 if old else 1,
            "published_by_user_id": tenant.actor_user_id,
            "published_at": datetime.now(UTC),
        }
        return (
            [
                _Change("update" if old else "insert", key, values),
                _Change("update", config_key, {"is_public": True}),
            ],
            target_id,
            {"published": True},
        )

    def _menu(self, tenant_id, operation, payload, target_id, rows, links):
        by_id = {row["menu_id"]: row for row in rows}
        changes = []
        result_id = target_id
        deleted = set()
        if operation == "menu.create":
            result_id = next_id()
            values = {
                key: value
                for key, value in payload.items()
                if key not in {"buttons", "query"}
            }
            self._parent(values.get("parent_id"), result_id, by_id)
            changes.append(
                _Change(
                    "insert",
                    _key(ResourceKind.MENU, tenant_id, (result_id,)),
                    {**values, "tenant_id": tenant_id, "menu_id": result_id},
                )
            )
            for button in payload["buttons"]:
                changes.append(self._new_button(tenant_id, result_id, button))
        elif operation == "menu.update":
            if target_id not in by_id:
                raise NotFoundException("菜单", target_id)
            current = by_id[target_id]
            values = {key: value for key, value in payload.items() if key != "buttons"}
            if "parent_id" in values:
                self._parent(values["parent_id"], target_id, by_id)
            if (
                values.get("menu_name", current["menu_name"]) != current["menu_name"]
                and values.get("i18n_key", current["i18n_key"]) == current["i18n_key"]
            ):
                values["i18n_key"] = ""
            changes.append(
                _Change(
                    "update", _key(ResourceKind.MENU, tenant_id, (target_id,)), values
                )
            )
            if payload.get("buttons") is not None:
                children = {
                    row["permission"]: row
                    for row in rows
                    if row["parent_id"] == target_id and row["menu_type"] == "F"
                }
                if len(children) != sum(
                    row["parent_id"] == target_id and row["menu_type"] == "F"
                    for row in rows
                ):
                    raise invalid("MENU_BUTTONS_AMBIGUOUS")
                codes = {button["code"] for button in payload["buttons"]}
                deleted = {
                    row["menu_id"]
                    for code, row in children.items()
                    if code not in codes
                }
                for button in payload["buttons"]:
                    old = children.get(button["code"])
                    if old is None:
                        changes.append(self._new_button(tenant_id, target_id, button))
                    else:
                        values = {"menu_name": button["desc"]}
                        if old["menu_name"] != button["desc"]:
                            values["i18n_key"] = ""
                        changes.append(
                            _Change(
                                "update",
                                _key(ResourceKind.MENU, tenant_id, (old["menu_id"],)),
                                values,
                            )
                        )
        else:
            deleted = (
                set(payload) if operation.endswith("batch_delete") else {target_id}
            )
            if not deleted <= by_id.keys():
                raise NotFoundException("菜单", target_id)
        if any(
            row["parent_id"] in deleted and row["menu_id"] not in deleted
            for row in rows
        ):
            raise invalid("MENU_HAS_CHILDREN")
        removals = [
            _Change(
                "delete",
                _key(
                    ResourceKind.ROLE_MENUS,
                    tenant_id,
                    (link["role_id"], link["menu_id"]),
                ),
            )
            for link in links
            if link["menu_id"] in deleted
        ]
        pending = set(deleted)
        while pending:
            leaves = sorted(
                key
                for key in pending
                if not any(by_id[child]["parent_id"] == key for child in pending)
            )
            if not leaves:
                raise invalid("MENU_PARENT_INVALID")
            removals.extend(
                _Change("delete", _key(ResourceKind.MENU, tenant_id, (key,)))
                for key in leaves
            )
            pending.difference_update(leaves)
        changes = removals + changes
        immediate = (
            len(deleted)
            if operation.endswith("batch_delete")
            else True
            if operation == "menu.delete"
            else None
        )
        return changes, result_id, immediate

    def _parent(self, parent_id, target_id, rows):
        visited = {target_id}
        while parent_id is not None:
            if parent_id in visited or parent_id not in rows:
                raise invalid("MENU_PARENT_INVALID")
            visited.add(parent_id)
            parent_id = rows[parent_id]["parent_id"]

    def _new_button(self, tenant_id, parent_id, button):
        row_id = next_id()
        return _Change(
            "insert",
            _key(ResourceKind.MENU, tenant_id, (row_id,)),
            {
                "menu_id": row_id,
                "tenant_id": tenant_id,
                "parent_id": parent_id,
                "menu_name": button["desc"],
                "permission": button["code"],
                "menu_type": "F",
                "order": 0,
                "multi_tab": False,
                "status": "1",
            },
        )

    def _config(self, tenant_id, operation, payload, target_id, rows):
        by_id = {row["config_id"]: row for row in rows}
        by_key = {row["config_key"]: row for row in rows}
        if operation == "config.import":
            changes, skipped = [], 0
            for record in payload.records:
                values = dict(record)
                classified = prepare_value(
                    key=values["config_key"],
                    config_type=values["config_type"],
                    replacement=values["config_value"],
                )
                values["value_sensitivity"] = classified.sensitivity.value
                if values["config_key"] in by_key or is_builtin_key(
                    values["config_key"]
                ):
                    skipped += 1
                    continue
                row_id = next_id()
                changes.append(
                    _Change(
                        "insert",
                        _key(ResourceKind.CONFIG, tenant_id, (row_id,)),
                        {**values, "config_id": row_id, "tenant_id": tenant_id},
                    )
                )
                by_key[values["config_key"]] = values
            return changes, None, {"success": len(changes), "skipped": skipped}
        if operation == "config.create":
            classified = prepare_value(
                key=payload["config_key"],
                config_type=payload["config_type"],
                replacement=payload["config_value"],
            )
            payload = {**payload, "value_sensitivity": classified.sensitivity.value}
            _require_custom_key(payload["config_key"])
            if payload["config_key"] in by_key:
                raise DuplicateException("配置键", payload["config_key"])
            row_id = next_id()
            return (
                [
                    _Change(
                        "insert",
                        _key(ResourceKind.CONFIG, tenant_id, (row_id,)),
                        {**payload, "tenant_id": tenant_id, "config_id": row_id},
                    )
                ],
                row_id,
                None,
            )
        ids = set(payload) if operation.endswith("batch_delete") else {target_id}
        if not ids <= by_id.keys():
            raise NotFoundException("配置", target_id)
        for row_id in ids:
            _require_custom_key(by_id[row_id]["config_key"])
        if operation == "config.update":
            values = dict(payload)
            old = by_id[target_id]
            options = {
                "key": values.get("config_key", old["config_key"]),
                "config_type": values.get("config_type", old["config_type"]),
                "previous": old["value_sensitivity"],
            }
            if "config_value" in values:
                options["replacement"] = values["config_value"]
            classified = prepare_value(**options)
            if not classified.write_value:
                values.pop("config_value", None)
            if classified.sensitivity.value != old["value_sensitivity"]:
                values["value_sensitivity"] = classified.sensitivity.value
            if "config_key" in values:
                _require_custom_key(values["config_key"])
                existing = by_key.get(values["config_key"])
                if existing is not None and existing["config_id"] != target_id:
                    raise DuplicateException("配置键", values["config_key"])
            if values.get("config_value") == MASKED_CONFIG_VALUE:
                values.pop("config_value")
            return (
                [
                    _Change(
                        "update",
                        _key(ResourceKind.CONFIG, tenant_id, (target_id,)),
                        values,
                    )
                ],
                target_id,
                None,
            )
        return (
            [
                _Change("delete", _key(ResourceKind.CONFIG, tenant_id, (row_id,)))
                for row_id in sorted(ids)
            ],
            None,
            len(ids) if operation.endswith("batch_delete") else True,
        )

    async def _settings(self, db, tenant, payload, rows, _mail):
        group = payload["group"]
        definitions = {key: d for key, d in SETTINGS.items() if d.group == group}
        snapshot, revision = await setting_snapshot(
            db,
            tenant.tenant_id,
            group,
            readable=tuple(key for key, d in definitions.items() if d.kind != "secret"),
        )
        stored = {row["setting_key"]: row for row in snapshot}
        current_values = {
            key: _decode(d, stored[key]["setting_value"])
            if key in stored
            and stored[key]["status"] == "1"
            and stored[key]["setting_value"] is not None
            else _decode(d, d.default)
            for key, d in definitions.items()
            if d.kind != "secret"
        }
        if not hmac.compare_digest(payload["revision"], revision):
            raise _conflict()
        if group == "account":
            merged = {
                **current_values,
                **{
                    key: _decode(SETTINGS[key], value)
                    for key, value in payload["values"].items()
                },
            }
            if (
                merged["register_enabled"] or merged["password_reset_enabled"]
            ) and not (await describe_mail(db))["ready"]:
                raise invalid("MAIL_NOT_CONFIGURED")
            await account_service.registration_role(
                db, tenant.tenant_id, merged["registration_role_id"]
            )
        by_key = {row["setting_key"]: row for row in rows}
        changes = []
        for key, value in payload["values"].items():
            if SETTINGS[key].kind == "secret" and not value:
                continue
            resource = _key(ResourceKind.SETTING, tenant.tenant_id, (key,))
            values = {"setting_value": value, "status": "1"}
            if key in by_key:
                changes.append(_Change("update", resource, values))
            else:
                changes.append(
                    _Change(
                        "insert",
                        resource,
                        {
                            **values,
                            "setting_key": key,
                            "tenant_id": tenant.tenant_id,
                        },
                    )
                )
        return changes, None, None

    def _cache(self, tenant_id, operation, rows, changes):
        if not operation.startswith("config.") or not changes:
            return (), True
        by_id = {row["config_id"]: row for row in rows}
        keys = {f"cache:tenant:{tenant_id}:config:custom:public"}
        for change in changes:
            if change.key.kind is not ResourceKind.CONFIG:
                continue
            old = by_id.get(change.key.identity[0])
            new = (
                {**(old or {}), **change.values} if change.action != "delete" else None
            )
            for row in (old, new):
                if row is not None:
                    keys.add(
                        f"cache:tenant:{tenant_id}:config:custom:key:{row['config_key']}"
                    )
                    keys.add(
                        f"cache:tenant:{tenant_id}:config:custom:group:{row['config_group']}"
                    )
            if len(keys) > 256:
                return (), False
        return tuple(sorted(keys)), True

    async def _result(self, db, tenant, operation, _payload, result_id, immediate):
        if immediate is not None:
            return immediate
        if operation.startswith("config."):
            return {"config_id": str(result_id)}
        if operation == "settings.save":
            raise _order_error()
        if operation == "mail.save":
            return await describe_mail(db)
        model, name = (
            (Menu, "menu_id")
            if operation.startswith("menu.")
            else (Config, "config_id")
        )
        if operation.startswith("menu."):
            if (
                await db.scalar(
                    select(FieldPolicyHead.tenant_id).where(
                        FieldPolicyHead.tenant_id == tenant.tenant_id
                    )
                )
                is not None
            ):
                return {"menu_id": str(result_id)}
        row = (
            (
                await db.execute(
                    select(model.__table__).where(
                        model.tenant_id == tenant.tenant_id,
                        getattr(model, name) == result_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        return _public(row)

    async def prepare_mail_test(
        self, db, *, tenant, actor_user_version, payload, audit
    ):
        require_candidate_session(db)
        if type(payload) is dict and set(payload) == {"recipient"}:
            payload = payload["recipient"]
        try:
            recipient = str(TypeAdapter(EmailStr).validate_python(payload))
        except ValidationError:
            raise invalid() from None
        self._audit(
            audit,
            required=True,
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation="mail.test",
            target_tenant_id=None,
        )
        with bounded_authorization_reads(
            db, AuthorizationReadBudget(max_statements=4096)
        ):
            async with authorization_transactions.scope(
                db,
                tenants=(TenantLock(tenant.tenant_id),),
                budget=TransactionBudget(timeout_ms=8000),
            ) as tx:
                identity = await read_identity(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    permissions=("system:setting:edit",),
                    system=True,
                )
                facts = await self._rows(db, tenant.tenant_id, "mail.test", None)
                if facts[2] is None:
                    raise invalid("MAIL_NOT_CONFIGURED")
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            set(identity.keys + audit.existing + (_MAIL_KEY,))
                        )
                    )
                )
                await assert_identity(db, identity)
                if _freeze(
                    await self._rows(db, tenant.tenant_id, "mail.test", None)
                ) != _freeze(facts):
                    raise _stale()
                config = _mail_config(facts[2])
                if not ready(config.model_copy(update={"enabled": True})):
                    raise invalid("MAIL_NOT_CONFIGURED")
                preparation = MailTestPreparation(
                    tenant,
                    actor_user_version,
                    audit.authorization_audit_id,
                    recipient,
                    _freeze(facts[2]),
                    config.model_dump_json(),
                    _MAIL_ISSUED,
                    ref(db),
                )
                _MAIL_STATES[preparation] = "read"
                return preparation

    async def run_mail_test(self, preparation):
        if (
            type(preparation) is not MailTestPreparation
            or preparation._issued is not _MAIL_ISSUED
            or _MAIL_STATES.get(preparation) != "read"
        ):
            raise _order_error()
        session = preparation._session()
        if session is not None and session.in_transaction():
            raise _order_error()
        _MAIL_STATES[preparation] = "sending"
        config = MailSettings.model_validate_json(preparation._config_json)
        accepted = ready(config.model_copy(update={"enabled": True}))
        if accepted:
            try:
                await smtp_worker.run(
                    mail_transport._send,
                    config,
                    preparation.recipient,
                    "HoHu SMTP test / 邮件测试",
                    "HoHu test email. SMTP acceptance does not guarantee inbox delivery.\nHoHu 测试邮件：SMTP 接受不代表已进入收件箱。",
                )
            except (smtplib.SMTPException, OSError, ValueError):
                accepted = False
        outcome = MailTestOutcome(
            accepted, datetime.now(UTC), preparation, _MAIL_ISSUED
        )
        _MAIL_STATES[preparation] = outcome._token
        return outcome

    async def complete_mail_test(
        self, db, *, tenant, actor_user_version, preparation, outcome, audit
    ):
        require_candidate_session(db)
        if (
            type(preparation) is not MailTestPreparation
            or preparation._issued is not _MAIL_ISSUED
            or type(outcome) is not MailTestOutcome
            or outcome._issued is not _MAIL_ISSUED
            or outcome._preparation is not preparation
            or _MAIL_STATES.get(preparation) is not outcome._token
            or tenant != preparation.tenant
            or actor_user_version != preparation.actor_user_version
            or type(audit) is not ControlAudit
            or audit.authorization_audit_id != preparation.authorization_audit_id
        ):
            raise _order_error()
        _MAIL_STATES[preparation] = "consumed"
        self._audit(
            audit,
            required=True,
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation="mail.test",
            target_tenant_id=None,
        )
        with bounded_authorization_reads(
            db, AuthorizationReadBudget(max_statements=4096)
        ):
            async with authorization_transactions.scope(
                db,
                tenants=(TenantLock(tenant.tenant_id),),
                budget=TransactionBudget(timeout_ms=8000),
            ) as tx:
                identity = await read_identity(
                    db,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    permissions=("system:setting:edit",),
                    system=True,
                )
                facts = await self._rows(db, tenant.tenant_id, "mail.test", None)
                if facts[2] is None or _freeze(facts[2]) != preparation._row:
                    raise _conflict()
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            set(identity.keys + audit.existing + (_MAIL_KEY,))
                        ),
                        new=audit.new,
                    )
                )
                await assert_identity(db, identity)
                current = (await self._rows(db, tenant.tenant_id, "mail.test", None))[2]
                if _freeze(current) != preparation._row:
                    raise _conflict()
                values = {
                    "last_test_at": outcome.tested_at,
                    "last_test_status": "accepted" if outcome.accepted else "failed",
                }
                await tx.update_metadata(
                    _MAIL_KEY, values, expected={"version": current["version"]}
                )
                await audit.append(
                    tx, {"operation": "mail.test", "accepted": outcome.accepted}
                )
                return await describe_mail(db)


configuration_authorization_candidate = ConfigurationAuthorizationCandidate()
