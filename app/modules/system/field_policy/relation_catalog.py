"""Candidate declarations; registration does not qualify any runtime adapter."""

from dataclasses import dataclass, replace
from types import MappingProxyType

from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
    RecordContext,
    SourceNode,
    SourceRef,
    collect_sources,
    project,
    resolve_fields,
)
from app.core.field_policy.contracts import identity, require

READ = Access("read", "management.read")
CREATE = Access("create", "management.create")
UPDATE = Access("update", "management.update")
CONTAINS = Access("contains", "management.query", "filter")
EXACT = Access("exact", "management.query", "filter")
EQ = Access("eq", "management.query", "filter")
JOIN = Access("eq", "management.query", "join")
SORT = Access("sort", "management.query", "sort")

# output -> (owner relationship field, actual source object, required source fields).
# These are per-cell contracts, not independent editable policy fields.
RELATION_OUTPUTS = MappingProxyType(
    {
        "role_names": ("roles", "system.role", ("role_name",)),
        "role_name_keys": ("roles", "system.role", ("role_name", "i18n_keys")),
        "role_codes": ("roles", "system.role", ("role_code",)),
        "dept_names": ("departments", "system.dept", ("dept_name",)),
    }
)


def field(
    name,
    kind="string",
    *,
    ordinary=False,
    create=False,
    nullable=False,
    queries=(),
    mask=None,
    sources=(),
):
    return FieldSpec(
        name,
        kind,
        (
            READ,
            *((CREATE, UPDATE) if ordinary else ((CREATE,) if create else ())),
            *queries,
        ),
        nullable=nullable,
        mask_id=mask,
        sources=sources,
    )


def object_spec(name, anchor, fields):
    return ObjectSpec(
        f"system.{name}",
        "tenant",
        f"{name}.scope/v1",
        (FieldSpec(anchor, "id", (READ,), structural=True), *fields),
    )


def build_relation_catalog(base):
    role = object_spec(
        "role",
        "role_id",
        (
            field("role_name", ordinary=True, queries=(CONTAINS,)),
            field("role_desc", ordinary=True, nullable=True),
            field("role_code", create=True, queries=(CONTAINS, EXACT, SORT)),
            field(
                "i18n_keys",
                "json",
                nullable=True,
                sources=(FieldRef("system.role", "role_name"),),
            ),
            field("status", queries=(EQ,)),
            field("data_scope", queries=(EQ,)),
            *(field(n, "json") for n in ("menu_ids", "dept_ids", "agent_ids")),
            field("create_time", "datetime"),
            field("update_time", "datetime", nullable=True),
        ),
    )
    dept = object_spec(
        "dept",
        "dept_id",
        (
            field("dept_name", ordinary=True, queries=(CONTAINS,)),
            field("order_num", "integer", ordinary=True, queries=(SORT,)),
            field("phone", ordinary=True, nullable=True, mask="phone/v1"),
            field("email", ordinary=True, nullable=True, mask="email/v1"),
            field(
                "leader",
                ordinary=True,
                nullable=True,
                queries=(CONTAINS,),
                sources=(
                    FieldRef("system.user", "user_name"),
                    FieldRef("system.user", "nickname"),
                ),
            ),
            field("status", ordinary=True, queries=(EQ,)),
            field("parent_id", "id", nullable=True),
            field("ancestors", nullable=True),
            field("create_time", "datetime"),
            field("update_time", "datetime", nullable=True),
        ),
    )
    menu = object_spec(
        "menu",
        "menu_id",
        (
            field("menu_name", ordinary=True, queries=(CONTAINS,)),
            field(
                "i18n_key",
                ordinary=True,
                nullable=True,
                sources=(FieldRef("system.menu", "menu_name"),),
            ),
            field("icon", ordinary=True, nullable=True),
            field("icon_type", ordinary=True, nullable=True),
            field("order", "integer", ordinary=True, queries=(SORT,)),
            *(
                field(n, "boolean", ordinary=True, nullable=True)
                for n in ("hide_in_menu", "keep_alive", "multi_tab")
            ),
            field("fixed_index_in_tab", "integer", ordinary=True, nullable=True),
            field("parent_id", "id", nullable=True),
            field("menu_type", queries=(EQ,)),
            field("status", queries=(EQ,)),
            field("permission", nullable=True),
            field("constant", "boolean", nullable=True),
            field("route_name", nullable=True, queries=(EXACT,)),
            *(
                field(n, nullable=True)
                for n in (
                    "path",
                    "component",
                    "route_path",
                    "path_param",
                    "page",
                    "layout",
                    "href",
                    "active_menu",
                )
            ),
            field("query", "json", nullable=True),
            field("create_time", "datetime"),
            field("update_time", "datetime", nullable=True),
        ),
    )
    user = base.objects["system.user"]
    derived = tuple(
        FieldSpec(
            output,
            "json",
            (),
            sources=(
                FieldRef("system.user", root),
                *(FieldRef(source, name) for name in names),
            ),
        )
        for output, (root, source, names) in RELATION_OUTPUTS.items()
    )
    objects = tuple(
        replace(user, fields=user.fields + derived)
        if o.object_id == user.object_id
        else o
        for o in base.objects.values()
    ) + (role, dept, menu)
    actions = list(base.actions.values())
    for obj in (role, dept, menu):
        name = obj.object_id.removeprefix("system.")
        for operation, permission, accesses in (
            ("list", "list", {READ, CONTAINS, EXACT, EQ, JOIN, SORT}),
            ("create", "add", {READ, CREATE}),
            ("update", "edit", {READ, UPDATE}),
        ):
            actions.append(
                ActionSpec(
                    f"{name}.{operation}",
                    (obj.object_id, "system.user")
                    if name == "dept" and operation != "list"
                    else (obj.object_id,),
                    tuple(
                        FieldUse(FieldRef(obj.object_id, f.field_id), a)
                        for f in obj.fields
                        for a in f.accesses
                        if a in accesses
                    )
                    + (
                        tuple(
                            FieldUse(FieldRef("system.user", f), a)
                            for f in ("user_name", "nickname")
                            for a in (EXACT, READ)
                        )
                        if name == "dept" and operation != "list"
                        else ()
                    ),
                    (f"system:{name}:{permission}",)
                    + (
                        ("system:dept:list",)
                        if name == "dept" and operation != "list"
                        else ()
                    ),
                )
            )
    uses = {
        FieldUse(FieldRef("system.user", root), READ)
        for root, _, _ in RELATION_OUTPUTS.values()
    }
    uses.update(
        FieldUse(FieldRef(source, name), READ)
        for _, source, names in RELATION_OUTPUTS.values()
        for name in names
    )
    actions.append(
        ActionSpec(
            "user.relations",
            ("system.user", "system.role", "system.dept"),
            tuple(sorted(uses)),
            ("system:user:list",),
        )
    )
    # Dedicated owner protocols still enforce delegation, complete lock plans and
    # lifecycle rules. Declaring an action supplies no ordinary write grant.
    for action_id, object_ids, permissions in (
        ("role.definition", ("system.role", "system.dept"), ("system:role:edit",)),
        ("role.menus", ("system.role", "system.menu"), ("system:role:menu-auth",)),
        ("role.agents", ("system.role",), ("system:role:ai-agent-auth",)),
        ("dept.move", ("system.dept",), ("system:dept:move", "system:dept:list")),
        (
            "dept.members",
            ("system.dept", "system.user"),
            ("system:dept:list", "system:dept:edit", "system:user:edit"),
        ),
        ("menu.registration", ("system.menu",), ("system:menu:edit",)),
    ):
        actions.append(
            ActionSpec(action_id, object_ids, (), permissions, kind="special")
        )
    user = next(o for o in objects if o.object_id == "system.user")
    for action, permissions in {
        "user.create": ("system:user:add",),
        "user.update": ("system:user:edit",),
        "user.password": ("system:user:reset-password",),
        "user.roles": ("system:user:edit", "system:user:role-auth"),
        "user.departments": ("system:user:edit", "system:dept:list"),
        "dept.members-write": (
            "system:dept:list",
            "system:dept:edit",
            "system:user:edit",
        ),
    }.items():
        accesses = {
            "user.create": {CREATE, Access("replace", "management.relations")},
            "user.update": {UPDATE},
            "user.password": {UPDATE},
        }.get(action, {Access("replace", "management.relations")})
        restricted = {
            "user.password": {"password"},
            "user.roles": {"roles"},
            "user.departments": {"departments"},
            "dept.members-write": {"departments"},
        }.get(action)
        actions.append(
            ActionSpec(
                action,
                ("system.user",),
                tuple(
                    FieldUse(FieldRef(user.object_id, f.field_id), a)
                    for f in user.fields
                    for a in f.accesses
                    if a in accesses
                    and (restricted is None or f.field_id in restricted)
                ),
                permissions,
            )
        )
    return Catalog(objects, tuple(actions))


@dataclass(frozen=True)
class RelationCell:
    visible: bool = False
    value: object = None
    sources: tuple[SourceRef, ...] = ()


def project_relation_cell(catalog, rules, *, owner, source, target_id, output, values):
    """Trusted owner supplies an observed relationship and two independently scoped records.

    This pure helper does not establish row visibility or fetch relations. Call only
    under the owner's live barrier; source evidence never grants execution rights.
    """
    require(type(output) is str and output in RELATION_OUTPUTS)
    require(type(owner) is RecordContext and type(source) is RecordContext)
    root, source_object, names = RELATION_OUTPUTS[output]
    identity(target_id)
    identity(owner.record_key)
    require(
        owner.object_id == "system.user"
        and source.object_id == source_object
        and owner.tenant_id == source.tenant_id
        and source.record_key == target_id
    )
    action = "user.relations"
    owner_decisions = resolve_fields(catalog, rules, owner, action, READ)
    source_decisions = resolve_fields(catalog, rules, source, action, READ)
    if not owner_decisions[root].allowed or any(
        not source_decisions[name].allowed for name in names
    ):
        return RelationCell()
    require(type(values) in (dict, MappingProxyType))
    require(all(name in values for name in names))
    projected = project(
        catalog, rules, source, action, READ, {name: values[name] for name in names}
    )
    version = "user-relations/v1"
    refs = (
        SourceRef(
            "tenant",
            owner.tenant_id,
            owner.record_key,
            FieldRef(owner.object_id, root),
            READ,
            catalog.digest,
            version,
        ),
    ) + tuple(
        SourceRef(
            "tenant",
            source.tenant_id,
            source.record_key,
            FieldRef(source.object_id, name),
            READ,
            catalog.digest,
            version,
        )
        for name in names
    )
    evidence = collect_sources(
        catalog,
        (SourceNode("cell", refs, ()),),
        ("cell",),
        tenant_id=owner.tenant_id,
        projection_version=version,
    )
    return RelationCell(True, projected[names[-1]], evidence)
