"""Detached preview errors require current sources; errors never prove a commit."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.core.authorization_io import file_transform_worker
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.modules.ai.agents.gateway import preview_execution
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.service.authorization_preview_candidate import read_preview_input
from app.modules.ai.service.delivery_source_domains import with_sources
from app.modules.auth.service import access_identity
from app.modules.system.models.file import File
from tests.fixture_session import FixtureSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_authorization_file_parse import parse_case as parse_case
from tests.modules.ai.test_authorization_file_parse import preview_case as preview_case
from tests.modules.ai.test_field_authority import evidence


async def test_preview_input_bytes_match_original_file_manifest(parse_case):
    identity = access_identity(parse_case.deps.user)
    snapshot = await AuthorizationRuntime(engine).preview._read(
        "input",
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        file_id=parse_case.file_id,
    )
    path = Path(dict(snapshot.facts)["file_path"])
    original = await asyncio.to_thread(path.read_bytes)
    try:
        await asyncio.to_thread(path.write_bytes, b"user_name,status\nchangedrow,1\n")
        with pytest.raises(BusinessException):
            await asyncio.to_thread(
                read_preview_input, snapshot, str(settings.PRIVATE_UPLOAD_DIR)
            )
    finally:
        await asyncio.to_thread(path.write_bytes, original)


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("stage", ["read_preview_input", "prepare_preview"])
async def test_preview_worker_failure_revalidates_file_and_carries_sources(
    parse_case, monkeypatch, retired, stage
):
    original = file_transform_worker.run

    async def fail(function, *args, **kwargs):
        if function.__name__ != stage:
            return await original(function, *args, **kwargs)
        if retired:
            async with FixtureSessionLocal() as db, db.begin():
                await db.execute(
                    update(File)
                    .where(File.file_id == parse_case.file_id)
                    .values(del_flag="2")
                )
        raise BusinessException(
            400, "SOURCE-DERIVED-HEADER", error_code="IMPORT_CONTENT_ERROR"
        )

    monkeypatch.setattr(file_transform_worker, "run", fail)
    identity = access_identity(parse_case.deps.user)
    with pytest.raises(BusinessException) as caught:
        await AuthorizationRuntime(engine).preview.preview(
            file_id=parse_case.file_id,
            reason="owned test",
            requested_outcome="preview_only",
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            conversation_id=parse_case.deps.conversation_id,
            source_id=parse_case.deps.source_user_message_id,
            trace_id=parse_case.deps.trace_id,
        )
    if retired:
        assert caught.value.error_code != "IMPORT_CONTENT_ERROR"
    else:
        assert caught.value.error_code == "IMPORT_CONTENT_ERROR"
        proof = getattr(caught.value, "_ai_delivery_authority", None)
        assert proof is not None and proof["source_complete"] is True
        assert any(
            g.get("kind") == "file"
            and g["file_id"] == str(parse_case.file_id)
            and {"content", "original_name"} <= set(g["read"])
            for g in proof["source_domains"]
        )


async def test_error_source_receipt_does_not_prove_a_preview_commit(monkeypatch):
    error = BusinessException(
        400, "SOURCE-DERIVED-HEADER", error_code="IMPORT_CONTENT_ERROR"
    )
    error._ai_delivery_authority = with_sources(
        evidence(), ({"kind": "input", "read": ["content"]},)
    )
    preview = SimpleNamespace(
        gateway_preview=AsyncMock(side_effect=error),
        receipt=AsyncMock(side_effect=BusinessException(409, "No proved receipt")),
    )
    monkeypatch.setattr(
        preview_execution,
        "get_authorization_runtime",
        lambda: SimpleNamespace(preview=preview),
    )
    monkeypatch.setattr(preview_execution, "_request", lambda *_args, **_kwargs: {})
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await preview_execution.execute_preview(
            SimpleNamespace(meta=SimpleNamespace(name="user.import_preview")),
            {},
            object(),
            log_id=1,
            tool_call_id="call",
            args_hash="a" * 64,
        )
    assert preview.receipt.await_count == 1
