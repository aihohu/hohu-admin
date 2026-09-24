"""Immutable hosted menu manifest used by tenant bootstrap.

The platform control plane must never copy mutable rows from Default Tenant:
that would let a tenant-local menu editor influence a future tenant's routes.
"""

from dataclasses import dataclass

from app.modules.system.menu_seed import (
    MENU_DEFINITIONS,
    build_initial_menus,
    menu_values,
)
from app.modules.system.models.menu import Menu


@dataclass(frozen=True, slots=True)
class HostedMenuBlueprint:
    key: str
    parent_key: str | None
    menu_name: str
    menu_type: str
    route_name: str | None = None
    route_path: str | None = None
    component: str | None = None
    page: str | None = None
    layout: str | None = None
    i18n_key: str | None = None
    icon: str | None = None
    icon_type: str | None = None
    order: int = 0
    hide_in_menu: bool | None = None
    keep_alive: bool | None = None
    constant: bool | None = None
    multi_tab: bool | None = None
    permission: str | None = None


HOSTED_ROUTE_NAMES = frozenset(
    (
        "ai_chat",
        "ai",
        "auth",
        "system",
        "task",
        "dashboard",
        "ai_trace",
        "system_dept",
        "system_user",
        "system_role",
        "system_menu",
        "system_dict",
        "system_dict_data",
        "system_file",
        "system_monitor",
        "system_job",
        "system_job-log",
        "system_operation-log",
        "system_login-log",
    )
)
HOSTED_BUTTON_PERMISSIONS = frozenset(
    (
        "ai:chat:use",
        "ai:file:parse",
        "system:dept:list",
        "system:dept:add",
        "system:dept:edit",
        "system:dept:move",
        "system:dept:delete",
        "system:dept:batch-delete",
        "system:user:list",
        "system:user:add",
        "system:user:edit",
        "system:user:delete",
        "system:user:reset-password",
        "system:user:import",
        "system:user:export",
        "system:user:role-auth",
        "system:role:list",
        "system:role:add",
        "system:role:edit",
        "system:role:menu-auth",
        "system:role:ai-agent-auth",
        "system:role:delete",
        "system:role:batch-delete",
        "system:file:list",
        "system:file:upload",
        "system:file:delete",
        "system:job:edit",
        "monitor:operation-log:list",
        "monitor:login-log:list",
    )
)

HOSTED_MENU_DEFINITIONS = tuple(
    d
    for d in MENU_DEFINITIONS
    if (d["menu_type"] != "F" and d["route_name"] in HOSTED_ROUTE_NAMES)
    or (d["menu_type"] == "F" and d.get("permission") in HOSTED_BUTTON_PERMISSIONS)
)
HOSTED_MENU_BLUEPRINTS = tuple(
    HostedMenuBlueprint(
        key=d["route_name"]
        if d["menu_type"] != "F"
        else f"permission:{d['permission']}",
        parent_key=None if d["parent_route"] == "0" else d["parent_route"],
        **{k: v for k, v in menu_values(d).items() if k != "status"},
    )
    for d in HOSTED_MENU_DEFINITIONS
)
HOSTED_PERMISSION_CODES = frozenset(
    b.permission for b in HOSTED_MENU_BLUEPRINTS if b.permission
)


def build_hosted_tenant_menus(tenant_id: int) -> list[Menu]:
    return build_initial_menus(tenant_id=tenant_id, definitions=HOSTED_MENU_DEFINITIONS)
