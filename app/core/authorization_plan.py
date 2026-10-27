"""Immutable declarations for the opt-in authorization transaction protocol.

These values are plans, never evidence that a database lock was acquired. The
fixed layout is pure data; importing it does not load services or ORM models.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.core.exceptions import BusinessException

_MAX_ID = 2**63 - 1


def _invalid() -> BusinessException:
    return BusinessException(
        409, "授权事务计划或资源无效", error_code="FIELD_POLICY_LOCK_ORDER"
    )


def _limit() -> BusinessException:
    return BusinessException(
        422, "授权事务计划超出预算", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    )


class LockMode(StrEnum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class ResourceKind(StrEnum):
    CONVERSATION = "conversation"
    SOURCE_MESSAGE = "source_message"
    PREPARED_ACTION = "prepared_action"
    OPERATION_LOG = "operation_log"
    ROLE = "role"
    DEPT = "dept"
    USER = "user"
    TENANT = "tenant"
    PLATFORM_PRINCIPAL = "platform_principal"
    MENU = "menu"
    PROVIDER = "provider"
    MODEL = "model"
    AGENT = "agent"
    TENANT_MODEL_POLICY = "tenant_model_policy"
    ROLE_AGENT = "role_agent"
    SETTING = "setting"
    CONFIG = "config"
    MAIL_CONFIG = "mail_config"
    POLICY_BINDING = "policy_binding"
    POLICY_REVISION = "policy_revision"
    POLICY_AUDIT = "policy_audit"
    IMPORT_BATCH = "import_batch"
    EXPORT_TASK = "export_task"
    IMPORT_BATCH_LOG = "import_batch_log"
    ASSISTANT_MESSAGE = "assistant_message"
    ROUTING_LOG = "routing_log"
    ROUTING_FEEDBACK = "routing_feedback"
    USER_ROLES = "user_roles"
    USER_DEPTS = "user_depts"
    ROLE_DEPTS = "role_depts"
    ROLE_MENUS = "role_menus"
    SYSTEM_OPERATION_LOG = "system_operation_log"
    PLATFORM_AUDIT = "platform_audit"
    LOGIN_LOG = "login_log"


@dataclass(frozen=True, slots=True)
class _ResourceDefinition:
    table_name: str
    key_columns: tuple[str, ...]
    tenant_column: str | None
    order: int
    module: str
    string_length: int = 0


def _definition(table, keys, order, module, *, global_scope=False, string_length=0):
    return _ResourceDefinition(
        table,
        keys,
        None if global_scope else "tenant_id",
        order,
        module,
        string_length,
    )


_SYSTEM = "app.modules.system.models."
_AI = "app.modules.ai.models."
_RESOURCE_DEFINITIONS = MappingProxyType(
    {
        ResourceKind.CONVERSATION: _definition(
            "ai_conversation", ("conversation_id",), 10, _AI + "conversation"
        ),
        ResourceKind.SOURCE_MESSAGE: _definition(
            "ai_message", ("message_id",), 20, _AI + "message"
        ),
        ResourceKind.PREPARED_ACTION: _definition(
            "ai_prepared_action", ("action_id",), 30, _AI + "prepared_action"
        ),
        ResourceKind.OPERATION_LOG: _definition(
            "ai_operation_log", ("log_id",), 40, _AI + "operation_log"
        ),
        ResourceKind.ROLE: _definition("sys_role", ("role_id",), 50, _SYSTEM + "role"),
        ResourceKind.DEPT: _definition("sys_dept", ("dept_id",), 60, _SYSTEM + "dept"),
        ResourceKind.USER: _definition("sys_user", ("user_id",), 70, _SYSTEM + "user"),
        ResourceKind.TENANT: _definition(
            "sys_tenant", ("tenant_id",), 100, _SYSTEM + "tenant", global_scope=True
        ),
        ResourceKind.PLATFORM_PRINCIPAL: _definition(
            "sys_platform_principal",
            ("principal_id",),
            110,
            "app.modules.platform.models",
            global_scope=True,
        ),
        ResourceKind.MENU: _definition("sys_menu", ("menu_id",), 200, _SYSTEM + "menu"),
        ResourceKind.PROVIDER: _definition(
            "ai_provider", ("provider_id",), 300, _AI + "provider", global_scope=True
        ),
        ResourceKind.MODEL: _definition(
            "ai_model", ("model_id",), 310, _AI + "model", global_scope=True
        ),
        ResourceKind.AGENT: _definition(
            "ai_agent", ("agent_id",), 320, _AI + "agent", global_scope=True
        ),
        ResourceKind.TENANT_MODEL_POLICY: _definition(
            "tenant_ai_model_policy", ("model_id",), 400, _AI + "model_policy"
        ),
        ResourceKind.ROLE_AGENT: _definition(
            "role_ai_agent", ("role_id", "agent_id"), 410, _AI + "role_ai_agent"
        ),
        ResourceKind.SETTING: _definition(
            "sys_setting", ("setting_key",), 500, _SYSTEM + "setting", string_length=100
        ),
        ResourceKind.CONFIG: _definition(
            "sys_config", ("config_id",), 510, _SYSTEM + "config"
        ),
        ResourceKind.MAIL_CONFIG: _definition(
            "sys_mail_config",
            ("config_id",),
            520,
            _SYSTEM + "mail_config",
            global_scope=True,
        ),
        ResourceKind.POLICY_BINDING: _definition(
            "sys_user_field_policy_binding",
            ("role_id",),
            600,
            _SYSTEM + "user_field_policy",
        ),
        ResourceKind.POLICY_REVISION: _definition(
            "sys_user_field_policy_revision",
            ("revision_id",),
            610,
            _SYSTEM + "user_field_policy",
        ),
        ResourceKind.POLICY_AUDIT: _definition(
            "sys_user_field_policy_audit",
            ("audit_id",),
            620,
            _SYSTEM + "user_field_policy",
        ),
        ResourceKind.IMPORT_BATCH: _definition(
            "sys_user_import_batch",
            ("batch_id",),
            700,
            _SYSTEM + "user_transfer",
            string_length=64,
        ),
        ResourceKind.EXPORT_TASK: _definition(
            "sys_user_export_task",
            ("export_id",),
            710,
            _SYSTEM + "user_transfer",
            string_length=64,
        ),
        ResourceKind.IMPORT_BATCH_LOG: _definition(
            "sys_user_import_batch_log",
            ("log_id",),
            720,
            _SYSTEM + "user_transfer",
            string_length=64,
        ),
        ResourceKind.ASSISTANT_MESSAGE: _definition(
            "ai_message", ("message_id",), 800, _AI + "message"
        ),
        ResourceKind.ROUTING_LOG: _definition(
            "ai_routing_log", ("log_id",), 810, _AI + "routing_log"
        ),
        ResourceKind.ROUTING_FEEDBACK: _definition(
            "ai_routing_feedback", ("feedback_id",), 820, _AI + "routing_feedback"
        ),
        ResourceKind.USER_ROLES: _definition(
            "sys_user_role", ("user_id", "role_id"), 900, "app.db.base"
        ),
        ResourceKind.USER_DEPTS: _definition(
            "sys_user_dept", ("user_id", "dept_id"), 910, "app.db.base"
        ),
        ResourceKind.ROLE_DEPTS: _definition(
            "sys_role_dept", ("role_id", "dept_id"), 920, "app.db.base"
        ),
        ResourceKind.ROLE_MENUS: _definition(
            "sys_role_menu", ("role_id", "menu_id"), 930, "app.db.base"
        ),
        ResourceKind.SYSTEM_OPERATION_LOG: _definition(
            "sys_operation_log", ("operation_log_id",), 940, _SYSTEM + "operation_log"
        ),
        ResourceKind.PLATFORM_AUDIT: _definition(
            "sys_platform_audit_log",
            ("audit_id",),
            950,
            "app.modules.platform.models",
            global_scope=True,
        ),
        ResourceKind.LOGIN_LOG: _definition(
            "sys_login_log", ("login_log_id",), 960, _SYSTEM + "login_log"
        ),
    }
)


def _resource_definition(kind: ResourceKind) -> _ResourceDefinition:
    if type(kind) is not ResourceKind:
        raise _invalid()
    return _RESOURCE_DEFINITIONS[kind]


def _valid_id(value: object, *, zero: bool = False) -> bool:
    return type(value) is int and (0 if zero else 1) <= value <= _MAX_ID


@dataclass(frozen=True, slots=True)
class TenantLock:
    tenant_id: int
    mode: LockMode = LockMode.SHARED

    def __post_init__(self) -> None:
        if not _valid_id(self.tenant_id, zero=True) or type(self.mode) is not LockMode:
            raise _invalid()


@dataclass(frozen=True, slots=True)
class ResourceKey:
    kind: ResourceKind
    tenant_id: int | None
    identity: int | str | tuple[int | str, ...]

    def __post_init__(self) -> None:
        definition = _resource_definition(self.kind)
        if definition.tenant_column is None:
            if self.tenant_id is not None:
                raise _invalid()
        elif not _valid_id(self.tenant_id, zero=True):
            raise _invalid()
        values = self.identity if type(self.identity) is tuple else (self.identity,)
        if len(values) != len(definition.key_columns):
            raise _invalid()
        for value in values:
            if definition.string_length:
                if (
                    type(value) is not str
                    or not 1 <= len(value) <= definition.string_length
                    or any(not 33 <= ord(char) <= 126 for char in value)
                ):
                    raise _invalid()
            elif not _valid_id(value, zero=self.kind is ResourceKind.TENANT):
                raise _invalid()
        if self.kind is ResourceKind.MAIL_CONFIG and values != (1,):
            raise _invalid()
        object.__setattr__(self, "identity", values)


def _key_values(key: ResourceKey) -> tuple[int | str, ...]:
    if type(key) is not ResourceKey:
        raise _invalid()
    # Construction canonicalizes scalar identities, including singleton keys.
    assert isinstance(key.identity, tuple)
    return key.identity


def _resource_order(key: ResourceKey) -> tuple[int, int, tuple[int | str, ...]]:
    values = _key_values(key)
    return (
        _resource_definition(key.kind).order,
        -1 if key.tenant_id is None else key.tenant_id,
        values,
    )


@dataclass(frozen=True, slots=True)
class NewResource:
    key: ResourceKey
    parent: ResourceKey | None = None

    def __post_init__(self) -> None:
        if type(self.key) is not ResourceKey:
            raise _invalid()
        if self.parent is not None and (
            type(self.parent) is not ResourceKey
            or self.parent.kind is not ResourceKind.CONVERSATION
            or self.parent.tenant_id != self.key.tenant_id
            or self.key.kind
            not in {
                ResourceKind.SOURCE_MESSAGE,
                ResourceKind.PREPARED_ACTION,
                ResourceKind.OPERATION_LOG,
                ResourceKind.ASSISTANT_MESSAGE,
            }
        ):
            raise _invalid()


_DOMAIN_LIMITS = {
    ResourceKind.ROLE: 4096,
    ResourceKind.DEPT: 8192,
    ResourceKind.USER: 10000,
    ResourceKind.TENANT_MODEL_POLICY: 10000,
}
_CONTEXT_KINDS = frozenset(
    {
        ResourceKind.CONVERSATION,
        ResourceKind.SOURCE_MESSAGE,
        ResourceKind.PREPARED_ACTION,
        ResourceKind.OPERATION_LOG,
    }
)


@dataclass(frozen=True, slots=True)
class LockPlan:
    existing: tuple[ResourceKey, ...] = ()
    new: tuple[NewResource, ...] = ()

    def __post_init__(self) -> None:
        if type(self.existing) is not tuple or type(self.new) is not tuple:
            raise _invalid()
        if len(self.existing) + len(self.new) > 100000:
            raise _limit()
        if any(type(key) is not ResourceKey for key in self.existing) or any(
            type(item) is not NewResource for item in self.new
        ):
            raise _invalid()
        physical = set()
        if any(
            item.parent is not None and item.parent not in self.existing
            for item in self.new
        ):
            raise _invalid()
        counts: dict[ResourceKind, int] = {}
        bindings: dict[int, int] = {}
        contexts = 0
        for key in (*self.existing, *(item.key for item in self.new)):
            identity = (
                _resource_definition(key.kind).table_name,
                key.tenant_id,
                _key_values(key),
            )
            if identity in physical:
                raise _invalid()
            physical.add(identity)
            counts[key.kind] = counts.get(key.kind, 0) + 1
            if key.kind in _CONTEXT_KINDS:
                contexts += 1
            if key.kind is ResourceKind.POLICY_BINDING:
                assert key.tenant_id is not None
                bindings[key.tenant_id] = bindings.get(key.tenant_id, 0) + 1
        if (
            contexts > 16384
            or any(
                counts.get(kind, 0) > limit for kind, limit in _DOMAIN_LIMITS.items()
            )
            or any(count > 1024 for count in bindings.values())
        ):
            raise _limit()
        object.__setattr__(
            self, "existing", tuple(sorted(self.existing, key=_resource_order))
        )
        object.__setattr__(
            self,
            "new",
            tuple(sorted(self.new, key=lambda item: _resource_order(item.key))),
        )


@dataclass(frozen=True, slots=True)
class TransactionBudget:
    timeout_ms: int = 15000
    lock_timeout_ms: int = 2000
    statement_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 1 <= value <= maximum
            for value, maximum in (
                (self.timeout_ms, 60000),
                (self.lock_timeout_ms, 2000),
                (self.statement_timeout_ms, 5000),
            )
        ):
            raise _invalid()
