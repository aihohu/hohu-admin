"""Machine-readable ownership inventory for the tenant isolation rollout."""

from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import Any

from sqlalchemy import Table


class TenantNullability(StrEnum):
    REQUIRED = "required"
    AUDIT_OPTIONAL = "audit_optional"


@dataclass(frozen=True, slots=True)
class TenantResource:
    table_name: str
    object_path: str
    nullability: TenantNullability = TenantNullability.REQUIRED
    unique_keys: tuple[tuple[str, ...], ...] = ()
    relationship_keys: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformGlobalResource:
    table_name: str
    object_path: str
    identity_column: str | None = None


def _resource(
    table_name: str,
    object_path: str,
    *,
    nullability: TenantNullability = TenantNullability.REQUIRED,
    unique_keys: tuple[tuple[str, ...], ...] = (),
    relationship_keys: tuple[tuple[str, ...], ...] = (),
) -> TenantResource:
    return TenantResource(
        table_name=table_name,
        object_path=object_path,
        nullability=nullability,
        unique_keys=unique_keys,
        relationship_keys=relationship_keys,
    )


TENANT_MODEL_INVENTORY: dict[str, TenantResource] = {
    "ai_conversation": _resource(
        "ai_conversation",
        "app.modules.ai.models.conversation:AiConversation",
        relationship_keys=(("tenant_id", "user_id"),),
    ),
    "ai_message": _resource(
        "ai_message",
        "app.modules.ai.models.message:AiMessage",
        relationship_keys=(("tenant_id", "conversation_id"),),
    ),
    "ai_prepared_action": _resource(
        "ai_prepared_action",
        "app.modules.ai.models.prepared_action:AiPreparedAction",
        unique_keys=(
            ("tenant_id", "confirmation_id"),
            ("tenant_id", "execute_tool_call_id"),
        ),
    ),
    "ai_operation_log": _resource(
        "ai_operation_log",
        "app.modules.ai.models.operation_log:AiOperationLog",
        unique_keys=(("tenant_id", "tool_call_id"),),
    ),
    "ai_routing_log": _resource(
        "ai_routing_log", "app.modules.ai.models.routing_log:AiRoutingLog"
    ),
    "ai_routing_feedback": _resource(
        "ai_routing_feedback",
        "app.modules.ai.models.routing_feedback:AiRoutingFeedback",
        relationship_keys=(("tenant_id", "message_id"),),
    ),
    "tenant_ai_model_policy": _resource(
        "tenant_ai_model_policy",
        "app.modules.ai.models.model_policy:TenantAiModelPolicy",
        unique_keys=(("tenant_id", "model_id"),),
    ),
    "sys_user": _resource(
        "sys_user",
        "app.modules.system.models.user:User",
        unique_keys=(("tenant_id", "user_name"), ("tenant_id", "employee_no")),
    ),
    "sys_role": _resource(
        "sys_role",
        "app.modules.system.models.role:Role",
        unique_keys=(("tenant_id", "role_code"), ("tenant_id", "role_name")),
    ),
    "sys_dept": _resource(
        "sys_dept",
        "app.modules.system.models.dept:Dept",
        relationship_keys=(("tenant_id", "parent_id"),),
    ),
    "sys_menu": _resource(
        "sys_menu",
        "app.modules.system.models.menu:Menu",
        relationship_keys=(("tenant_id", "parent_id"),),
    ),
    "sys_user_role": _resource(
        "sys_user_role",
        "app.db.base:user_roles",
        relationship_keys=(("tenant_id", "user_id"), ("tenant_id", "role_id")),
    ),
    "sys_user_dept": _resource(
        "sys_user_dept",
        "app.db.base:user_depts",
        relationship_keys=(("tenant_id", "user_id"), ("tenant_id", "dept_id")),
    ),
    "sys_role_menu": _resource(
        "sys_role_menu",
        "app.db.base:role_menus",
        relationship_keys=(("tenant_id", "role_id"), ("tenant_id", "menu_id")),
    ),
    "sys_role_dept": _resource(
        "sys_role_dept",
        "app.db.base:role_depts",
        relationship_keys=(("tenant_id", "role_id"), ("tenant_id", "dept_id")),
    ),
    "role_ai_agent": _resource(
        "role_ai_agent",
        "app.modules.ai.models.role_ai_agent:RoleAiAgent",
        relationship_keys=(("tenant_id", "role_id"),),
    ),
    "sys_config": _resource(
        "sys_config",
        "app.modules.system.models.config:Config",
        unique_keys=(("tenant_id", "config_key"),),
    ),
    "sys_dict_type": _resource(
        "sys_dict_type",
        "app.modules.system.models.dict_type:DictType",
        unique_keys=(("tenant_id", "dict_type"), ("tenant_id", "dict_name")),
    ),
    "sys_dict_data": _resource(
        "sys_dict_data",
        "app.modules.system.models.dict_data:DictData",
        relationship_keys=(("tenant_id", "dict_type"),),
    ),
    "sys_file": _resource(
        "sys_file",
        "app.modules.system.models.file:File",
        relationship_keys=(("tenant_id", "owner_user_id"),),
    ),
    "sys_data_scope_demo": _resource(
        "sys_data_scope_demo",
        "app.modules.system.models.data_scope_demo:DataScopeDemo",
        relationship_keys=(("tenant_id", "dept_id"), ("tenant_id", "create_by")),
    ),
    "sys_user_import_batch": _resource(
        "sys_user_import_batch",
        "app.modules.system.models.user_transfer:UserImportBatch",
        unique_keys=(("tenant_id", "preview_token"),),
        relationship_keys=(("tenant_id", "operator_id"),),
    ),
    "sys_user_import_batch_log": _resource(
        "sys_user_import_batch_log",
        "app.modules.system.models.user_transfer:UserImportBatchLog",
        relationship_keys=(("tenant_id", "batch_id"), ("tenant_id", "operator_id")),
    ),
    "sys_user_export_task": _resource(
        "sys_user_export_task",
        "app.modules.system.models.user_transfer:UserExportTask",
        relationship_keys=(("tenant_id", "operator_id"),),
    ),
    "sys_job": _resource(
        "sys_job",
        "app.modules.job.models.job:SysJob",
        unique_keys=(("tenant_id", "job_key"),),
    ),
    "sys_job_log": _resource(
        "sys_job_log",
        "app.modules.job.models.job:SysJobLog",
        relationship_keys=(("tenant_id", "job_id"),),
    ),
    "sys_operation_log": _resource(
        "sys_operation_log", "app.modules.system.models.operation_log:SysOperationLog"
    ),
    "sys_login_log": _resource(
        "sys_login_log",
        "app.modules.system.models.login_log:SysLoginLog",
        nullability=TenantNullability.AUDIT_OPTIONAL,
    ),
}


PLATFORM_GLOBAL_TABLES: dict[str, PlatformGlobalResource] = {
    "sys_tenant": PlatformGlobalResource(
        "sys_tenant", "app.modules.system.models.tenant:Tenant", "tenant_id"
    ),
    "ai_agent": PlatformGlobalResource(
        "ai_agent", "app.modules.ai.models.agent:AiAgent"
    ),
    "ai_provider": PlatformGlobalResource(
        "ai_provider", "app.modules.ai.models.provider:AiProvider"
    ),
    "ai_model": PlatformGlobalResource(
        "ai_model", "app.modules.ai.models.model:AiModel"
    ),
}


def load_inventory_table(resource: Any) -> Table:
    """Resolve an inventory object without keeping model instances in global state."""
    module_name, object_name = resource.object_path.split(":", maxsplit=1)
    value = getattr(import_module(module_name), object_name)
    table = value if isinstance(value, Table) else value.__table__
    if table.name != resource.table_name:
        raise RuntimeError(f"inventory mismatch for {resource.table_name}")
    return table
