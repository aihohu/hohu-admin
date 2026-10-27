"""System seed reconciliation expressed as a complete, fixed-resource plan."""

from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select

from app.constants import SUPER_ADMIN_ROLE_CODE, USER_ROLE_CODE
from app.core.authorization_plan import ResourceKey, ResourceKind
from app.core.authorization_seed_plan import SeedChange, SeedPlan
from app.core.exceptions import BusinessException, BusinessRuleException
from app.core.id_generator import next_id
from app.modules.system.constants import PLATFORM_ONLY_TENANT_ROUTE_NAMES
from app.modules.system.hosted_menu_seed import HOSTED_MENU_DEFINITIONS
from app.modules.system.menu_seed import MENU_DEFINITIONS, menu_values
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)
from scripts.init_db import build_init_roles, fresh_role_permission_menus
from scripts.seed_settings import build_initial_settings
from scripts.sync_menus import _MENU_PARTITIONS


def _key(kind, tid, row):
    return ResourceKey(
        kind, tid, tuple(row[name] for name in resource_spec(kind).key_columns)
    )


def _menus(tid, before, bindings):
    rows = deepcopy(before)
    deletes = []
    if tid == 0:
        roots = {
            row["menu_id"]
            for row in rows
            if row["route_name"] in PLATFORM_ONLY_TENANT_ROUTE_NAMES
            or row["route_name"] == "home"
        }
        retired = set(roots)
        while True:
            children = {row["menu_id"] for row in rows if row["parent_id"] in retired}
            if children <= retired:
                break
            retired.update(children)
        for row in bindings:
            if row["menu_id"] in retired:
                deletes.append(
                    SeedChange("delete", _key(ResourceKind.ROLE_MENUS, tid, row), {})
                )
        pending = [row for row in rows if row["menu_id"] in retired]
        while pending:
            leaves = [
                row
                for row in pending
                if not any(child["parent_id"] == row["menu_id"] for child in pending)
            ]
            if not leaves:
                raise BusinessRuleException(
                    "菜单层级存在循环", error_code="AUTHORIZATION_SNAPSHOT_STALE"
                )
            deletes.extend(
                SeedChange("delete", _key(ResourceKind.MENU, tid, row), {})
                for row in leaves
            )
            pending = [row for row in pending if row not in leaves]
        had_home = any(row["route_name"] == "home" for row in rows)
        rows = [row for row in rows if row["menu_id"] not in retired]
        if had_home:
            for row in rows:
                if row["route_name"] == "ai_chat":
                    row.update(
                        parent_id=None,
                        menu_name="AI 助手",
                        icon="carbon:chat-bot",
                        component="layout.base$view.ai_chat",
                        layout="base",
                        order=0,
                    )
                if row["route_name"] == "ai":
                    row["menu_name"] = "AI 管理"
    definitions = MENU_DEFINITIONS if tid == 0 else HOSTED_MENU_DEFINITIONS
    by_route = {row["route_name"]: row for row in rows if row["route_name"]}
    by_permission = {row["permission"]: row for row in rows if row["permission"]}
    pending = []
    for definition in definitions:
        row = (
            by_permission.get(definition.get("permission"))
            if definition["menu_type"] == "F"
            else by_route.get(definition["route_name"])
        )
        if row is None:
            pending.append(definition)
        elif row["i18n_key"] is None and row["menu_name"] == definition["menu_name"]:
            row["i18n_key"] = menu_values(definition).get("i18n_key")
    inserted = []
    while pending:
        remaining = []
        for definition in pending:
            parent = definition["parent_route"]
            if parent != "0" and parent not in by_route:
                remaining.append(definition)
                continue
            row = dict(
                tenant_id=tid,
                menu_id=next_id(),
                parent_id=None if parent == "0" else by_route[parent]["menu_id"],
                **menu_values(definition),
            )
            row.setdefault("route_name", None)
            row.setdefault("permission", None)
            inserted.append(row)
            rows.append(row)
            if row["route_name"]:
                by_route[row["route_name"]] = row
            if row["permission"]:
                by_permission[row["permission"]] = row
        if len(remaining) == len(pending):
            raise BusinessRuleException(
                "菜单种子父项不可用", error_code="AUTHORIZATION_SNAPSHOT_STALE"
            )
        pending = remaining
    if tid == 0:
        for parent, children in _MENU_PARTITIONS.items():
            if parent in by_route:
                for child, order in children.items():
                    if child in by_route:
                        by_route[child].update(
                            parent_id=by_route[parent]["menu_id"], order=order
                        )
    changes = list(deletes)
    changes.extend(
        SeedChange("insert", _key(ResourceKind.MENU, tid, row), row) for row in inserted
    )
    old = {row["menu_id"]: row for row in before}
    for row in rows:
        if row["menu_id"] in old:
            difference = {
                name: value
                for name, value in row.items()
                if old[row["menu_id"]][name] != value
            }
            if difference:
                changes.append(
                    SeedChange("update", _key(ResourceKind.MENU, tid, row), difference)
                )
    return changes, rows


class SystemSeedCandidate:
    async def plan(self, db, *, action, password_hash, published_agents):
        tenants = (
            await db.execute(
                select(Tenant.tenant_id, Tenant.bootstrap_version)
                .order_by(Tenant.tenant_id)
                .limit(1025)
            )
        ).all()
        if len(tenants) > 1024 or not any(row.tenant_id == 0 for row in tenants):
            raise BusinessException(
                503, "迁移后的默认租户不可用", error_code="FIELD_POLICY_UNAVAILABLE"
            )
        existing, changes = [], []
        fresh = False
        for tenant in tenants:
            tid = tenant.tenant_id
            if tid != 0 and (action != "seed.system" or tenant.bootstrap_version == 0):
                continue
            facts = await system_authorization_candidate._facts(db, tid)
            existing.append(ResourceKey(ResourceKind.TENANT, None, (tid,)))
            for kind, rows in facts.items():
                existing.extend(_key(kind, tid, row) for row in rows)
            menus = facts[ResourceKind.MENU]
            if action == "seed.system":
                menu_changes, menus = _menus(tid, menus, facts[ResourceKind.ROLE_MENUS])
                changes.extend(menu_changes)
                for role in facts[ResourceKind.ROLE]:
                    if (
                        role["role_code"] not in {SUPER_ADMIN_ROLE_CODE, USER_ROLE_CODE}
                        or role["i18n_keys"] is not None
                    ):
                        continue
                    prefix = (
                        "builtin.role.user"
                        if role["role_code"] == USER_ROLE_CODE
                        else "builtin.role.systemAdmin"
                        if tid == 0
                        else "builtin.role.tenantAdmin"
                    )
                    default_name = (
                        "普通用户"
                        if role["role_code"] == USER_ROLE_CODE
                        else "系统超级管理员"
                        if tid == 0
                        else "租户管理员"
                    )
                    value = (
                        {"roleName": prefix + ".roleName"}
                        if role["role_name"] == default_name
                        else {}
                    )
                    changes.append(
                        SeedChange(
                            "update",
                            _key(ResourceKind.ROLE, tid, role),
                            {"i18n_keys": value},
                        )
                    )
            if tid != 0:
                continue
            fresh = action == "seed.system" and not facts[ResourceKind.USER]
            if fresh and password_hash is None:
                raise BusinessRuleException(
                    "首次安装需要管理员密码", error_code="ADMIN_PASSWORD_REQUIRED"
                )
            setting_keys = {row["setting_key"] for row in facts[ResourceKind.SETTING]}
            for row in build_initial_settings(tenant_id=tid, fresh=fresh):
                if row.setting_key not in setting_keys:
                    values = {
                        "tenant_id": tid,
                        "setting_key": row.setting_key,
                        "setting_value": row.setting_value,
                        "status": "1",
                    }
                    changes.append(
                        SeedChange(
                            "insert",
                            ResourceKey(ResourceKind.SETTING, tid, (row.setting_key,)),
                            values,
                        )
                    )
            if fresh:
                roles = build_init_roles()
                for role in roles:
                    values = {
                        name: value
                        for name, value in role.__dict__.items()
                        if not name.startswith("_")
                    }
                    values["role_id"] = next_id()
                    changes.append(
                        SeedChange(
                            "insert", _key(ResourceKind.ROLE, tid, values), values
                        )
                    )
                admin_role_id = changes[-2].key.identity[0]
                user_id = next_id()
                changes.append(
                    SeedChange(
                        "insert",
                        ResourceKey(ResourceKind.USER, tid, (user_id,)),
                        {
                            "tenant_id": tid,
                            "user_id": user_id,
                            "user_name": "admin",
                            "nickname": "R_ADMIN",
                            "hashed_password": password_hash,
                            "status": "1",
                        },
                    )
                )
                changes.append(
                    SeedChange(
                        "insert",
                        ResourceKey(
                            ResourceKind.USER_ROLES, tid, (user_id, admin_role_id)
                        ),
                        {
                            "tenant_id": tid,
                            "user_id": user_id,
                            "role_id": admin_role_id,
                        },
                    )
                )
                for menu in fresh_role_permission_menus(
                    [SimpleNamespace(**row) for row in menus]
                ):
                    changes.append(
                        SeedChange(
                            "insert",
                            ResourceKey(
                                ResourceKind.ROLE_MENUS,
                                tid,
                                (admin_role_id, menu.menu_id),
                            ),
                            {
                                "tenant_id": tid,
                                "role_id": admin_role_id,
                                "menu_id": menu.menu_id,
                            },
                        )
                    )
                changes.extend(
                    SeedChange(
                        "insert",
                        ResourceKey(
                            ResourceKind.ROLE_AGENT, tid, (admin_role_id, agent_id)
                        ),
                        {
                            "tenant_id": tid,
                            "role_id": admin_role_id,
                            "agent_id": agent_id,
                            "enabled": True,
                        },
                    )
                    for agent_id in published_agents
                )
        return SeedPlan(tuple(set(existing)), tuple(changes)), fresh

    async def apply(self, tx, plan):
        for change in plan.changes:
            if change.action == "insert":
                await tx.insert_reserved(change.key, change.values)
            elif change.action == "delete":
                await tx.delete_held(change.key)
            else:
                await tx.update_held(change.key, change.values)


system_seed_candidate = SystemSeedCandidate()
