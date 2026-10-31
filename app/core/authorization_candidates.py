"""Pure inventory of authorization lock and IO contracts.

This is a review/static-check manifest, never a runtime dispatch or rollout flag.
"""

from dataclasses import dataclass, replace
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Candidate:
    entry_id: str
    qualified_name: str
    source: tuple[str, ...]
    owner: str
    phase: str
    G_mode: str  # Strongest mode used by this entry; conditional scope is below.
    tenant_resolver: str
    B_modes: tuple[str, ...]
    plan_builder: str
    apply_function: str
    io_profile: str
    epoch_owner: str
    proof_only: bool
    operation: str = ""
    maintenance: bool = False
    production_connected: bool = False  # This manifest cannot certify ingress wiring.


IO_PROFILES = MappingProxyType(
    {
        "system-write": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "auth-read": (8000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "user-read": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "auth-write": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "auth-email": (8000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "policy-proof": (15000, 2000, 5000, 100000, 128 * 1024 * 1024),
        "import-write": (60000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "bounded-plan": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "bounded-read": (5000, 0, 5000, 4104, 64 * 1024 * 1024),
        "global-write": (60000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "metadata": (8000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "audit-intent": (3000, 2000, 3000, 100000, 64 * 1024 * 1024),
        "external-dns": (0, 0, 0, 0, 64 * 1024),
        "external-probe": (0, 0, 0, 0, 64 * 1024),
        "external-mail": (0, 0, 0, 0, 64 * 1024),
        "external-password": (0, 0, 0, 0, 1024),
        "external-import": (0, 0, 0, 1000, 10 * 1024 * 1024),
        "transfer-write": (15000, 2000, 5000, 100000, 64 * 1024 * 1024),
        "transfer-prepare": (5000, 0, 5000, 100000, 64 * 1024 * 1024),
        "lease-cas": (2000, 0, 2000, 1, 1024),
        "external-redis-hint": (0, 0, 0, 0, 1024),
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
        "dept.members": (
            "user_department_assignment_service.UserDepartmentAssignmentService.replace_department_members",
        ),
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
        _SYSTEM + ".plan",
        _SYSTEM + ".apply",
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

_A = "app.modules.ai.service."
_P = "app.modules.platform."
_CONTROL_OWNER = "app.db.authorization_control_owner.ControlCandidateOwner"
_CATALOG = _A + "authorization_catalog_candidate.CatalogAuthorizationCandidate"
_CONFIG = (
    _S + "authorization_configuration_candidate.ConfigurationAuthorizationCandidate"
)
_TENANT = _S + "authorization_tenant_candidate.TenantControlCandidate"
_PRINCIPAL = _P + "authorization_principal_candidate.PrincipalAuthorizationCandidate"
_PRINCIPAL_OWNER = _P + "authorization_principal_owner.PrincipalCandidateOwner"
_RETENTION_OWNER = _P + "authorization_retention_owner.RetentionCandidateOwner"
_SEED_OWNER = "app.db.authorization_seed_owner.SeedCandidateOwner.execute"

CATALOG_OPERATIONS = MappingProxyType(
    {
        "agent.update": _A + "agent_admin.AgentAdminService.update_agent",
        **{
            f"{kind}.{action}": _A + f"{kind}_service.{kind.title()}Service.{action}"
            for kind in ("provider", "model")
            for action in ("create", "update", "delete")
        },
        **{
            f"model_policy.{action}": _A
            + f"tenant_model_policy_admin_service.TenantModelPolicyAdminService.{action}"
            for action in ("put_many", "put", "delete")
        },
        "role.agents": _A + "role_agent.RoleAgentService.put_binding",
    }
)
CONFIGURATION_OPERATIONS = MappingProxyType(
    {
        "menu.create": _S + "menu_service.MenuService.create_menu",
        "menu.update": _S + "menu_service.MenuService.update_menu",
        "menu.delete": _S + "menu_service.MenuService.delete_menu",
        "menu.batch_delete": _S + "menu_service.MenuService.batch_delete_menus",
        "settings.save": _S + "settings_service.SettingsService.save_group",
        "mail.save": _S + "mail_service.MailService.save",
        **{
            f"config.{action}": _S + f"config_service.ConfigService.{action}"
            for action in ("create", "update", "delete", "batch_delete")
        },
        "config.import": _S + "config_service.ConfigService.import_configs",
    }
)
TENANT_OPERATIONS = MappingProxyType(
    {
        f"tenant.{action}": _S
        + f"tenant_lifecycle_service.TenantLifecycleService.{action}_tenant"
        for action in ("prepare", "activate", "disable")
    }
)
OPERATION_FAMILIES = MappingProxyType(
    {
        "system": (
            SYSTEM_OPERATIONS,
            _S + "authorization_system_candidate",
            "SYSTEM_CANDIDATE_ENTRIES",
        ),
        "catalog": (
            CATALOG_OPERATIONS,
            _A + "authorization_catalog_inputs",
            "CATALOG_CANDIDATE_ENTRIES",
        ),
        "configuration": (
            CONFIGURATION_OPERATIONS,
            _S + "authorization_configuration_candidate",
            "CONFIGURATION_PERMISSIONS",
        ),
        "tenant": (
            TENANT_OPERATIONS,
            _S + "authorization_tenant_candidate",
            "TENANT_OPERATIONS",
        ),
    }
)


def _control_entry(
    entry_id,
    qualified,
    *,
    source=(),
    owner="caller_owned",
    phase="proof_writer",
    guard="S",
    barriers=("S", "X"),
    plan="none",
    apply="none",
    profile="global-write",
    epoch="none",
    operation="",
    maintenance=False,
):
    return Candidate(
        entry_id,
        qualified,
        source,
        owner,
        phase,
        guard,
        _control_tenants(entry_id, operation),
        barriers,
        plan,
        apply,
        profile,
        epoch,
        phase != "owner",
        operation,
        maintenance,
    )


def _control_tenants(entry_id, operation):
    if operation == "tenant.prepare":
        return "structure:new-B-only;replay:source-S+target-X"
    if entry_id.startswith("bootstrap."):
        return "source-S+target-X;G-X-does-not-imply-all-B"
    if operation.startswith("model_policy."):
        return "verified-system-source-S+target-X"
    if operation == "settings.save":
        return "security:G-X/all-B-X;other:G-S/current-B-X"
    if operation == "mail.save" or operation.startswith(
        ("agent.", "provider.", "model.")
    ):
        return "database:all-registered-tenants<=1024;all-B-X"
    if operation:
        return "verified-current-or-lifecycle-target-B-X;source-S-if-distinct"
    if entry_id in {"principal.create", "principal.permissions"} or entry_id.startswith(
        "seed."
    ):
        return "database:all-registered-tenants<=1024;all-B-X"
    if entry_id.startswith("principal."):
        return "principal-only:no-tenant-barrier"
    if entry_id.startswith("retention."):
        return "verified-target-B;purge-X/preview-S"
    return "caller:explicit-complete-scope;external-phase:no-barriers"


CANDIDATES += tuple(
    _control_entry(
        f"{family}.{operation}",
        target + ".execute",
        source=(source,),
        owner=_CONTROL_OWNER + ".execute",
        guard="X"
        if operation
        in {
            "agent.update",
            "provider.create",
            "provider.update",
            "provider.delete",
            "model.create",
            "model.update",
            "model.delete",
        }
        or operation in {"mail.save", "settings.save", "tenant.prepare"}
        else "S",
        plan=target + ".execute",
        apply=target + ".execute",
        epoch=_S
        + "authorization_transaction_service.AuthorizationTransactionService.scope_creation"
        if operation == "tenant.prepare"
        else _TX + ".aggregate_global"
        if operation
        in {
            "agent.update",
            "provider.create",
            "provider.update",
            "provider.delete",
            "model.create",
            "model.update",
            "model.delete",
        }
        or operation in {"mail.save", "settings.save"}
        else _TX + ".aggregate",
        operation=operation,
    )
    for family, operations, target in (
        ("catalog", CATALOG_OPERATIONS, _CATALOG),
        ("configuration", CONFIGURATION_OPERATIONS, _CONFIG),
        ("tenant", TENANT_OPERATIONS, _TENANT),
    )
    for operation, source in operations.items()
)

# Explicit phase inventory. No filesystem-generated entries: adding a public
# async phase must be reviewed here and in the AST coverage gate.
_CONTROL_PHASES = (
    (
        "catalog.probe_transport",
        _A + "authorization_catalog_probe.probe",
        "external",
        "none",
        "external-probe",
    ),
    (
        "configuration.import_parser",
        _S + "authorization_configuration_input.parse_import",
        "external",
        "none",
        "external-import",
    ),
    ("control.authorize", _CONTROL_OWNER + ".authorize", "owner", "S", "audit-intent"),
    (
        "control.complete_failure",
        _CONTROL_OWNER + ".complete_failure",
        "owner",
        "S",
        "audit-intent",
    ),
    ("catalog.prepare", _CATALOG + ".prepare", "read", "none", "bounded-read"),
    (
        "catalog.validate",
        _CATALOG + ".validate_preparation",
        "external",
        "none",
        "external-dns",
    ),
    (
        "catalog.prepare_probe",
        _CATALOG + ".prepare_probe",
        "read",
        "none",
        "bounded-read",
    ),
    (
        "catalog.run_probe",
        _CATALOG + ".run_probe",
        "external",
        "none",
        "external-probe",
    ),
    (
        "catalog.complete_probe",
        _CATALOG + ".complete_probe",
        "proof_writer",
        "S",
        "metadata",
    ),
    (
        "configuration.parse_import",
        _CONFIG + ".parse_import",
        "external",
        "none",
        "external-import",
    ),
    (
        "configuration.prepare_mail",
        _CONFIG + ".prepare_mail_test",
        "read",
        "S",
        "metadata",
    ),
    (
        "configuration.run_mail",
        _CONFIG + ".run_mail_test",
        "external",
        "none",
        "external-mail",
    ),
    (
        "configuration.complete_mail",
        _CONFIG + ".complete_mail_test",
        "proof_writer",
        "S",
        "metadata",
    ),
    (
        "control.identity",
        _S + "authorization_control_identity.read_identity",
        "read",
        "S",
        "bounded-plan",
    ),
    (
        "control.revalidate",
        _S + "authorization_control_identity.assert_identity",
        "read",
        "S",
        "bounded-plan",
    ),
    (
        "control.target",
        _S + "authorization_control_identity.read_policy_target",
        "read",
        "X",
        "bounded-plan",
    ),
    (
        "control.append_audit",
        _S + "authorization_control_audit.ControlAudit.append",
        "proof_writer",
        "S",
        "audit-intent",
    ),
    (
        "bootstrap.execute",
        _P + "authorization_bootstrap_candidate.TenantBootstrapCandidate.execute",
        "proof_writer",
        "X",
        "global-write",
    ),
    (
        "bootstrap.system_plan",
        _S + "authorization_bootstrap_candidate.SystemBootstrapCandidate.prepare",
        "plan",
        "X",
        "global-write",
    ),
    (
        "bootstrap.system_apply",
        _S + "authorization_bootstrap_candidate.SystemBootstrapCandidate.apply",
        "proof_writer",
        "X",
        "global-write",
    ),
    (
        "bootstrap.ai_plan",
        _A + "authorization_bootstrap_candidate.AiBootstrapCandidate.prepare",
        "plan",
        "X",
        "global-write",
    ),
    (
        "bootstrap.ai_apply",
        _A + "authorization_bootstrap_candidate.AiBootstrapCandidate.apply",
        "proof_writer",
        "X",
        "global-write",
    ),
    (
        "principal.read",
        _P + "authorization_principal_candidate.read_principal",
        "read",
        "S",
        "bounded-plan",
    ),
    (
        "principal.revalidate",
        _P + "authorization_principal_candidate.assert_principal",
        "read",
        "S",
        "bounded-plan",
    ),
    (
        "principal.password_prepare",
        _P + "authorization_principal_candidate.prepare_password",
        "read",
        "none",
        "bounded-read",
    ),
    (
        "principal.password_verify",
        _P + "authorization_principal_candidate.verify_preparation",
        "external",
        "none",
        "external-password",
    ),
    (
        "principal.password_revalidate",
        _P + "authorization_principal_candidate.check_password_preparation",
        "read",
        "S",
        "bounded-plan",
    ),
    ("principal.login_apply", _PRINCIPAL + ".login", "proof_writer", "S", "metadata"),
    ("principal.current_read", _PRINCIPAL + ".current", "read", "S", "metadata"),
    ("principal.login", _PRINCIPAL_OWNER + ".login", "owner", "S", "metadata"),
    ("principal.current", _PRINCIPAL_OWNER + ".current", "owner", "S", "metadata"),
    (
        "principal.authorize",
        _PRINCIPAL_OWNER + ".authorize",
        "owner",
        "S",
        "audit-intent",
    ),
    (
        "principal.append_audit",
        _P + "authorization_principal_audit.PrincipalAudit.append",
        "proof_writer",
        "S",
        "audit-intent",
    ),
    (
        "principal.create",
        _PRINCIPAL_OWNER + ".create_first",
        "owner",
        "X",
        "global-write",
    ),
    (
        "principal.permissions",
        _PRINCIPAL_OWNER + ".replace_permissions",
        "owner",
        "X",
        "global-write",
    ),
    (
        "principal.complete_failure",
        _PRINCIPAL_OWNER + ".complete_failure",
        "owner",
        "S",
        "audit-intent",
    ),
    (
        "support.execute",
        _P + "authorization_support_owner.SupportCandidateOwner.execute",
        "owner",
        "S",
        "metadata",
    ),
    ("retention.purge", _RETENTION_OWNER + ".purge", "owner", "S", "global-write"),
    ("retention.preview", _RETENTION_OWNER + ".preview", "owner", "S", "global-write"),
    (
        "retention.apply",
        _S
        + "authorization_retention_candidate.RetentionAuthorizationCandidate.execute",
        "proof_writer",
        "S",
        "global-write",
    ),
    ("seed.execute", _SEED_OWNER, "owner", "X", "global-write"),
    (
        "seed.system_plan",
        _S + "authorization_seed_candidate.SystemSeedCandidate.plan",
        "plan",
        "X",
        "global-write",
    ),
    (
        "seed.system_apply",
        _S + "authorization_seed_candidate.SystemSeedCandidate.apply",
        "proof_writer",
        "X",
        "global-write",
    ),
    (
        "seed.ai_plan",
        _A + "authorization_seed_candidate.AiSeedCandidate.plan",
        "plan",
        "X",
        "global-write",
    ),
    (
        "seed.ai_apply",
        _A + "authorization_seed_candidate.AiSeedCandidate.apply",
        "proof_writer",
        "X",
        "global-write",
    ),
)
MAINTENANCE_ENTRIES = frozenset(
    {
        "principal.create",
        "principal.permissions",
        "seed.execute",
        "seed.system_plan",
        "seed.system_apply",
        "seed.ai_plan",
        "seed.ai_apply",
    }
)
CANDIDATES += tuple(
    _control_entry(
        entry,
        qualified,
        phase=phase,
        guard=guard,
        barriers=()
        if guard == "none" or entry.startswith("principal.") and guard == "S"
        else ("S", "X"),
        owner=qualified if phase == "owner" else "caller_owned",
        profile=profile,
        maintenance=entry in MAINTENANCE_ENTRIES,
        epoch=_TX + ".aggregate_global"
        if entry in {"principal.create", "principal.permissions", "seed.execute"}
        else _TX + ".aggregate"
        if entry == "bootstrap.execute"
        else "none",
    )
    for entry, qualified, phase, guard, profile in _CONTROL_PHASES
)

_AI_FACT_PHASES = (
    ("ai.seal_insertions", _TX + ".seal_insertions", "proof_writer"),
    ("ai.assert_planning", _TX + ".assert_planning", "read"),
    ("ai.assert_planned", _TX + ".assert_planned", "read"),
    ("ai.file_phase", _TX + ".begin_file_phase", "proof_writer"),
    ("ai.file_budget", _TX + ".file_io", "proof_writer"),
    (
        "ai.context_discover",
        _A + "authorization_context_candidate.AIContextCandidate.discover",
        "plan",
    ),
    (
        "ai.context_recheck",
        _A + "authorization_context_candidate.AIContextCandidate.recheck",
        "read",
    ),
    (
        "ai.recovery",
        _A + "authorization_recovery_candidate.AIRecoveryCandidate.reconcile",
        "proof_writer",
    ),
    (
        "ai.recovery_owner",
        "app.db.authorization_ai_owner.AIAuthorizationOwner.reconcile",
        "owner",
    ),
    (
        "ai.recovery_page",
        "app.db.authorization_ai_owner.AIAuthorizationOwner.recover_page",
        "owner",
    ),
    ("ai.factual_update", _TX + ".update_factual", "proof_writer"),
    ("ai.database_clock", _TX + ".database_now", "read"),
)
CANDIDATES += tuple(
    Candidate(
        entry,
        qualified,
        (),
        qualified if phase == "owner" else "caller_owned",
        phase,
        "S",
        "exact-tenant; recovery_page discovers without barriers then each item uses a fresh single-tenant proof",
        ("S",),
        _A + "authorization_context_candidate.AIContextCandidate.discover",
        qualified,
        "auth-read",
        "none",
        phase == "proof_writer",
    )
    for entry, qualified, phase in _AI_FACT_PHASES
)

_COMPOSITION_PHASES = (
    (
        "system.prepare_input",
        _SYSTEM + ".prepare_input",
        "external",
        "external-password",
    ),
    ("system.plan", _SYSTEM + ".plan", "plan", "system-write"),
    ("system.apply", _SYSTEM + ".apply", "proof_writer", "system-write"),
    ("import.prepare_input", _IMPORT + ".prepare_input", "external", "external-import"),
    ("import.plan", _IMPORT + ".plan", "plan", "import-write"),
    ("import.apply", _IMPORT + ".apply", "proof_writer", "import-write"),
)
CANDIDATES += tuple(
    Candidate(
        entry,
        qualified,
        (),
        "caller_owned",
        phase,
        "none" if phase == "external" else "S",
        "argument:TenantContext",
        () if phase == "external" else ("X",),
        qualified,
        qualified,
        profile,
        _TX + ".aggregate" if phase == "proof_writer" else "none",
        phase == "proof_writer",
    )
    for entry, qualified, phase, profile in _COMPOSITION_PHASES
)

_GATEWAY = "app.modules.ai.agents.gateway.authorization_execution.PlannedGatewayOwner.execute_claim"
_EXECUTION = _A + "authorization_execution_candidate.AIExecutionCandidate"
_TOOL_ADAPTER = _S + "authorization_ai_tool_adapter.SystemAIToolAdapter"
_GATEWAY_TOOLS = (
    "dept.create",
    "dept.update",
    "dept.move",
    "role.create",
    "role.update",
    "role.update_menus",
    "user.update_dept",
    "user.update_roles",
    "user.create",
    "user.reset_password",
    "user.update",
    "role.update_agents",
    "job.update_cron",
    "user.batch_delete",
    "user.export",
    "user.import_execute",
)
OPERATION_FAMILIES = MappingProxyType(
    dict(OPERATION_FAMILIES)
    | {
        "gateway": (
            _GATEWAY_TOOLS,
            _S + "authorization_ai_tool_adapter",
            "TOOL_OPERATIONS",
        ),
    }
)
CANDIDATES += tuple(
    Candidate(
        "gateway." + tool,
        _GATEWAY,
        (),
        _GATEWAY,
        "owner",
        "S",
        "trusted TenantContext; exact single tenant; lock-free preflight then fresh proof",
        ("X",),
        _TOOL_ADAPTER + ".plan",
        _EXECUTION + ".execute",
        "import-write",
        "none" if tool in {"job.update_cron", "user.export"} else _TX + ".aggregate",
        False,
        operation=tool,
    )
    for tool in _GATEWAY_TOOLS
)
CANDIDATES += tuple(
    Candidate(
        "ai.execution." + entry,
        qualified,
        (),
        _GATEWAY,
        phase,
        guard,
        "trusted TenantContext; fixed DB tool mapping",
        () if guard == "none" else ("X",),
        qualified,
        qualified,
        profile,
        _TX + ".aggregate" if phase == "proof_writer" else "none",
        phase == "proof_writer",
    )
    for entry, qualified, phase, guard, profile in (
        ("reference", _EXECUTION + ".reference", "read", "none", "bounded-read"),
        ("authorize", _EXECUTION + ".authorize", "read", "S", "import-write"),
        ("execute", _EXECUTION + ".execute", "proof_writer", "S", "import-write"),
        (
            "prepare_input",
            _TOOL_ADAPTER + ".prepare_input",
            "external",
            "none",
            "external-password",
        ),
        ("plan", _TOOL_ADAPTER + ".plan", "plan", "S", "import-write"),
        ("apply", _TOOL_ADAPTER + ".apply", "proof_writer", "S", "import-write"),
    )
)

_LIFECYCLE = _A + "authorization_lifecycle_candidate.LifecycleCandidate"
_CHAT = _A + "authorization_chat_candidate.ChatCandidate"
_TRANSFER = _S + "authorization_transfer_candidate.TransferCandidate"
_CLEANUP = _S + "authorization_transfer_cleanup_candidate.TransferCleanupCandidate"
_DELETE = _S + "authorization_ai_delete_candidate.AIDeleteCandidate"
CANDIDATES += (
    Candidate(
        "http_delete.execute",
        _DELETE + ".execute_http",
        (
            _S + "user_service.UserService.delete_user",
            _S + "user_service.UserService.batch_delete_users",
        ),
        _SYSTEM_OWNER,
        "proof_writer",
        "S",
        "argument:TenantContext",
        ("X",),
        _DELETE + ".plan",
        _DELETE + ".apply",
        "system-write",
        _TX + ".aggregate",
        True,
    ),
)
_JOB = "app.modules.job.service.authorization_job_candidate.JobAuthorizationCandidate"
_FILE_ADAPTER = _S + "authorization_ai_file_adapter.AIFileAdapter"
_COMPLETION_ENTRIES = (
    (
        "app.db.authorization_reconciliation_owner.ReconciliationOwner",
        ("inspect", "annotate"),
        "owner",
        "S",
        ("S", "X"),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_reconciliation_candidate.ReconciliationCandidate",
        ("execute",),
        "proof_writer",
        "S",
        ("S", "X"),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_completion_facts_owner.CompletionFactsOwner",
        ("read",),
        "owner",
        "none",
        (),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_business_owner.BusinessOwner",
        ("execute",),
        "owner",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        _S + "authorization_business_candidate.BusinessCandidate",
        ("discover",),
        "read",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        _S + "authorization_business_candidate.BusinessCandidate",
        ("execute",),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        "app.db.authorization_job_owner.JobOwner",
        ("execute", "claim", "finish", "scan"),
        "owner",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        "app.modules.job.service.authorization_job_http.JobHttpCandidate",
        ("discover",),
        "read",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        "app.modules.job.service.authorization_job_http.JobHttpCandidate",
        ("execute",),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        "app.modules.job.service.authorization_job_lifecycle.JobLifecycleCandidate",
        ("claim", "finish", "reclaim"),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        "app.db.authorization_http_audit_owner.HttpAuditOwner",
        ("username",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_http_audit_owner.HttpAuditOwner",
        ("operation", "login"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_gateway_read_candidate.GatewayReadCandidate",
        ("read_tool", "preflight"),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_chat_owner.ChatOwner",
        ("read_tool", "preflight"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_gateway_audit_candidate.GatewayAuditCandidate",
        ("start_operation", "finish_operation"),
        "proof_writer",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_chat_owner.ChatOwner",
        ("start_operation", "finish_operation"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _S + "authorization_chat_image_candidate.ChatImageCandidate",
        ("image",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_upload_owner.UploadOwner",
        ("read_chat_image",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_chat_model",
        ("model_candidates",),
        "read",
        "none",
        (),
        "bounded-read",
        "none",
    ),
    (
        _A + "authorization_chat_ingress_candidate.ChatIngressCandidate",
        ("model_candidates",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_chat_owner.ChatOwner",
        ("select_model",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_upload_owner.UploadOwner",
        ("upload", "delete"),
        "owner",
        "S",
        ("S", "X"),
        "transfer-write",
        "none",
    ),
    (
        "app.modules.system.service.authorization_upload_candidate.UploadCandidate",
        ("policies",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.modules.system.service.authorization_upload_candidate.UploadCandidate",
        ("insert", "delete"),
        "proof_writer",
        "S",
        ("X",),
        "transfer-write",
        "none",
    ),
    (
        "app.db.authorization_user_read_owner.UserReadOwner",
        ("execute",),
        "owner",
        "S",
        ("S",),
        "user-read",
        "none",
    ),
    (
        _S + "user_read_service.UserReadService",
        ("execute", "actor", "legacy_context", "user_list"),
        "read",
        "S",
        ("S",),
        "user-read",
        "none",
    ),
    (
        _S + "user_field_context.UserFieldContextService",
        ("load",),
        "read",
        "S",
        ("S",),
        "user-read",
        "none",
    ),
    (
        _S + "user_field_context.UserFieldContext",
        ("decisions",),
        "read",
        "S",
        ("S",),
        "user-read",
        "none",
    ),
    (
        _S + "user_copy_read_service.UserCopyReadService",
        ("audit_page",),
        "read",
        "S",
        ("S",),
        "user-read",
        "none",
    ),
    (
        _S + "user_copy_read_service.UserCopyReadService",
        ("projected_audit_page",),
        "read",
        "S",
        ("X",),
        "user-read",
        "none",
    ),
    (
        "app.db.authorization_catalog_read_owner.CatalogReadOwner",
        ("execute",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.modules.platform.authorization_catalog_read.CatalogReadCandidate",
        ("project",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (_CATALOG, ("plan_role_agents",), "plan", "S", ("X",), "system-write", "none"),
    (
        _CATALOG,
        ("apply_role_agents",),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        _TX + ".aggregate",
    ),
    (_TOOL_ADAPTER, ("validate_planned",), "read", "S", ("X",), "import-write", "none"),
    (_JOB, ("plan",), "plan", "S", ("X",), "system-write", "none"),
    (_JOB, ("apply",), "proof_writer", "S", ("X",), "system-write", "none"),
    (_DELETE, ("plan",), "plan", "S", ("X",), "system-write", "none"),
    (
        _DELETE,
        ("apply",),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        _TX + ".aggregate",
    ),
    (
        _FILE_ADAPTER,
        ("prepare_input",),
        "external",
        "none",
        (),
        "transfer-prepare",
        "none",
    ),
    (_FILE_ADAPTER, ("plan",), "plan", "S", ("X",), "import-write", "none"),
    (
        _FILE_ADAPTER,
        ("apply",),
        "proof_writer",
        "S",
        ("X",),
        "import-write",
        _TX + ".aggregate",
    ),
    (_TRANSFER, ("plan",), "plan", "S", ("X",), "transfer-write", "none"),
    (
        _TRANSFER,
        ("apply", "materialize_preview", "publish_preview"),
        "proof_writer",
        "S",
        ("X",),
        "transfer-write",
        "none",
    ),
    (
        _TRANSFER,
        ("download", "rest_download"),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (_CLEANUP, ("prepare",), "external", "none", (), "transfer-prepare", "none"),
    (_CLEANUP, ("execute",), "proof_writer", "S", ("S",), "auth-read", "none"),
    (_CLEANUP, ("cancel",), "proof_writer", "S", ("X",), "transfer-write", "none"),
    (_LIFECYCLE, ("reference",), "read", "none", (), "bounded-read", "none"),
    (
        _LIFECYCLE,
        ("unexecuted", "resume_read"),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_orphan_candidate.OrphanCandidate",
        ("settle",),
        "proof_writer",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _LIFECYCLE,
        ("prepare", "confirm"),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        _LIFECYCLE,
        ("decide", "expire", "sweep", "admit", "gateway_timeout", "poll"),
        "proof_writer",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (_CHAT, ("conversation",), "proof_writer", "S", ("X",), "auth-read", "none"),
    (
        _A + "authorization_chat_ingress_candidate",
        ("freeze_run_authority", "tool_settings"),
        "read",
        "none",
        (),
        "bounded-read",
        "none",
    ),
    (
        _A + "authorization_chat_ingress_candidate.ChatIngressCandidate",
        ("start_run", "routing_audit"),
        "proof_writer",
        "S",
        ("X",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_chat_owner.ChatOwner",
        ("start_run", "routing_audit", "routing_feedback"),
        "owner",
        "S",
        ("X",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_feedback_candidate.FeedbackCandidate",
        ("submit",),
        "proof_writer",
        "S",
        ("X",),
        "auth-read",
        "none",
    ),
    (
        _CHAT,
        ("source", "finalize", "invalidate", "recover", "recover_orphan"),
        "proof_writer",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _CHAT,
        ("messages", "list_conversations"),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.modules.auth.service",
        ("load_background_projection_authority",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_preview_candidate.PreviewCandidate",
        ("input_file", "receipt", "admit_gateway"),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_pending_candidate.PendingCandidate",
        ("reference", "prepare"),
        "proof_writer",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_read_confirmation.ReadConfirmationCandidate",
        ("execute", "project"),
        "proof_writer",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_file_parse",
        ("file_parse_input",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _S + "authorization_auxiliary_candidate.AuxiliaryCandidate",
        ("options",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_auxiliary_owner.AuxiliaryOwner",
        ("options",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_read_confirmation_owner.ReadConfirmationOwner",
        ("confirm", "project"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_preview_candidate.PreviewCandidate",
        ("execute",),
        "proof_writer",
        "S",
        ("X",),
        "transfer-write",
        "none",
    ),
    (
        _A + "authorization_trace_candidate.TraceCandidate",
        ("list", "detail", "operation"),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_chat_owner.ChatOwner",
        ("conversation",),
        "owner",
        "S",
        ("X",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_chat_owner.ChatOwner",
        (
            "source",
            "finalize",
            "invalidate",
            "messages",
            "list_conversations",
            "recover",
            "recover_orphan",
        ),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_lifecycle_owner.LifecycleOwner",
        ("prepare",),
        "owner",
        "S",
        ("X",),
        "import-write",
        "none",
    ),
    (
        "app.db.authorization_lifecycle_owner.LifecycleOwner",
        ("confirm",),
        "owner",
        "S",
        ("X",),
        "import-write",
        _TX + ".aggregate",
    ),
    (
        "app.db.authorization_lifecycle_owner.LifecycleOwner",
        ("decide", "unexecuted", "admit", "resume_read", "gateway_timeout", "poll"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_transfer_owner.TransferOwner",
        ("export", "preview", "cancel"),
        "owner",
        "S",
        ("X",),
        "transfer-write",
        "none",
    ),
    (
        "app.db.authorization_transfer_owner.TransferOwner",
        ("download",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_transfer_owner.TransferOwner",
        ("cleanup_page",),
        "owner",
        "S",
        ("S", "X"),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_preview_owner.PreviewOwner",
        ("receipt",),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_pending_owner.PendingOwner",
        ("reference", "prepare", "handoff", "read_handoff"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_preview_owner.PreviewOwner",
        ("preview", "gateway_preview"),
        "owner",
        "S",
        ("X",),
        "transfer-write",
        "none",
    ),
    (
        "app.db.authorization_ai_owner.AIAuthorizationOwner",
        ("expire_page", "sweep_page", "observe_cached", "orphan_cached"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_ai_owner.AIAuthorizationOwner",
        ("cutoff",),
        "owner",
        "S",
        (),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_lease_owner.LeaseOwner",
        ("renew",),
        "owner",
        "none",
        (),
        "lease-cas",
        "none",
    ),
    (
        "app.db.authorization_trace_owner.TraceOwner",
        ("list", "detail", "operation"),
        "owner",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        _S + "authorization_ai_user_input",
        ("context", "ensure_targets_in_scope", "recheck_user"),
        "read",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
    (
        _S + "authorization_ai_user_input",
        ("prepare_user",),
        "external",
        "none",
        (),
        "transfer-prepare",
        "none",
    ),
    (
        _A + "authorization_chat_model",
        ("select_model",),
        "read",
        "none",
        (),
        "transfer-prepare",
        "none",
    ),
    (
        _A + "authorization_chat_model",
        ("recheck_model",),
        "read",
        "S",
        ("S", "X"),
        "auth-read",
        "none",
    ),
    (
        _A + "authorization_completion_projection",
        ("project_completions",),
        "read",
        "S",
        ("S",),
        "auth-read",
        "none",
    ),
    (
        "app.db.authorization_transfer_cache",
        ("cache_preview", "retire_preview_hint"),
        "external",
        "none",
        (),
        "external-redis-hint",
        "none",
    ),
    (
        _TX,
        ("delete_context_fact", "delete_job_fact", "delete_business_fact"),
        "proof_writer",
        "S",
        ("X",),
        "system-write",
        "none",
    ),
)
CANDIDATES += tuple(
    Candidate(
        "completion." + qualified + "." + method,
        qualified + "." + method,
        (),
        qualified + "." + method if phase == "owner" else "caller_owned",
        phase,
        guard,
        "exact tenant; fixed owner; cleanup/expiry pages use fresh per-item proofs; lease is a fixed CAS exception",
        barriers,
        qualified + "." + method,
        qualified + "." + method,
        profile,
        epoch,
        phase == "proof_writer",
    )
    for qualified, methods, phase, guard, barriers, profile, epoch in _COMPLETION_ENTRIES
    for method in methods
)

# Composition is checked separately from ingress activation. These fixed methods
# keep the delegated lock/IO contract; none implies an HTTP route is connected.
_RUNTIME = "app.db.authorization_runtime.AuthorizationRuntime"
_RUNTIME_DELEGATES = {
    "user_read": "app.db.authorization_user_read_owner.UserReadOwner.execute",
    "control_read": "app.db.authorization_catalog_read_owner.CatalogReadOwner.execute",
    "system": _SYSTEM_OWNER,
    "import_users": _IMPORT_OWNER,
    "control": "app.db.authorization_control_owner.ControlCandidateOwner.execute",
    "authorize_control": "app.db.authorization_control_owner.ControlCandidateOwner.authorize",
    "fail_control": "app.db.authorization_control_owner.ControlCandidateOwner.complete_failure",
    "seed": _SEED_OWNER,
    "login": _AUTH + ".login",
    "current": _AUTH + ".current",
    "refresh": _AUTH + ".refresh",
    "register": _AUTH + ".register",
    "reset_password": _AUTH + ".reset",
    "bind_email": _AUTH + ".bind",
    "prepare_email": _AUTH + ".prepare_email",
    "principal_login": _PRINCIPAL_OWNER + ".login",
    "principal_current": _PRINCIPAL_OWNER + ".current",
    "authorize_principal": _PRINCIPAL_OWNER + ".authorize",
    "fail_principal": _PRINCIPAL_OWNER + ".complete_failure",
    "principal_control": _RETENTION_OWNER + ".purge",
    "create_principal": _PRINCIPAL_OWNER + ".create_first",
    "replace_principal_permissions": _PRINCIPAL_OWNER + ".replace_permissions",
}
_composition = tuple(
    replace(
        next(
            entry
            for entry in CANDIDATES
            if delegate in {entry.qualified_name, entry.owner}
        ),
        entry_id="composition." + method,
        qualified_name=_RUNTIME + "." + method,
        source=(delegate,),
        owner=_RUNTIME + "." + method,
        operation="",
        phase="owner",
        proof_only=False,
    )
    for method, delegate in _RUNTIME_DELEGATES.items()
)
MAINTENANCE_ENTRIES = MAINTENANCE_ENTRIES | frozenset(
    entry.entry_id for entry in _composition if entry.maintenance
)
CANDIDATES += _composition

PENDING_SURFACES = MappingProxyType({})
