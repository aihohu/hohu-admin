"""FILE tools bind publication to the original claim and completion owner."""

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update

from app.core import file_storage
from app.core.authorization_plan import LockMode, TenantLock
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.file_storage import ArtifactExecution, LocalFileStorage
from app.db.authorization_lifecycle_owner import LifecycleOwner
from app.db.authorization_preview_owner import PreviewOwner
from app.db.authorization_transfer_owner import TransferOwner
from app.db.base import role_menus
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import ClaimBinding
from app.modules.ai.service.authorization_execution_candidate import (
    ai_execution_candidate,
)
from app.modules.ai.service.preview_completion_service import verify_historical_preview
from app.modules.system.models.dept import Dept
from app.modules.system.models.file import File
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_transfer_artifact_service import batch_snapshot
from tests.modules.ai.test_authorization_chat_lifecycle import (
    chat_model_case as chat_model_case,
)
from tests.modules.ai.test_authorization_gateway_execution import execute, rebind
from tests.modules.ai.test_authorization_gateway_execution import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.system.test_authorization_system_candidate import epoch
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize("outcome", ["execute_if_approved", "preview_only"])
async def test_preview_creates_bound_handoff_and_verified_history(
    chat_model_case, tmp_path, monkeypatch, outcome
):

    factory, plain, case = chat_model_case
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path))
    data = b"user_name,dept_input,status\npreviewuser,Team,1\n"
    (tmp_path / "source.csv").write_bytes(data)
    storage = LocalFileStorage(tmp_path / "owned")
    monkeypatch.setattr(file_storage, "get_file_storage", lambda: storage)
    async with plain() as db:
        await db.execute(update(AiAgent).values(code="user_mgmt"))
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=303,
                role_name="Member",
                role_code="R_USER",
                status="1",
                data_scope="5",
            )
        )
        await db.execute(
            insert(SystemSetting).values(
                tenant_id=0,
                setting_key="auth:default_password",
                setting_value="Continuation876!",
                status="1",
            )
        )
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=301, dept_name="Team", ancestors="0", status="1"
            )
        )
        await db.execute(
            insert(File).values(
                tenant_id=0,
                file_id=501,
                owner_user_id=101,
                original_name="source.csv",
                file_name="source.csv",
                file_path=str(tmp_path / "source.csv"),
                file_url="/private/source.csv",
                file_size=len(data),
                file_ext=".csv",
                mime_type="text/csv",
                business_type="user-import",
                del_flag="0",
            )
        )
        for index, permission in enumerate(
            ("system:user:import", "system:dept:list"), 9003
        ):
            existing = await db.scalar(
                select(Menu.menu_id).where(Menu.permission == permission)
            )
            if existing is None:
                await db.execute(
                    insert(Menu).values(
                        tenant_id=0,
                        menu_id=index,
                        menu_name=permission,
                        menu_type="F",
                        status="1",
                        permission=permission,
                    )
                )
                await db.execute(
                    insert(role_menus).values(tenant_id=0, role_id=201, menu_id=index)
                )
        await db.commit()
    result = await PreviewOwner(factory).preview(
        file_id="501",
        reason="onboarding",
        requested_outcome=outcome,
        conversation_id=case.claim.conversation_id,
        source_id=case.claim.source_user_message_id,
        trace_id=case.claim.trace_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    assert (
        bool(result.confirmation_id) == (outcome == "execute_if_approved")
        and result.batch_id
    )
    async with plain() as db:
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.tool_name == "user.import_preview"
            )
        )
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.confirmation_id == result.confirmation_id
            )
        )
        audit = await verify_historical_preview(
            db, log, case.conversation, case.source, tenant_id=0, user_id=101
        )
        assert audit["batch_id"] == result.batch_id and await epoch(db) == 1
        if outcome == "execute_if_approved":
            assert (
                action.prepare_tool_call_id == log.tool_call_id
                and action.status == "pending_confirmation"
            )
        else:
            assert action is None
    if result.confirmation_id:
        assert (
            await LifecycleOwner(factory).confirm(
                result.confirmation_id, tenant=case.tenant, actor_user_version=1
            )
        ).disposition == "succeeded"
        async with plain() as db:
            assert (
                await db.scalar(
                    select(User.user_id).where(User.user_name == "previewuser")
                )
                is not None
            )
            assert await epoch(db) == 2


async def test_claim_export_publishes_once(execution_case, tmp_path, monkeypatch):
    factory, plain, case = execution_case
    storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr(file_storage, "get_file_storage", lambda: storage)
    async with plain() as db:
        await db.execute(update(AiAgent).values(code="user_mgmt"))
        await db.execute(
            update(AiPreparedAction).values(
                execution_owner="v0162-file:" + str(uuid4())
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9003,
                menu_name="export",
                menu_type="F",
                status="1",
                permission="system:user:export",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9003)
        )
        await rebind(db, case, "user.export", "user_mgmt", {"reason": "audit"}, None)
    assert (await execute(factory, case)).disposition == "succeeded"
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        tasks = list(await db.scalars(select(UserExportTask)))
        assert len(tasks) == 1 and tasks[0].status.value == "SUCCESS"
        identity = await storage.artifact_identity(tasks[0].file_storage_key)
        assert identity.operation_id == str(case.claim.action_id)
        assert identity.execution_owner == case.claim.execution_owner
        assert await epoch(db) == 1
        action = await db.scalar(select(AiPreparedAction))

        def tokens(value):
            if isinstance(value, dict):
                return [token for child in value.values() for token in tokens(child)]
            if isinstance(value, list):
                return [token for child in value for token in tokens(child)]
            if isinstance(value, str) and "/ai/download/user-export/" in value:
                return parse_qs(urlsplit(value).query).get("token", [])
            return []

        token = tokens(action.result_ui)[0]
        export_id = tasks[0].export_id
    owner = TransferOwner(factory, storage)
    with pytest.raises(BusinessException):
        await owner.download(
            tenant=case.tenant,
            actor_user_version=1,
            kind="export",
            identifier=export_id,
        )
    data, _ = await owner.download(
        tenant=case.tenant,
        actor_user_version=1,
        kind="export",
        identifier=export_id,
        token=token,
    )
    assert data[:2] == b"PK"


async def test_claim_import_uses_preview_source(execution_case, tmp_path, monkeypatch):

    factory, plain, case = execution_case
    storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr(file_storage, "get_file_storage", lambda: storage)
    async with plain() as db:
        await db.execute(update(AiAgent).values(code="user_mgmt"))
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=303,
                role_name="Member",
                role_code="R_USER",
                status="1",
                data_scope="5",
            )
        )
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=301, dept_name="Team", ancestors="0", status="1"
            )
        )
        await db.execute(
            insert(SystemSetting).values(
                tenant_id=0,
                setting_key="auth:default_password",
                setting_value="Continuation876!",
                status="1",
            )
        )
        for index, permission in enumerate(
            ("system:user:import", "system:dept:list"), 9003
        ):
            existing = await db.scalar(
                select(Menu.menu_id).where(Menu.permission == permission)
            )
            if existing is None:
                await db.execute(
                    insert(Menu).values(
                        tenant_id=0,
                        menu_id=index,
                        menu_name=permission,
                        menu_type="F",
                        status="1",
                        permission=permission,
                    )
                )
                await db.execute(
                    insert(role_menus).values(tenant_id=0, role_id=201, menu_id=index)
                )
        await db.commit()
    preview = await TransferOwner(factory, storage).preview(
        tenant=case.tenant,
        actor_user_version=1,
        file_bytes=b"user_name,dept_input,status\nimportnew,Team,1\n",
        filename="input.csv",
        reason="audit",
    )
    async with plain() as db:
        snapshot = batch_snapshot(await db.get(UserImportBatch, preview.resource_id))
        await db.execute(
            update(AiPreparedAction).values(
                execution_owner="v0162-file:" + str(uuid4()),
                interaction_flow="prepared",
                requested_outcome="execute_if_approved",
            )
        )
        await rebind(
            db,
            case,
            "user.import_execute",
            "user_mgmt",
            {
                "preview_token": preview.preview_token,
                "reason": "audit",
                "on_conflict": "skip",
                "sync_mode": "CREATE_ONLY",
            },
            snapshot,
        )
        await db.execute(
            update(AiPreparedAction).values(
                snapshot=snapshot,
                snapshot_hash=compute_args_hash(snapshot),
                subject_ref={"type": "user_import_batch", "id": preview.resource_id},
            )
        )
        await db.commit()
        action = await db.scalar(
            select(AiPreparedAction).execution_options(populate_existing=True)
        )
        case.claim = ClaimBinding.from_action(action, case.log.log_id)

    async with factory() as db:
        command = await system_ai_tool_adapter.prepare_input(
            db,
            tool="user.import_execute",
            args={
                "preview_token": preview.preview_token,
                "reason": "audit",
                "on_conflict": "skip",
                "sync_mode": "CREATE_ONLY",
            },
            snapshot=snapshot,
            tenant=case.tenant,
            actor_user_version=1,
            execution=ArtifactExecution(
                0, str(case.claim.action_id), case.claim.execution_owner
            ),
        )
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            assert (
                await ai_execution_candidate.execute(
                    db,
                    tx,
                    command,
                    case.claim,
                    tenant=case.tenant,
                    actor_user_version=1,
                )
            ).disposition == "succeeded"
        await db.rollback()
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        assert await db.scalar(
            select(User.user_id).where(User.user_name == "importnew")
        )
        assert (
            await db.get(UserImportBatch, preview.resource_id)
        ).status.value == "SUCCESS"
        assert await epoch(db) == 2
