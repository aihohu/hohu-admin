"""Legacy adapters remain closed when a future global barrier opens new modes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.service.user_field_compatibility import (
    require_legacy_field_policy,
)


@pytest.mark.parametrize(
    "mode,ever,digest",
    [
        ("enforcing", False, None),
        ("frozen", False, None),
        ("legacy", True, None),
        ("ready", False, "active"),
    ],
)
async def test_compatibility_adapter_rejects_active_or_irreversible_state(
    mode, ever, digest
):
    result = Mock()
    result.one_or_none.return_value = SimpleNamespace(
        mode=mode,
        ever_enforced=ever,
        active_manifest_digest=digest,
    )
    db = AsyncMock()
    db.execute.return_value = result
    tx = Mock(spec=["assert_session", "assert_planning"])
    with pytest.raises(BusinessException) as error:
        await require_legacy_field_policy(db, tx, tenant_id=7)
    assert error.value.error_code == "FIELD_POLICY_UNAVAILABLE"
    tx.assert_session.assert_called_once_with(db)
    tx.assert_planning.assert_called_once_with(7)
