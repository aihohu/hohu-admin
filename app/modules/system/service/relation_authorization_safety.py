"""Pure before/after checks over the owner's complete tenant facts."""

from collections import defaultdict
from copy import deepcopy

from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException, BusinessException

REPAIR_CODES = frozenset(
    f"system:role:field-policy-{a}" for a in ("view", "edit", "activate")
)
REGISTRATION_FIELDS = frozenset(
    {
        "parent_id",
        "menu_type",
        "status",
        "permission",
        "constant",
        "path",
        "component",
        "route_name",
        "route_path",
        "path_param",
        "page",
        "layout",
        "href",
        "query",
        "active_menu",
    }
)


def manager_accounts(facts):
    roles = {
        r["role_id"]
        for r in facts[K.ROLE]
        if r["status"] == "1" and r["role_code"] == "R_SUPER"
    }
    members = {r["user_id"] for r in facts[K.USER_ROLES] if r["role_id"] in roles}
    return {
        u["user_id"]
        for u in facts[K.USER]
        if u["status"] == "1" and u["user_id"] in members
    }


def repair_accounts(facts):
    menus = {m["menu_id"]: m for m in facts[K.MENU]}
    user_roles, role_menus = defaultdict(set), defaultdict(set)
    enabled = {r["role_id"] for r in facts[K.ROLE] if r["status"] == "1"}
    for row in facts[K.USER_ROLES]:
        user_roles[row["user_id"]].add(row["role_id"])
    for row in facts[K.ROLE_MENUS]:
        role_menus[row["role_id"]].add(row["menu_id"])
    cache, visits = {}, 0
    eligible = set()
    for uid in manager_accounts(facts):
        roles = frozenset(user_roles[uid] & enabled)
        if roles in cache:
            if cache[roles]:
                eligible.add(uid)
            continue
        cache[roles] = False
        bound = set().union(*(role_menus[r] for r in roles))
        # R_SUPER navigation uses all enabled tenant routes; field management
        # independently requires explicit bound codes. F-menu status does not
        # revoke those codes (collect_user_permission_codes owns that contract).
        available = set(menus)
        visits += len(available) + len(bound)
        if visits > 100000:
            raise BusinessException(
                422,
                "修复入口检查超过预算",
                error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
            )
        codes = {m.get("permission") for key, m in menus.items() if key in bound}
        if not REPAIR_CODES <= codes:
            continue
        for key in available:
            node = menus.get(key)
            if (
                not node
                or node.get("route_name") != "system_role"
                or node.get("component") != "view.system_role"
                or not node.get("route_path")
                or node.get("href")
            ):
                continue
            seen, current = set(), key
            while current not in (None, 0):
                visits += 1
                if visits > 100000:
                    raise BusinessException(
                        422,
                        "修复入口检查超过预算",
                        error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
                    )
                node = menus.get(current)
                if (
                    current in seen
                    or current not in available
                    or not node
                    or node["status"] != "1"
                    or node.get("hide_in_menu")
                    or node["menu_type"] not in {"M", "C"}
                    or not node.get("route_name")
                    or not node.get("route_path")
                    or node.get("href")
                    or (
                        current != key
                        and node.get("component") not in {"layout.base", "layout.blank"}
                    )
                ):
                    break
                seen.add(current)
                current = node["parent_id"]
            else:
                eligible.add(uid)
                cache[roles] = True
                break
    return eligible


def require_manager_preserved(before, after):
    before_repair, after_repair = repair_accounts(before), repair_accounts(after)
    established = any(m.get("route_name") == "system_role" for m in before[K.MENU])
    if (manager_accounts(before) and not manager_accounts(after)) or (
        (before_repair or established) and not after_repair
    ):
        raise BusinessException(
            409,
            "至少保留一个可用的字段权限管理入口",
            error_code="AUTHORIZATION_LAST_MANAGER_REQUIRED",
        )


def system_after(facts, operation, payload, target_id, _new_id, prepared):
    result = deepcopy(facts)
    group, action = operation.split(".", 1)
    kind, pk = {
        "user": (K.USER, "user_id"),
        "role": (K.ROLE, "role_id"),
        "dept": (K.DEPT, "dept_id"),
    }[group]
    if action == "update":
        for row in result[kind]:
            if row[pk] == target_id:
                row.update(payload.model_dump(exclude_unset=True, exclude={"dept_ids"}))
    elif action in {"delete", "batch_delete"}:
        ids = set(payload) if action == "batch_delete" else {target_id}
        result[kind] = [r for r in result[kind] if r[pk] not in ids]
    elif operation == "user.roles":
        result[K.USER_ROLES] = [
            r for r in result[K.USER_ROLES] if r["user_id"] != target_id
        ] + [{"user_id": target_id, "role_id": rid} for rid in prepared["roles"]]
    elif operation == "role.menus":
        result[K.ROLE_MENUS] = [
            r for r in result[K.ROLE_MENUS] if r["role_id"] != target_id
        ] + [
            {"role_id": target_id, "menu_id": int(m.menu_id)}
            for m in prepared["evaluation"].candidate_menus
        ]
    return result


def menu_after(facts, changes):
    result = deepcopy(facts)
    for change in changes:
        kind = change.key.kind
        if kind not in {K.MENU, K.ROLE_MENUS}:
            continue
        names = ("menu_id",) if kind == K.MENU else ("role_id", "menu_id")
        identity = dict(zip(names, change.key.identity, strict=True))
        rows = result[kind]
        if change.action == "insert":
            rows.append(identity | change.values)
        elif change.action == "delete":
            result[kind] = [
                r for r in rows if not all(r[n] == v for n, v in identity.items())
            ]
        else:
            for row in rows:
                if all(row[n] == v for n, v in identity.items()):
                    row.update(change.values)
    return result


def validate_menu_delegation(before, _after, changes, authority):
    if authority.super_admin:
        return
    menus = {m["menu_id"]: m for m in before[K.MENU]}
    affected = set()
    for change in changes:
        if change.key.kind != K.MENU:
            continue
        key = change.key.identity[0]
        old = menus.get(key, {})
        special = change.action != "update" or bool(
            REGISTRATION_FIELDS & change.values.keys()
        )
        if not special:
            continue
        affected.add(key)
        parent_id = change.values.get("parent_id")
        if parent_id is not None and parent_id not in authority.menu_ids:
            raise AuthorizationException(
                "目标父菜单超出可委派范围", error_code="AUTHORIZATION_DELEGATION_DENIED"
            )
        permission = (change.values or {}).get("permission")
        if (
            old.get("permission")
            and old["permission"] not in authority.permission_codes
        ) or (permission and permission not in authority.permission_codes):
            raise AuthorizationException(
                "菜单注册超出可委派权限", error_code="AUTHORIZATION_DELEGATION_DENIED"
            )
    children = defaultdict(set)
    for row in before[K.MENU]:
        children[row["parent_id"]].add(row["menu_id"])
    pending = set(affected)
    while pending:
        found = set().union(*(children[k] for k in pending)) - affected
        affected.update(found)
        pending = found
    for key in affected:
        permission = menus.get(key, {}).get("permission")
        if permission and permission not in authority.permission_codes:
            raise AuthorizationException(
                "菜单子树超出可委派权限", error_code="AUTHORIZATION_DELEGATION_DENIED"
            )
    role_ids = {r["role_id"] for r in before[K.ROLE_MENUS] if r["menu_id"] in affected}
    protected = {r["role_id"] for r in before[K.ROLE] if r["role_code"] == "R_SUPER"}
    members = {r["user_id"] for r in before[K.USER_ROLES] if r["role_id"] in role_ids}
    if (
        role_ids & (protected | set(authority.enabled_role_ids))
        or (
            authority.accessible_user_scope is not None
            and not members <= authority.accessible_user_scope
        )
        or not affected & menus.keys() <= authority.menu_ids
    ):
        raise AuthorizationException(
            "菜单影响超出可委派范围", error_code="AUTHORIZATION_DELEGATION_DENIED"
        )
