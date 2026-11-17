"""Live source retirement is checked even when it does not advance G/B epochs."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.auth.service import access_identity
from tests.fixture_session import FixtureSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_authorization_file_parse import invoke
from tests.modules.ai.test_authorization_file_parse import parse_case as parse_case
from tests.modules.ai.test_authorization_file_parse import preview_case as preview_case


async def test_probe_refuses_a_file_retired_by_its_real_owner_without_epoch_change(
    parse_case,
):
    result = await invoke(parse_case)
    proof = result.projection.field_authority
    identity = access_identity(parse_case.deps.user)
    runtime = AuthorizationRuntime(engine)
    request = {
        "tenant": identity.tenant,
        "actor_user_version": identity.user_version,
        "header": proof["authority"],
        "receipt": proof,
    }
    assert (await runtime.chat.delivery_probe(**request))["unchanged"]
    assert (
        await runtime.upload.delete(
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            identifiers=(parse_case.file_id,),
        )
        == 1
    )
    assert not (await runtime.chat.delivery_probe(**request))["unchanged"]


@pytest.mark.parametrize("mutation", ["inactive", "parent_deleted"])
async def test_probe_checks_original_source_without_epoch_change(parse_case, mutation):
    proof = (await invoke(parse_case)).projection.field_authority
    identity = access_identity(parse_case.deps.user)
    runtime = AuthorizationRuntime(engine)
    request = {
        "tenant": identity.tenant,
        "actor_user_version": identity.user_version,
        "header": proof["authority"],
        "receipt": proof,
    }
    assert (await runtime.chat.delivery_probe(**request))["unchanged"]
    async with FixtureSessionLocal() as db, db.begin():
        if mutation == "inactive":
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == parse_case.deps.source_user_message_id)
                .values(is_active=False)
            )
        else:
            await db.execute(
                update(AiConversation)
                .where(
                    AiConversation.conversation_id == parse_case.deps.conversation_id
                )
                .values(deleted_at=datetime.now(UTC))
            )
    assert not (await runtime.chat.delivery_probe(**request))["unchanged"]
