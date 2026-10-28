"""Fixed authorization composition root, awaiting complete ingress migration.

Constructing this object does not switch HTTP routes or attest stopped writers.
Each call owns a new session; only the delegated owner completes its transaction.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.file_storage import get_file_storage
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_chat_owner import ChatOwner
from app.db.authorization_control_owner import control_candidate_owner
from app.db.authorization_lease_owner import LeaseOwner
from app.db.authorization_lifecycle_owner import LifecycleOwner
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.db.authorization_preview_owner import PreviewOwner
from app.db.authorization_seed_owner import seed_candidate_owner
from app.db.authorization_system_owner import system_candidate_owner
from app.db.authorization_trace_owner import TraceOwner
from app.db.authorization_transfer_owner import TransferOwner
from app.modules.ai.agents.gateway.authorization_execution import PlannedGatewayOwner
from app.modules.auth.candidate_owner import auth_candidate_owner
from app.modules.platform.authorization_principal_owner import principal_candidate_owner


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
        self.gateway = PlannedGatewayOwner(self.sessions)
        self.lifecycle = LifecycleOwner(self.sessions)
        self.chat = ChatOwner(self.sessions)
        self.preview = PreviewOwner(self.sessions)
        self.transfer = TransferOwner(self.sessions, get_file_storage())
        self.trace = TraceOwner(self.sessions)
        self.recovery = AIAuthorizationOwner(self.sessions)
        self.lease = LeaseOwner(self.sessions)

    async def system(self, **request):
        async with self.sessions() as db:
            return await system_candidate_owner.execute(db, **request)

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

    async def create_principal(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.create_first(db, **request)

    async def replace_principal_permissions(self, **request):
        async with self.sessions() as db:
            return await principal_candidate_owner.replace_permissions(
                db, audit_factory=self.sessions, **request
            )
