"""Explicit test-data factory; production imports must never depend on this file.

Legacy service unit tests need ordinary mutations to create and roll back their
owned fixtures. The real application factory is not patched: HTTP dependencies,
startup and fixed transaction owners keep their production session boundaries.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.request_session import RequestSession
from app.db.session import engine


class FixtureSession(RequestSession):
    def __init__(self, *args, **kwargs):
        AuthorizationPlanSession.__init__(self, *args, require_plan=False, **kwargs)
        self._install_request_tracking()

    def begin(self):
        self._check_open()
        return AsyncSession.begin(self)

    def begin_nested(self):
        self._check_open()
        return AsyncSession.begin_nested(self)

    async def delete(self, *args, **kwargs):
        self._check_open()
        return await AsyncSession.delete(self, *args, **kwargs)

    async def merge(self, *args, **kwargs):
        self._check_open()
        return await AsyncSession.merge(self, *args, **kwargs)

    async def run_sync(self, *args, **kwargs):
        self._check_open()
        return await AsyncSession.run_sync(self, *args, **kwargs)

    async def stream(self, *args, **kwargs):
        self._check_open()
        return await AsyncSession.stream(self, *args, **kwargs)

    async def stream_scalars(self, *args, **kwargs):
        self._check_open()
        return await AsyncSession.stream_scalars(self, *args, **kwargs)

    async def connection(self, *args, **kwargs):
        self._check_open()
        return await AsyncSession.connection(self, *args, **kwargs)

    def get_transaction(self):
        return AsyncSession.get_transaction(self)

    def get_nested_transaction(self):
        return AsyncSession.get_nested_transaction(self)


FixtureSessionLocal = async_sessionmaker(
    engine, class_=FixtureSession, expire_on_commit=False, autoflush=False
)
