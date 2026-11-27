"""Reviewed field-purpose mapping for the role editor and its immutable revisions."""

from app.core.field_policy import Access, FieldRef, Grant, compile_rules
from app.core.field_policy.contracts import primitive, require
from app.core.field_policy.documents import load_rules
from app.modules.system.field_policy.list_admission import FieldRoles

READ = Access("read", "management.read")
FILTER = Access("contains", "management.query", "filter")
EQ = Access("eq", "management.query", "filter")
JOIN = Access("eq", "management.query", "join")
SORT = Access("sort", "management.query", "sort")
CREATE = Access("create", "management.create")
UPDATE = Access("update", "management.update")
PROFILE = ("user_name", "nickname", "user_gender", "status", "user_phone", "user_email")

# References are to the actual public list projection. Structural user_id is fixed.
USES = {
    "read": dict.fromkeys(
        (
            "user_name",
            "nickname",
            "user_gender",
            "status",
            "create_time",
            "user_phone",
            "user_email",
            "roles",
            "departments",
        ),
        (READ,),
    ),
    "query": {
        "user_name": (FILTER,),
        "nickname": (FILTER,),
        "user_phone": (FILTER, SORT),
        "user_email": (FILTER,),
        "user_gender": (EQ,),
        "status": (EQ,),
        "roles": (JOIN,),
        "departments": (JOIN,),
    },
    "create": dict.fromkeys((*PROFILE, "password"), (CREATE,)),
    "update": dict.fromkeys(PROFILE, (UPDATE,)),
}
SENSITIVE = frozenset({"user_phone", "user_email", "roles", "departments", "password"})
DEFAULT = {"mode": "restrict_selected", **{purpose: [] for purpose in USES}}


def selected_grants(tenant_id, role_id, policy):
    require(
        type(policy) is dict
        and {"mode", "read", "query"} <= policy.keys() <= DEFAULT.keys() | {"sensitive"}
    )
    mode = policy["mode"]
    require(mode in ("allow_selected", "restrict_selected"))
    positive = policy.get("sensitive", {})
    require(type(positive) is dict and positive.keys() <= policy.keys() & USES.keys())
    result = []
    for purpose, fields in USES.items():
        if purpose not in policy:
            continue  # Older revisions keep their explicit rules for this purpose.
        names = policy[purpose]
        require(
            type(names) is list and all(type(n) is str and n in fields for n in names)
        )
        require(names == sorted(set(names)))
        explicit = positive.get(purpose, [])
        require(
            type(explicit) is list
            and all(type(n) is str and n in SENSITIVE and n in fields for n in explicit)
        )
        require(
            explicit == sorted(set(explicit)) and not set(explicit).intersection(names)
        )
        require(not explicit or mode == "restrict_selected")
        for name, accesses in fields.items():
            selected = name in names
            if mode == "allow_selected" and not selected:
                continue
            if (
                mode == "restrict_selected"
                and not selected
                and name in SENSITIVE
                and name not in explicit
            ):
                continue
            effect = "deny" if mode == "restrict_selected" and selected else "allow"
            for access in accesses:
                level = (
                    "masked"
                    if access == READ
                    and name in {"user_phone", "user_email"}
                    and effect == "allow"
                    else "plain"
                )
                result.append(
                    Grant(
                        tenant_id,
                        str(role_id),
                        FieldRef("system.user", name),
                        access,
                        effect,
                        level,
                    )
                )
    return tuple(result)


def editor_defaults(document, active_grants, tenant_id, role_id):
    grants = active_grants or ()
    current = FieldRoles(tenant_id, grants)
    copy_fields = {
        purpose: sorted(
            name
            for name, accesses in fields.items()
            if all(
                current.permits(
                    tenant_id, {str(role_id)}, FieldRef("system.user", name), a
                )
                for a in accesses
            )
        )
        for purpose, fields in USES.items()
    }
    owned = {
        (name, a)
        for fields in USES.values()
        for name, accesses in fields.items()
        for a in accesses
    }
    policy = document["list_policy"] if isinstance(document, dict) else {}
    raw = document["rules"] if isinstance(document, dict) else document
    managed = {
        (name, a)
        for purpose in {"read", "query"} | (policy.keys() & USES.keys())
        for name, accesses in USES[purpose].items()
        for a in accesses
    }
    remainder = [
        rule
        for rule in raw
        if (rule["field_id"], Access(**rule["access"])) not in managed
    ]
    return {
        "list_fields": {p: sorted(fs) for p, fs in USES.items()},
        "sensitive_fields": sorted(SENSITIVE),
        "list_accesses": [
            {"field_id": name, "access": primitive(a)} for name, a in sorted(owned)
        ],
        "copy_fields": copy_fields if active_grants is not None else None,
        "list_remainder": remainder,
    }


def load_policy_rules(catalog, document, *, tenant_id, role_id, object_id):
    if type(document) is list:
        return load_rules(
            catalog, document, tenant_id=tenant_id, role_id=role_id, object_id=object_id
        )
    require(
        object_id == "system.user"
        and type(document) is dict
        and set(document) == {"list_policy", "rules"}
    )
    policy = document["list_policy"]
    grants = selected_grants(tenant_id, role_id, policy)
    others = load_rules(
        catalog,
        document["rules"],
        tenant_id=tenant_id,
        role_id=role_id,
        object_id=object_id,
    )
    require(
        all(
            grant.access not in fields.get(grant.ref.field_id, ())
            for grant in others
            for purpose, fields in USES.items()
            if purpose in policy
        )
    )
    result = grants + others
    compile_rules(catalog, result)
    return result
