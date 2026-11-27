"""Evaluate the user-list action once per role before compiling its SQL scope."""

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

import casbin
from sqlalchemy import and_, false, or_


@dataclass(frozen=True)
class RoleFacts:
    tenant: int
    enabled: bool
    super_admin: bool
    permissions: frozenset[str]


@lru_cache(maxsize=1)
def list_engine():
    # This model contains only the reviewed action. User/tenant facts are
    # request arguments and never stored on the shared enforcer.
    model = casbin.Model()
    model.load_model_from_text("""
[request_definition]
r = tenant, role, action
[policy_definition]
p = action, eft
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = r.action == p.action && r.tenant == r.role.tenant && r.role.enabled && (r.role.super_admin || r.action in r.role.permissions)
""")
    engine = casbin.Enforcer(model)
    engine.enable_auto_save(False)
    engine.add_policy("system:user:list", "allow")
    return engine


@lru_cache(maxsize=1)
def field_engine():
    model = casbin.Model()
    model.load_model_from_text("""
[request_definition]
r = tenant, owner, roles, allowed, denied
[policy_definition]
p = eft
[policy_effect]
e = some(where (p.eft == allow)) && !some(where (p.eft == deny))
[matchers]
m = r.tenant == r.owner && ((p.eft == "allow" && overlaps(r.roles, r.allowed)) || (p.eft == "deny" && overlaps(r.roles, r.denied)))
""")
    engine = casbin.Enforcer(model)
    engine.enable_auto_save(False)
    engine.add_function("overlaps", lambda left, right: not left.isdisjoint(right))
    engine.add_policy("allow")
    engine.add_policy("deny")
    return engine


class FieldRoles:
    """Both the point matcher and SQL consume the same tenant-bound grant sets."""

    def __init__(self, tenant_id, grants):
        self.tenant_id = tenant_id
        self.by_field = defaultdict(lambda: (set(), set()))
        for grant in grants:
            if grant.tenant_id == tenant_id:
                allow, deny = self.by_field[grant.ref, grant.access]
                (allow if grant.effect == "allow" else deny).add(grant.role_id)

    def permits(self, tenant_id, applicable_roles, ref, access):
        allow, deny = self.by_field.get((ref, access), ((), ()))
        return field_engine().enforce(
            tenant_id, self.tenant_id, applicable_roles, allow, deny
        )

    def coverage(self, scopes, ref, access):
        allow, deny = self.by_field.get((ref, access), ((), ()))

        def intersects(roles):
            return or_(
                false(),
                *(scopes[rid].is_(True) for rid in sorted(roles) if rid in scopes),
            )

        return and_(intersects(allow), ~intersects(deny))
