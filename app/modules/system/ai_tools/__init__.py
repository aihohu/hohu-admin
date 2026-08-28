"""System-domain AI tool adapters.

The package keeps a compatibility import surface while runtime registration
loads each leaf module explicitly through the shared built-in manifest.
"""

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "AiDepartmentId": "dept",
    "AiRoleId": "role",
    "AiRoleRelatedId": "role",
    "AiRoleRelatedIds": "role",
    "AiUserRoleId": "user_assignment",
    "AiUserRoleIds": "user_assignment",
    "AiUserDepartmentAssignment": "user_assignment",
    "_DEPT_LOOKUP_MAX_MATCHES": "dept",
    "_LIST_DEFAULT_LIMIT": "common",
    "_LIST_MAX_LIMIT": "common",
    "_ROLE_LOOKUP_MAX_MATCHES": "user_assignment",
    "_USER_IMPORT_FILE_POLICY": "user_transfer",
    "_USER_UPDATE_ALLOWED_FIELDS": "user_management",
    "_bound_confirmation_fields": "common",
    "_build_ai_user_create_schema": "user_management",
    "_build_scoped_dept_lookup_stmt": "dept",
    "_coerce_list_limit": "common",
    "_confirmation_display": "common",
    "_department_result": "dept",
    "_dry_run_dept_create": "dept",
    "_dry_run_dept_move": "dept",
    "_dry_run_dept_update": "dept",
    "_dry_run_role_create": "role",
    "_dry_run_role_update": "role",
    "_dry_run_role_update_agents": "role",
    "_dry_run_role_update_menus": "role",
    "_dry_run_user_batch_delete": "user_management",
    "_dry_run_user_create": "user_management",
    "_dry_run_user_export": "user_transfer",
    "_dry_run_user_reset_password": "user_management",
    "_dry_run_user_update": "user_management",
    "_dry_run_user_update_dept": "user_assignment",
    "_dry_run_user_update_roles": "user_assignment",
    "_format_ai_dept_assignments": "user_assignment",
    "_format_ai_roles": "user_assignment",
    "_get_ai_default_password": "user_management",
    "_load_ai_create_policy": "user_management",
    "_load_ai_reset_target": "user_management",
    "_load_ai_user_department_assignments": "user_management",
    "_load_file_bytes": "user_transfer",
    "_lookup_departments": "dept",
    "_parse_ai_dept_assignments": "user_assignment",
    "_parse_ai_role_ids": "user_assignment",
    "_require_department_snapshot": "dept",
    "_require_role_snapshot": "role",
    "_require_unique_role_related_ids": "role",
    "_resolve_users": "user_management",
    "_result_projection": "common",
    "_role_result": "role",
    "_user_import_mime_for_filename": "user_transfer",
    "_user_import_suffix_for_mime": "user_transfer",
    "dept_count": "analytics",
    "dept_create": "dept",
    "dept_list": "dept",
    "dept_lookup": "dept",
    "dept_move": "dept",
    "dept_service": "dept",
    "dept_update": "dept",
    "role_count": "analytics",
    "role_create": "role",
    "role_list": "role",
    "role_lookup": "role",
    "role_management_service": "role",
    "role_update": "role",
    "role_update_agents": "role",
    "role_update_menus": "role",
    "user_batch_delete": "user_management",
    "user_count": "analytics",
    "user_create": "user_management",
    "user_dept_lookup": "dept",
    "user_distinct": "analytics",
    "user_export": "user_transfer",
    "user_import_execute": "user_transfer",
    "user_import_preview": "user_transfer",
    "user_list": "user_management",
    "user_lookup": "user_management",
    "user_reset_password": "user_management",
    "user_role_lookup": "user_assignment",
    "user_stats": "analytics",
    "user_update": "user_management",
    "user_update_dept": "user_assignment",
    "user_update_roles": "user_assignment",
}

__all__ = tuple(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    """Resolve legacy package-level imports without eager tool registration."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the compatibility surface to introspection tools."""
    return sorted({*globals(), *_EXPORT_MODULES})
