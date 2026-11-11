"""Dormant control declarations. Production keeps its exact seven-object catalog."""

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
    SourceNode,
    SourceRef,
    collect_sources,
    project,
    resolve_fields,
)
from app.core.field_policy.contracts import identity, require
from app.modules.system.field_policy.ordinary_catalog import relation_catalog

READ = Access("read", "management.read")
CREATE = Access("create", "management.create")
UPDATE = Access("update", "management.update")
CONTAINS = Access("contains", "management.query", "filter")
EQ = Access("eq", "management.query", "filter")
EXPORT = Access("export", "export")
IMPORT_CREATE = Access("create", "import")
IMPORT_MATCH = Access("exact", "import", "match")
CONTROL_CATALOG_REVISION = "system-fields/4-controls"
NEXT_RUN_SOURCES = (
    "status",
    "trigger_type",
    "cron_expression",
    "interval_value",
    "interval_unit",
)

_SETTING_FIELDS = (
    ("site_name", "string"),
    ("site_logo", "string"),
    ("site_description", "string"),
    ("site_icp", "string"),
    ("site_copyright", "string"),
    ("user_agreement", "string"),
    ("privacy_policy", "string"),
    ("default_avatar", "string"),
    ("default_locale", "string"),
    ("email_code_minutes", "integer"),
    ("register_enabled", "boolean"),
    ("password_reset_enabled", "boolean"),
    ("registration_role_id", "string"),
    ("user_require_primary_dept", "boolean"),
    ("upload:max_bytes", "integer"),
    ("upload:image_max_bytes", "integer"),
    ("upload:import_max_bytes", "integer"),
    ("upload:allowed_extensions", "string"),
    ("auth:default_password", "string"),
)
_ACCOUNT_FIELDS = frozenset(
    {
        "register_enabled",
        "password_reset_enabled",
        "registration_role_id",
        "user_require_primary_dept",
    }
)
_FILE_FIELDS = frozenset(
    {
        "upload:max_bytes",
        "upload:image_max_bytes",
        "upload:import_max_bytes",
        "upload:allowed_extensions",
    }
)
_PASSWORD = "auth:default_password"


def _uses(obj, accesses, names=None):
    return tuple(
        FieldUse(FieldRef(obj.object_id, f.field_id), a)
        for f in obj.fields
        for a in f.accesses
        if a in accesses and (names is None or f.field_id in names)
    )


def _config():
    fields = [FieldSpec("config_id", "id", (READ,), structural=True)]
    for name in (
        "config_name",
        "config_key",
        "config_value",
        "config_type",
        "config_group",
        "status",
        "remark",
    ):
        queries = (
            (CONTAINS, IMPORT_MATCH)
            if name == "config_key"
            else (CONTAINS,)
            if name in {"config_name", "config_group"}
            else (EQ,)
            if name == "status"
            else ()
        )
        fields.append(
            FieldSpec(
                name,
                "string",
                (READ, CREATE, UPDATE, EXPORT, IMPORT_CREATE, *queries),
                nullable=name == "remark",
            )
        )
    fields.extend(
        (
            FieldSpec("is_public", "boolean", (READ, EXPORT)),
            FieldSpec("create_time", "datetime", (READ,)),
            FieldSpec("update_time", "datetime", (READ,)),
        )
    )
    return ObjectSpec("system.config", "tenant", "config.tenant/v1", tuple(fields))


def _setting():
    return ObjectSpec(
        "system.setting",
        "tenant",
        "setting.key/v1",
        tuple(
            FieldSpec(
                key,
                kind,
                (UPDATE,) if key == _PASSWORD else (READ, UPDATE),
                read_cap="hidden" if key == _PASSWORD else "plain",
            )
            for key, kind in _SETTING_FIELDS
        ),
    )


def _job():
    fields = [FieldSpec("job_id", "id", (READ,), structural=True)]
    for name, kind, nullable in (
        ("job_name", "string", False),
        ("job_key", "string", False),
        ("cron_expression", "string", True),
        ("trigger_type", "string", False),
        ("interval_value", "integer", True),
        ("interval_unit", "string", True),
        ("job_args", "string", True),
        ("status", "string", False),
        ("concurrent", "string", False),
        ("timeout_seconds", "integer", True),
        ("max_retries", "integer", False),
        ("run_on_enable", "boolean", False),
        ("remark", "string", True),
    ):
        query = (
            (CONTAINS,)
            if name in {"job_name", "job_key"}
            else (EQ,)
            if name == "status"
            else ()
        )
        fields.append(
            FieldSpec(
                name,
                kind,
                (READ, CREATE, *((UPDATE,) if name != "job_key" else ()), *query),
                nullable=nullable,
            )
        )
    fields.extend(
        (
            FieldSpec("create_time", "datetime", (READ,)),
            FieldSpec("update_time", "datetime", (READ,)),
            FieldSpec(
                "next_run_time",
                "datetime",
                (),
                nullable=True,
                sources=tuple(
                    FieldRef("job.definition", name) for name in NEXT_RUN_SOURCES
                ),
            ),
        )
    )
    return ObjectSpec("job.definition", "tenant", "job.tenant/v1", tuple(fields))


@lru_cache(maxsize=1)
def control_catalog():
    """Build candidates without registering, preparing or activating them."""
    base, config, setting, job = relation_catalog(), _config(), _setting(), _job()
    actions = list(base.actions.values())

    def add(obj, action, permission, uses, *, special=False):
        actions.append(
            ActionSpec(
                action,
                (obj.object_id,),
                uses,
                (permission,),
                kind="special" if special else "data",
            )
        )

    for operation, permission, accesses in (
        ("list", "list", {READ, CONTAINS, EQ}),
        ("create", "add", {READ, CREATE}),
        ("update", "edit", {READ, UPDATE}),
        ("export", "export", {READ, EXPORT, CONTAINS, EQ}),
        ("import", "import", {IMPORT_CREATE, IMPORT_MATCH}),
    ):
        add(
            config,
            f"config.{operation}",
            f"system:config:{permission}",
            _uses(config, accesses),
        )
    for operation, permission in (
        ("delete", "delete"),
        ("batch-delete", "batch-delete"),
        ("unpublish", "publish"),
    ):
        add(
            config,
            f"config.{operation}",
            f"system:config:{permission}",
            _uses(config, {READ}, {"config_id"}),
            special=True,
        )
    actions.append(
        ActionSpec(
            "config.publish",
            (config.object_id,),
            _uses(
                config,
                {READ},
                {
                    "config_id",
                    "is_public",
                    "config_key",
                    "config_value",
                    "config_type",
                    "status",
                },
            )
            + _uses(
                config,
                {UPDATE},
                {"config_key", "config_value", "config_type", "status"},
            ),
            ("system:config:edit", "system:config:publish"),
            kind="special",
        )
    )

    add(setting, "settings.group", "system:setting:list", _uses(setting, {READ}))
    ordinary = (
        {key for key, _ in _SETTING_FIELDS}
        - _ACCOUNT_FIELDS
        - _FILE_FIELDS
        - {_PASSWORD}
    )
    for action, fields, special in (
        ("save", ordinary, False),
        ("account", _ACCOUNT_FIELDS, True),
        ("files", _FILE_FIELDS, True),
        ("password", {_PASSWORD}, True),
    ):
        add(
            setting,
            "settings." + action,
            "system:setting:edit",
            _uses(setting, {UPDATE}, fields),
            special=special,
        )
    role = base.objects["system.role"]
    actions.append(
        ActionSpec(
            "settings.registration-options",
            (setting.object_id, role.object_id),
            _uses(setting, {UPDATE}, {"registration_role_id"})
            + _uses(role, {READ}, {"role_id", "role_name"}),
            ("system:setting:edit", "system:user:role-auth"),
            kind="special",
        )
    )

    for operation, permission, accesses, names, special in (
        ("list", "list", {READ, CONTAINS, EQ}, None, False),
        ("create", "add", {READ, CREATE}, None, False),
        ("update", "edit", {UPDATE}, {"job_name", "remark"}, False),
        (
            "schedule",
            "edit",
            {UPDATE},
            {
                "cron_expression",
                "trigger_type",
                "interval_value",
                "interval_unit",
                "job_args",
                "concurrent",
                "timeout_seconds",
                "max_retries",
                "run_on_enable",
            },
            True,
        ),
        ("status", "edit", {UPDATE}, {"status"}, True),
        ("delete", "delete", {READ}, {"job_id"}, True),
        ("batch-delete", "batch-delete", {READ}, {"job_id"}, True),
        ("run", "run", {READ}, {"job_id", "job_key", "job_args", "status"}, True),
        ("registered", "list", set(), None, True),
    ):
        add(
            job,
            f"job.{operation}",
            f"system:job:{permission}",
            _uses(job, accesses, names),
            special=special,
        )
    return Catalog(
        tuple(base.objects.values()) + (config, setting, job), tuple(actions)
    )


@dataclass(frozen=True, slots=True)
class DerivedJobCell:
    visible: bool = False
    value: datetime | None = field(default=None, repr=False)
    sources: tuple[SourceRef, ...] = ()


def project_next_run(catalog, rules, context, values, computed_value):
    """Project an owner-computed value only with all actual sources readable.

    The trusted read owner establishes the row and computes its trigger. These
    refs record dependencies; they neither compute nor authorize job execution.
    """
    require(context.object_id == "job.definition")
    identity(context.record_key)
    decisions = resolve_fields(catalog, rules, context, "job.list", READ)
    if any(not decisions[name].allowed for name in NEXT_RUN_SOURCES):
        return DerivedJobCell()
    require(type(values) is dict and set(values) == set(NEXT_RUN_SOURCES))
    require(computed_value is None or type(computed_value) is datetime)
    project(catalog, rules, context, "job.list", READ, values)
    version = "job-next-run/v1"
    refs = tuple(
        SourceRef(
            "tenant",
            context.tenant_id,
            context.record_key,
            FieldRef(context.object_id, name),
            READ,
            catalog.digest,
            version,
        )
        for name in NEXT_RUN_SOURCES
    )
    sources = collect_sources(
        catalog,
        (SourceNode("next-run", refs, ()),),
        ("next-run",),
        tenant_id=context.tenant_id,
        projection_version=version,
    )
    return DerivedJobCell(True, computed_value, sources)
