"""Pure inventory of dormant authorization candidates and unfinished surfaces.

This is a review/static-check manifest, never a runtime dispatch or rollout flag.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Candidate:
    entry_id: str
    qualified_name: str
    source: tuple[str, ...]
    owner: str
    phase: str
    G_mode: str
    tenant_resolver: str
    B_modes: tuple[str, ...]
    plan_builder: str
    apply_function: str
    io_profile: str
    epoch_owner: str
    proof_only: bool
    operation: str = ""
    maintenance: bool = False
    production_connected: bool = False


IO_PROFILES = MappingProxyType(
    {
        "system-write": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "auth-read": (8000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "auth-write": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "auth-email": (8000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "policy-proof": (15000, 2000, 5000, 100000, 128 * 1024 * 1024),
        "import-write": (60000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "bounded-plan": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "bounded-read": (5000, 0, 5000, 4104, 64 * 1024 * 1024),
    }
)
# (protected ms, cumulative G/B ms, statement ms, read rows, decoded bytes).
# These are logical fail-closed limits, not a process-RAM ceiling. ACK has its
# own 8s phase; password 3s / SMTP 15s each have 4 workers + 32 queued jobs.

_S = "app.modules.system.service."
_SYSTEM = _S + "authorization_system_candidate.SystemAuthorizationCandidate"
_SYSTEM_OWNER = "app.db.authorization_system_owner.SystemCandidateOwner.execute"
_IMPORT_OWNER = "app.db.authorization_system_owner.SystemCandidateOwner.import_users"
_AUTH = "app.modules.auth.candidate_owner.AuthCandidateOwner"
_AUTH_SERVICE = "app.modules.auth.candidate_service.AuthCandidateService.apply"
_ACCOUNTS = _S + "auth_candidate_account_service.AuthCandidateAccountService"
_POLICY = _S + "planned_user_field_policy_service.PlannedUserFieldPolicyService"
_TX = _S + "authorization_transaction_service._Transaction"
_IMPORT = _S + "authorization_import_candidate.ImportAuthorizationCandidate"

SYSTEM_OPERATIONS = MappingProxyType(
    {
        "user.create": (
            "user_operations_service.UserOperationsService.create_user",
            "user_service.UserService.create_user",
        ),
        "user.update": ("user_service.UserService.update_user",),
        "user.profile": ("user_service.UserService.update_profile",),
        "user.password": ("user_service.UserService.reset_password",),
        "user.change_password": ("user_service.UserService.change_password",),
        "user.revoke": ("user_service.UserService.revoke_sessions",),
        "user.roles": (
            "user_role_assignment_service.UserRoleAssignmentService.replace_roles",
        ),
        "user.departments": (
            "user_department_assignment_service.UserDepartmentAssignmentService.replace_departments",
        ),
        "user.delete": ("user_service.UserService.delete_user",),
        "user.batch_delete": ("user_service.UserService.batch_delete_users",),
        "role.create": (
            "role_management_service.RoleManagementService.create",
            "role_service.RoleService.create_role",
        ),
        "role.update": (
            "role_management_service.RoleManagementService.update",
            "role_service.RoleService.update_role",
        ),
        "role.menus": (
            "role_management_service.RoleManagementService.update_menus",
            "role_service.RoleService.update_role_menu",
        ),
        "role.delete": ("role_service.RoleService.delete_role",),
        "role.batch_delete": ("role_service.RoleService.batch_delete_roles",),
        "dept.create": ("dept_service.DeptService.create",),
        "dept.update": ("dept_service.DeptService.update",),
        "dept.move": ("dept_service.DeptService.move",),
        "dept.delete": ("dept_service.DeptService.delete",),
        "dept.batch_delete": ("dept_service.DeptService.batch_delete",),
    }
)

_system = tuple(
    Candidate(
        "system." + operation,
        _SYSTEM + ".execute",
        tuple(_S + source for source in sources),
        _SYSTEM_OWNER,
        "owner",
        "S",
        "argument:TenantContext",
        ("X",),
        _SYSTEM + "._prepare",
        _SYSTEM + "._apply",
        "system-write",
        _TX + ".aggregate",
        False,
        operation=operation,
    )
    for operation, sources in SYSTEM_OPERATIONS.items()
)

_auth_sources = {
    "login": ("app.modules.auth.service.AuthService.authenticate",),
    "refresh": ("app.modules.auth.service.refresh_access_token",),
    "current": ("app.modules.auth.service.get_current_user",),
    "register": ("app.modules.auth.self_service_api.register",),
    "reset": ("app.modules.auth.self_service_api.reset_password",),
    "bind": ("app.modules.auth.self_service_api.bind_email",),
    "prepare_email": (
        "app.modules.auth.self_service_api.send_code",
        "app.modules.auth.self_service_api.binding_code",
    ),
}
_auth = tuple(
    Candidate(
        "auth." + operation,
        _AUTH + "." + operation,
        sources,
        _AUTH + "." + operation,
        "owner",
        "S",
        "database:tenant-and-user-security-versions",
        ("X",) if operation in {"register", "reset", "bind"} else ("S",),
        _ACCOUNTS + ".plan",
        _AUTH_SERVICE,
        "auth-write"
        if operation in {"register", "reset", "bind"}
        else "auth-email"
        if operation == "prepare_email"
        else "auth-read",
        _TX + ".aggregate" if operation in {"register", "reset", "bind"} else "none",
        False,
    )
    for operation, sources in _auth_sources.items()
)

_policy = tuple(
    Candidate(
        "policy." + method,
        _POLICY + "." + method,
        (_S + "user_field_policy_service.UserFieldPolicyService." + method,),
        "caller_owned",
        "proof_writer",
        "S",
        "argument:TenantContext",
        ("X",),
        _POLICY + ".plan",
        _POLICY + "." + method,
        "policy-proof",
        _TX + ".complete_policy_change",
        True,
    )
    for method in ("publish", "archive", "mark_ready")
)

CANDIDATES = (
    _system
    + _auth
    + _policy
    + (
        Candidate(
            "auth.protected_apply",
            _AUTH_SERVICE,
            tuple(source for sources in _auth_sources.values() for source in sources),
            _AUTH + "._protected",
            "proof_writer",
            "S",
            "database:tenant-and-user-security-versions",
            ("S", "X"),
            _ACCOUNTS + ".plan",
            _AUTH_SERVICE,
            "auth-write",
            _TX + ".aggregate",
            True,
        ),
        Candidate(
            "auth.account_apply",
            _ACCOUNTS + ".apply",
            (
                _S + "account_service.AccountService.register",
                _S + "account_service.AccountService.reset",
                _S + "account_service.AccountService.bind",
            ),
            _AUTH + "._protected",
            "proof_writer",
            "S",
            "database:tenant-and-user-security-versions",
            ("X",),
            _ACCOUNTS + ".plan",
            _ACCOUNTS + ".apply",
            "auth-write",
            _TX + ".aggregate",
            True,
        ),
        Candidate(
            "auth.account_read",
            _ACCOUNTS + ".read",
            (_S + "account_service.AccountService.find",),
            _AUTH + "._read",
            "read",
            "none",
            "argument:tenant-id",
            (),
            "none",
            "none",
            "bounded-read",
            "none",
            False,
        ),
        Candidate(
            "auth.tenant_locator",
            _ACCOUNTS + ".tenant_id",
            ("app.modules.auth.service.AuthService.resolve_login_tenant",),
            _AUTH + ".login",
            "read",
            "none",
            "argument:tenant-code",
            (),
            "none",
            "none",
            "bounded-read",
            "none",
            False,
        ),
        Candidate(
            "auth.account_plan",
            _ACCOUNTS + ".plan",
            (),
            "caller_owned",
            "plan",
            "S",
            "argument:tenant-id",
            ("S", "X"),
            _ACCOUNTS + ".plan",
            "none",
            "bounded-plan",
            "none",
            True,
        ),
        Candidate(
            "policy.plan",
            _POLICY + ".plan",
            (),
            "caller_owned",
            "plan",
            "S",
            "argument:TenantContext",
            ("X",),
            _POLICY + ".plan",
            "none",
            "policy-proof",
            "none",
            True,
        ),
        Candidate(
            "policy.role_history",
            _S + "planned_user_field_policy_service.assert_role_history_deletable",
            (),
            _SYSTEM_OWNER,
            "read",
            "S",
            "argument:TenantContext",
            ("X",),
            "none",
            "none",
            "bounded-plan",
            "none",
            True,
        ),
        Candidate(
            "import.execute",
            _IMPORT + ".execute",
            (_S + "user_import_service.batch_create_users_from_records",),
            _IMPORT_OWNER,
            "proof_writer",
            "S",
            "argument:TenantContext",
            ("X",),
            _IMPORT + ".execute",
            _IMPORT + "._apply",
            "import-write",
            _TX + ".aggregate",
            True,
        ),
        Candidate(
            "import.owner",
            _IMPORT_OWNER,
            (_S + "user_import_service.batch_create_users_from_records",),
            _IMPORT_OWNER,
            "owner",
            "S",
            "argument:TenantContext",
            ("X",),
            _IMPORT + ".execute",
            _IMPORT + "._apply",
            "import-write",
            _TX + ".aggregate",
            False,
        ),
    )
)

PENDING_SURFACES = MappingProxyType(
    {
        "2B3": (
            "global menu/buttons",
            "role-agent",
            "Agent/Provider/Model/security policy",
            "Platform tenant/Principal",
            "settings/mail/seed/maintenance",
        ),
        "2B4": (
            "AI context/gateway owners",
            "delete context dependencies",
            "export/FILE",
            "startup/cleanup/recovery",
        ),
        "2B5": (
            "production routers/session factory",
            "full writer barrier wiring",
            "combination concurrency verification",
        ),
    }
)
