"""Closed tenant delivery declarations; historical catalogs remain immutable."""

from dataclasses import replace
from functools import lru_cache
from types import MappingProxyType

from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
)
from app.modules.system.field_policy.transfer_catalog import READ, transfer_catalog

DELIVERY_CATALOG_REVISION = "system-fields/6-delivery"
CREATE = Access("create", "management.create")
UPDATE = Access("update", "management.update")
EQ = Access("eq", "management.query", "filter")
CONTAINS = Access("contains", "management.query", "filter")
RANGE = Access("range", "management.query", "filter")
SORT = Access("sort", "management.query", "sort")
GROUP = Access("group", "management.query", "group")
AGGREGATE = Access("aggregate", "management.query", "aggregate")
JOIN = Access("eq", "management.query", "join")

# This list is code-owned. Registry callbacks and caller-supplied tool names do
# not extend it or provide field authority.
TOOL_OBJECTS = MappingProxyType(
    {
        **{
            f"user.{op}": ("system.user", "system.dept", "system.role")
            for op in (
                "list",
                "lookup",
                "count",
                "stats",
                "distinct",
                "create",
                "update",
                "reset_password",
                "batch_delete",
                "update_roles",
                "update_dept",
                "role_lookup",
                "dept_lookup",
                "export",
                "import_preview",
                "import_execute",
            )
        },
        **{
            f"role.{op}": (
                "system.role",
                "system.menu",
                "system.dept",
                "system.user",
                "ai.agent",
            )
            for op in (
                "list",
                "lookup",
                "count",
                "menu_lookup",
                "agent_lookup",
                "create",
                "update",
                "update_menus",
                "update_agents",
            )
        },
        **{
            f"dept.{op}": ("system.dept", "system.user")
            for op in (
                "list",
                "lookup",
                "count",
                "create",
                "update",
                "move",
            )
        },
        "file.parse": ("system.file", "system.user"),
        "job.update_cron": ("job.definition",),
    }
)


def _field(name, kind="string", *, query=(), write=(), sources=()):
    return FieldSpec(name, kind, (READ, *query, *write), nullable=True, sources=sources)


def _object(obj, anchor, scope, fields, *, anchor_type="id"):
    return ObjectSpec(
        obj,
        "tenant",
        scope,
        (FieldSpec(anchor, anchor_type, (READ,), structural=True), *fields),
    )


@lru_cache(maxsize=1)
def delivery_catalog():
    base = transfer_catalog()
    user_name = (FieldRef("system.user", "user_name"),)
    extensions = {
        "system.menu": dict.fromkeys(
            ("permission", "route_name", "route_path", "i18n_key"), (CONTAINS,)
        ),
        "system.dept": dict.fromkeys(("parent_id", "ancestors"), (JOIN,)),
    }
    existing = tuple(
        replace(
            obj,
            fields=tuple(
                replace(
                    f,
                    accesses=tuple(
                        dict.fromkeys(
                            (
                                *f.accesses,
                                *extensions.get(obj.object_id, {}).get(f.field_id, ()),
                            )
                        )
                    ),
                )
                for f in obj.fields
            ),
        )
        for obj in base.objects.values()
    )
    objects = existing + (
        _object(
            "system.operation_log",
            "operation_log_id",
            "audit.tenant/v1",
            (
                *(_field(n, query=(CONTAINS,)) for n in ("module", "action")),
                _field("method"),
                _field("path"),
                _field("status_code", "integer", query=(EQ,)),
                _field("ip"),
                _field("duration", "integer"),
                _field("create_time", "datetime", query=(RANGE, SORT)),
                _field("username", query=(CONTAINS,), sources=user_name),
            ),
        ),
        _object(
            "system.login_log",
            "login_log_id",
            "audit.tenant/v1",
            (
                _field("username", query=(CONTAINS,), sources=user_name),
                _field("ip", query=(CONTAINS,)),
                _field("status", query=(EQ,)),
                _field("login_time", "datetime", query=(RANGE, SORT)),
            ),
        ),
        _object(
            "job.log",
            "job_log_id",
            "audit.tenant/v1",
            (
                _field("job_id", "id", query=(EQ,)),
                _field("job_name", sources=(FieldRef("job.definition", "job_name"),)),
                _field(
                    "job_key",
                    query=(CONTAINS,),
                    sources=(FieldRef("job.definition", "job_key"),),
                ),
                _field("status", query=(EQ,)),
                _field("start_time", "datetime", query=(RANGE, SORT)),
                _field("end_time", "datetime"),
                _field("duration", "integer"),
                _field("attempt_count", "integer"),
            ),
        ),
        _object(
            "ai.agent",
            "agent_id",
            "agent.visible/v1",
            (
                FieldSpec("code", "opaque_id", (READ,), structural=True),
                _field("name", query=(CONTAINS,)),
                _field("description", query=(CONTAINS,)),
                _field(
                    "i18n_keys",
                    "json",
                    sources=(
                        FieldRef("ai.agent", "name"),
                        FieldRef("ai.agent", "description"),
                    ),
                ),
                _field("display_order", "integer", query=(SORT,)),
            ),
        ),
        _object(
            "ai.conversation",
            "conversation_id",
            "ai.owner/v1",
            (
                _field("title", query=(CONTAINS,), write=(CREATE, UPDATE)),
                _field("status", "integer", query=(EQ,), write=(UPDATE,)),
                _field("create_time", "datetime"),
                _field("update_time", "datetime", query=(SORT,)),
            ),
        ),
        _object(
            "ai.message",
            "message_id",
            "ai.owner/v1",
            (
                _field("content", write=(CREATE,)),
                _field("parts", "json", write=(CREATE,)),
                _field("tool_calls", "json"),
                _field("create_time", "datetime"),
            ),
        ),
        _object(
            "ai.operation_log",
            "log_id",
            "ai.audit/v1",
            (
                *(
                    _field(n, query=(EQ, GROUP, AGGREGATE))
                    for n in (
                        "tool_name",
                        "agent_code",
                        "status",
                        "error_code",
                        "execution_mode",
                        "risk_level",
                    )
                ),
                _field("queued_at", "datetime", query=(RANGE, SORT)),
                _field("started_at", "datetime"),
                _field("finished_at", "datetime"),
                _field("duration_ms", "integer"),
                _field("hitl_wait_ms", "integer"),
                _field("is_security_event", "boolean", query=(EQ,)),
                _field("actor_name", sources=user_name),
                _field("target_summary", "json"),
            ),
        ),
        _object(
            "ai.routing_log",
            "log_id",
            "ai.audit/v1",
            (
                _field("final_agent", query=(EQ,)),
                _field("reason"),
                _field("latency_ms", "integer"),
                _field("create_time", "datetime", query=(RANGE,)),
            ),
        ),
        _object(
            "ai.routing_feedback",
            "feedback_id",
            "audit.tenant/v1",
            (
                *(
                    _field(n, query=(EQ, GROUP, AGGREGATE))
                    for n in ("feedback", "original_agent", "corrected_agent")
                ),
                _field("create_time", "datetime", query=(RANGE, SORT)),
                _field("user_name", sources=user_name),
            ),
        ),
    )
    index = {obj.object_id: obj for obj in objects}
    actions = list(base.actions.values())

    def add(action, selected, permission, accesses=None):
        actions.append(
            ActionSpec(
                action,
                selected,
                tuple(
                    FieldUse(FieldRef(obj, f.field_id), access)
                    for obj in selected
                    for f in index[obj].fields
                    for access in f.accesses
                    if accesses is None or access in accesses
                    if not action.startswith("ai.tool.")
                    or access.purpose in {"management.read", "management.query"}
                    if not (action.startswith("ai.tool.role.") and obj == "system.user")
                    or f.field_id == "roles"
                    and access in {READ, JOIN}
                ),
                (permission,),
            )
        )

    for action, selected, permission in (
        (
            "operation-list",
            ("system.operation_log", "system.user"),
            "monitor:operation-log:list",
        ),
        ("login-list", ("system.login_log", "system.user"), "monitor:login-log:list"),
        ("job-list", ("job.log", "job.definition"), "system:job-log:list"),
        ("agent-list", ("ai.agent",), "ai:chat:use"),
        (
            "operation-read",
            ("ai.operation_log", "system.user", "ai.agent"),
            "ai:chat:use",
        ),
        ("conversation-list", ("ai.conversation",), "ai:chat:use"),
        ("conversation-write", ("ai.conversation",), "ai:chat:use"),
        ("message-read", ("ai.message", "system.file", "system.user"), "ai:chat:use"),
        ("message-create", ("ai.message", "system.file", "system.user"), "ai:chat:use"),
        (
            "trace-list",
            ("ai.operation_log", "system.user", "ai.agent"),
            "ai:trace:view",
        ),
        (
            "feedback-list",
            ("ai.routing_feedback", "ai.routing_log", "system.user", "ai.agent"),
            "ai:routing-feedback:list",
        ),
    ):
        add("delivery." + action, selected, permission)
    add("ai.source-read", tuple(index), "ai:chat:use", {READ})
    for tool, selected in TOOL_OBJECTS.items():
        add("ai.tool." + tool, selected, "ai:chat:use")
    return Catalog(objects, tuple(actions))
