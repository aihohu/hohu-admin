"""Track request reads and permanently close their SQL boundary on handoff."""

from sqlalchemy import event
from sqlalchemy.sql import Select, visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import CompoundSelect

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error


class RequestSession(AuthorizationPlanSession):
    def __init__(self, *args, **kwargs):
        if "require_plan" in kwargs:
            raise _order_error()
        super().__init__(*args, require_plan=True, **kwargs)
        self._install_request_tracking()

    def _install_request_tracking(self):
        self._request_released = False
        self._request_wrote = False
        event.listen(self.sync_session, "after_begin", self._observe_connection)
        event.listen(self.sync_session, "after_transaction_create", self._check_open)
        event.listen(self.sync_session, "before_commit", self._check_open)
        event.listen(self.sync_session, "do_orm_execute", self._check_open)
        event.listen(self.sync_session, "before_flush", self._record_flush)

    def _check_open(self, *_args):
        if self._request_released:
            raise _order_error()

    async def _observed_io(self, method, *args, **kwargs):
        try:
            return await super()._observed_io(method, *args, **kwargs)
        except BusinessException as error:
            if error.error_code == "FIELD_POLICY_LOCK_ORDER":
                self._request_wrote = True
            raise

    def begin(self):
        self._check_open()
        return super().begin()

    async def commit(self):
        self._check_open()
        return await super().commit()

    async def flush(self, objects=None):
        self._check_open()
        return await super().flush(objects)

    async def connection(self, *args, **kwargs):
        self._check_open()
        return await super().connection(*args, **kwargs)

    def _record_flush(self, *_args):
        self._check_open()
        self._request_wrote = True

    def _observe_connection(self, _session, _transaction, connection):
        event.listen(connection, "before_cursor_execute", self._record_sql)

    def _record_sql(self, _connection, _cursor, _sql, _parameters, context, _many):
        self._check_open()
        statement = context.compiled.statement if context.compiled else None
        if not isinstance(statement, (Select, CompoundSelect)) or any(
            isinstance(node, (TextClause, UpdateBase))
            or (isinstance(node, Select) and node._for_update_arg is not None)
            for node in visitors.iterate(statement)
        ):
            self._request_wrote = True


def request_reads_released(session):
    return isinstance(session, RequestSession) and session._request_released
