"""Behavior tests for the ``user.export`` AI tool.

The synchronous path always creates an ExportTask and exposes the authorized
download URL only through UI data. The LLM-facing payload must never contain a
bearer download token.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException
from app.core.file_storage import MockFileStorage
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.system.ai_tools import user_export
from app.modules.system.constants import ExportTaskStatus
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask
from tests.tenant_helpers import bind_test_user


def test_user_export_always_requires_hitl() -> None:
    """Exporting personal data must never become autonomous by row count."""
    meta = user_export.__ai_tool_meta__

    assert meta.risk == "high"
    assert meta.dry_run_supported is True
    assert meta.hitl_always is True


async def _make_ctx(db: AsyncSession) -> AiToolContext:
    """Build the minimum unrestricted data-scope context for export tests."""
    actor = await db.scalar(
        select(User).where(User.tenant_id == 0, User.user_name == "admin")
    )
    assert actor is not None
    tenant = bind_test_user(actor)
    data_scope = DataScopeContext(
        tenant=tenant,
        accessible_dept_ids=None,
        accessible_user_scope=None,
        filters=[],
    )
    meta = AiToolMeta(
        name="user.export",
        agent="user_mgmt",
        summary="导出用户",
        risk="low",
        required_perms=("system:user:export",),
        allowed_filters=("status",),
    )
    return AiToolContext(
        user=actor,
        perms={"system:user:export"},
        db=db,
        data_scope=data_scope,
        trace_id="tr_export_test",
        tool_meta=meta,
        tenant=tenant,
        data_scope_hash="scope-hash",
    )


@pytest.fixture
def file_storage() -> MockFileStorage:
    return MockFileStorage()


@pytest.fixture(autouse=True)
def authorized_download_token(monkeypatch) -> None:
    """Isolate export rendering from the projection policy's dedicated tests."""
    monkeypatch.setattr(
        result_projection_service,
        "authorize_result_projection",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        result_projection_service,
        "issue_download_token",
        AsyncMock(return_value="signed-token"),
    )


class TestUserExportDetailCard:
    """user_export 返回 detail_card 和 downloadUrl。"""

    async def test_result_view_is_detail_card(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """result_view 从 rows_affected → detail_card（spec line 2619）。"""
        # Mock get_file_storage 让 export_users_to_excel 用 MockFileStorage
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = await _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export detail card")

        assert result.ui is not None
        assert result.ui.view_type == "detail_card"

    async def test_view_data_contains_download_url(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """view_data 含 downloadUrl 指向 GET /export/{export_id}/download。"""
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = await _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export download url")

        view_data = result.ui.view_data
        assert "downloadUrl" in view_data
        export_id = result.data["exportId"]
        assert view_data["downloadUrl"] == (
            f"/ai/download/user-export/{export_id}?token=signed-token"
        )

    async def test_view_data_contains_file_size_and_expiry(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """view_data 含 fileSize / expiresAt（spec line 1626）。"""
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = await _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export metadata")

        view_data = result.ui.view_data
        assert "fileSize" in view_data
        assert isinstance(view_data["fileSize"], int)
        assert view_data["fileSize"] > 0
        assert "expiresAt" in view_data
        # ISO 8601 字符串
        assert isinstance(view_data["expiresAt"], str)
        assert "T" in view_data["expiresAt"]

    async def test_llm_data_excludes_download_bearer_token(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """Keep the bearer URL in UI data and out of the LLM prompt payload."""
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = await _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export data")

        assert "exportId" in result.data
        assert "rowCount" in result.data
        assert result.data["downloadReady"] is True
        assert "downloadUrl" not in result.data
        assert "signed-token" not in str(result.data)

    async def test_projection_denial_happens_before_export_side_effects(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """A denied projection must not create a task or write an export file."""
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        authorize = AsyncMock(return_value=False)
        issue = AsyncMock(return_value="must-not-be-issued")
        monkeypatch.setattr(
            result_projection_service,
            "authorize_result_projection",
            authorize,
        )
        monkeypatch.setattr(
            result_projection_service,
            "issue_download_token",
            issue,
        )

        ctx = await _make_ctx(db_session)
        with pytest.raises(AuthorizationException) as exc_info:
            await user_export(ctx, reason="QA denied projection")

        assert getattr(exc_info.value, "error_code", None) == (
            "AI_RESULT_PROJECTION_FORBIDDEN"
        )
        assert file_storage._store == {}
        issue.assert_not_awaited()
        task_count = await db_session.scalar(
            select(func.count(UserExportTask.export_id)).where(
                UserExportTask.tenant_id == 0,
                UserExportTask.operator_id == ctx.user.user_id,
                UserExportTask.reason == "QA denied projection",
            )
        )
        assert task_count == 0

    async def test_late_projection_revocation_removes_the_uncommitted_export_file(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """Compensate the file write when authorization changes during export."""
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        monkeypatch.setattr(
            result_projection_service,
            "issue_download_token",
            AsyncMock(return_value=None),
        )

        ctx = await _make_ctx(db_session)
        with pytest.raises(AuthorizationException) as exc_info:
            await user_export(ctx, reason="QA denied projection")

        assert getattr(exc_info.value, "error_code", None) == (
            "AI_RESULT_PROJECTION_FORBIDDEN"
        )
        assert file_storage._store == {}


class TestAlwaysCreatesTask:
    """同步 AI 导出也必须形成完整审计链。
    AI tool `user.export` 任何行数都建 ExportTask。

    防止「HR 凌晨通过 AI 导出 1 行员工数据，事后无 DB 记录可追溯」。
    与 HTTP 路径共用 export_users_to_excel service（决策 27.1），但
    service 层测试覆盖的是直接调用入口，本类显式断言 AI tool 入口
    也走同一条建 task 路径。
    """

    async def test_ai_export_always_creates_task(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """AI tool 入口导出 → DB 有 ExportTask 行 + status=SUCCESS + 审计字段齐。

        即使同步路径只有一行，也必须创建任务。
        """
        monkeypatch.setattr(
            "app.modules.system.service.user_export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = await _make_ctx(db_session)
        result = await user_export(ctx, reason="QA always creates task")

        export_id = result.data["exportId"]
        row_count = result.data["rowCount"]

        # 显式用 export_id 反查 DB（不假设全表只 1 行）
        task = (
            await db_session.execute(
                select(UserExportTask).where(UserExportTask.export_id == export_id)
            )
        ).scalar_one()

        # 审计字段齐全。
        assert task.status == ExportTaskStatus.SUCCESS
        assert task.row_count == row_count
        assert task.reason == "QA always creates task"
        assert task.operator_id == ctx.user.user_id
        assert task.file_storage_key is not None
        assert task.file_size_bytes is not None
        assert task.file_size_bytes > 0
        # filter_snapshot 冻结导出时条件。
        assert "accessible_dept_ids" in task.filter_snapshot
        assert "filter_evaluated_at" in task.filter_snapshot
        # 时间戳链路完整
        assert task.started_at is not None
        assert task.finished_at is not None
        assert task.duration_ms is not None
