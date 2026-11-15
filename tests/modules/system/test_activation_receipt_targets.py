"""A request receipt cannot authorize a different current manifest target."""

import pytest

from app.core.exceptions import BusinessException
from tests.modules.system.test_generic_activation_management import (
    admin_sessions as admin_sessions,
)
from tests.modules.system.test_generic_activation_management import (
    generic_sessions as generic_sessions,
)
from tests.modules.system.test_generic_activation_management import (
    managed as managed,
)
from tests.modules.system.test_generic_activation_management import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_generic_activation_management import (
    policies as policies,
)
from tests.modules.system.test_generic_activation_management import (
    policy_sessions as policy_sessions,
)
from tests.modules.system.test_generic_activation_management import (
    setup_candidate,
    target,
    transition,
)
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_generic_policy_transitions import command


@pytest.mark.parametrize("operation", ["ready", "activate"])
async def test_prior_request_id_cannot_be_rebound_to_a_new_manifest(
    managed, tmp_path, monkeypatch, operation
):
    await setup_candidate(managed, tmp_path, monkeypatch)
    first_id, payload = await target(managed)
    if operation == "activate":
        await transition(managed, "ready", first_id, payload)
        first_id, payload = await target(managed)
    first = await transition(managed, operation, first_id, payload)
    assert await transition(managed, operation, first_id, payload) == first
    await run(managed, command("build", int(first["headVersion"])))
    second_id, current = await target(managed)
    assert second_id != first_id
    if operation == "activate":
        await transition(managed, "ready", second_id, current)
        second_id, current = await target(managed)
    rebound = payload | {"candidateManifestDigest": current["candidateManifestDigest"]}
    with pytest.raises(BusinessException) as error:
        await transition(managed, operation, second_id, rebound)
    assert error.value.error_code == "FIELD_POLICY_REQUEST_CONFLICT"
