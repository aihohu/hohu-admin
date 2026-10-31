"""Fixed authorization composition root, awaiting complete ingress migration.

Constructing this object does not switch HTTP routes or attest stopped writers.
Each call owns a new session; only the delegated owner completes its transaction.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.file_storage import get_file_storage
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_auxiliary_owner import AuxiliaryOwner
from app.db.authorization_business_owner import BusinessOwner
from app.db.authorization_catalog_read_owner import CatalogReadOwner
from app.db.authorization_chat_owner import ChatOwner
from app.db.authorization_completion_facts_owner import CompletionFactsOwner
from app.db.authorization_control_owner import control_candidate_owner
from app.db.authorization_http_audit_owner import HttpAuditOwner
from app.db.authorization_job_owner import JobOwner
from app.db.authorization_lease_owner import LeaseOwner
from app.db.authorization_lifecycle_owner import LifecycleOwner
from app.db.authorization_owner import authorization_session
from app.db.authorization_pending_owner import PendingOwner
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.db.authorization_preview_owner import PreviewOwner
from app.db.authorization_read_confirmation_owner import ReadConfirmationOwner
from app.db.authorization_reconciliation_owner import ReconciliationOwner
from app.db.authorization_seed_owner import seed_candidate_owner
from app.db.authorization_system_owner import system_candidate_owner
from app.db.authorization_trace_owner import TraceOwner
from app.db.authorization_transfer_owner import TransferOwner
from app.db.authorization_upload_owner import UploadOwner
from app.db.authorization_user_read_owner import user_read_owner
from app.modules.ai.agents.gateway.authorization_execution import PlannedGatewayOwner
from app.modules.auth.candidate_owner import auth_candidate_owner
from app.modules.platform.authorization_principal_audit import consume_principal_audit
from app.modules.platform.authorization_principal_owner import principal_candidate_owner
from app.modules.platform.authorization_retention_owner import retention_candidate_owner
from app.modules.platform.authorization_support_owner import support_candidate_owner
from app.modules.system.service.authorization_control_audit import consume_control_audit


@dataclass(frozen=True, slots=True)
class _SessionFactory:
    engine: AsyncEngine

    def __call__(self):
        return AuthorizationPlanSession(
            bind=self.engine, require_plan=True, autoflush=False, expire_on_commit=False
        )


class AuthorizationRuntime:
    """Application-owned assembly; never accepts a client-selected factory."""

    def __init__(self, engine):
        if not isinstance(engine, AsyncEngine):
            raise _order_error()
        self.sessions = _SessionFactory(engine)
        self.auxiliary = AuxiliaryOwner(self.sessions)
        self.http_audit = HttpAuditOwner(self.sessions)
        self.job = JobOwner(self.sessions)
        self.business = BusinessOwner(self.sessions)
        self.completion_facts = CompletionFactsOwner(self.sessions)
        self.reconciliation = ReconciliationOwner(self.sessions)
        self.gateway = PlannedGatewayOwner(self.sessions)
        self.lifecycle = LifecycleOwner(self.sessions)
        self.chat = ChatOwner(self.sessions)
        self.preview = PreviewOwner(self.sessions)
        self.pending = PendingOwner(self.sessions)
        self.read_confirmation = ReadConfirmationOwner(self.sessions)
        self.transfer = TransferOwner(self.sessions, get_file_storage())
        self.upload = UploadOwner(self.sessions)
        self.trace = TraceOwner(self.sessions)
        self.recovery = AIAuthorizationOwner(self.sessions)
        self.lease = LeaseOwner(self.sessions)

    async def system(self, **request):
        async with self.sessions() as db:
            return await system_candidate_owner.execute(db, **request)

    async def user_read(self, **request):
        async with authorization_session(self.sessions) as db:
            return await user_read_owner.execute(db, **request)

    async def import_users(self, **request):
        async with self.sessions() as db:
            return await system_candidate_owner.import_users(db, **request)

    async def control(self, **request):
        async with self.sessions() as db:
            return await control_candidate_owner.execute(
                db, audit_factory=self.sessions, **request
            )

    async def authorize_control(self, **request):
        async with self.sessions() as db:
            return await control_candidate_owner.authorize(db, **request)

    async def control_read(self, **request):
        return await CatalogReadOwner(self.sessions).execute(**request)

    async def fail_control(self, audit, error):
        consume_control_audit(audit)
        return await control_candidate_owner.complete_failure(
            self.sessions, audit, error
        )

    async def seed(self, **request):
        async with self.sessions() as db:
            return await seed_candidate_owner.execute(db, **request)

    async def login(self, credentials, *, host=None):
        async with self.sessions() as db:
            return await auth_candidate_owner.login(db, credentials, host=host)

    async def current(self, token):
        async with self.sessions() as db:
            return await auth_candidate_owner.current(db, token)

    async def refresh(self, token):
        async with self.sessions() as db:
            return await auth_candidate_owner.refresh(db, token)

    async def register(self, tenant_id, payload):
        async with self.sessions() as db:
            return await auth_candidate_owner.register(db, tenant_id, payload)

    async def reset_password(self, tenant_id, payload):
        async with self.sessions() as db:
            return await auth_candidate_owner.reset(db, tenant_id, payload)

    async def bind_email(self, token, payload):
        async with self.sessions() as db:
            return await auth_candidate_owner.bind(db, token, payload)

    async def prepare_email(self, tenant_id, payload, *, ip, token=None):
        async with self.sessions() as db:
            return await auth_candidate_owner.prepare_email(
                db, tenant_id, payload, ip=ip, token=token
            )

    async def principal_login(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.login(db, **request)

    async def principal_current(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.current(db, **request)

    async def authorize_principal(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.authorize(db, **request)

    async def fail_principal(self, audit, error):
        consume_principal_audit(audit)
        return await principal_candidate_owner.complete_failure(
            self.sessions, audit, error
        )

    async def principal_control(self, *, operation, **request):
        async with self.sessions() as db:
            if operation in {"support.operation_logs", "support.login_logs"}:
                return await support_candidate_owner.execute(
                    db, operation=operation, audit_factory=self.sessions, **request
                )
            if operation in {"retention.preview", "retention.purge"}:
                method = (
                    retention_candidate_owner.preview
                    if operation == "retention.preview"
                    else retention_candidate_owner.purge
                )
                return await method(db, audit_factory=self.sessions, **request)
            raise _order_error()

    async def create_principal(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.create_first(db, **request)

    async def replace_principal_permissions(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.replace_permissions(
                db, audit_factory=self.sessions, **request
            )
