"""Reuse the system module's outer-transaction db_session fixture (full rollback)."""

from tests.modules.system.conftest import db_session  # noqa: F401
