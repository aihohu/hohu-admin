"""AI 工具 ``user.export`` 行为测试。

同步导出始终创建 ExportTask，并将 result_view 从
rows_affected 升级为 detail_card，携带 downloadUrl / fileSize / expiresAt，
让前端 DetailCardView 渲染「下载」按钮（AI 对话内闭环）。

覆盖：
- result_view == "detail_card"
- view_data 含 downloadUrl / fileSize / expiresAt / rowCount / exportId
- data（LLM 视角）含 exportId + rowCount + downloadUrl
- downloadUrl 路径与 GET /export/{export_id}/download 端点一致
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.file_storage import MockFileStorage
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.ai_tools import user_export
from app.modules.system.user.constants import ExportTaskStatus
from app.modules.system.user.models import UserExportTask


def _make_ctx(db: AsyncSession) -> AiToolContext:
    """最小 AiToolContext：超管视角（无 data_scope filter）。"""
    data_scope = DataScopeContext(
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
        user=MagicMock(user_id=1),
        perms={"system:user:export"},
        db=db,
        data_scope=data_scope,
        trace_id="tr_export_test",
        tool_meta=meta,
    )


@pytest.fixture
def file_storage() -> MockFileStorage:
    return MockFileStorage()


class TestUserExportDetailCard:
    """user_export 返回 detail_card 和 downloadUrl。"""

    async def test_result_view_is_detail_card(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """result_view 从 rows_affected → detail_card（spec line 2619）。"""
        # Mock get_file_storage 让 export_users_to_excel 用 MockFileStorage
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export detail card")

        assert result.ui is not None
        assert result.ui.view_type == "detail_card"

    async def test_view_data_contains_download_url(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """view_data 含 downloadUrl 指向 GET /export/{export_id}/download。"""
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export download url")

        view_data = result.ui.view_data
        assert "downloadUrl" in view_data
        export_id = result.data["exportId"]
        # 路径模板：/system/user/export/{export_id}/download
        assert view_data["downloadUrl"] == f"/system/user/export/{export_id}/download"

    async def test_view_data_contains_file_size_and_expiry(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """view_data 含 fileSize / expiresAt（spec line 1626）。"""
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export metadata")

        view_data = result.ui.view_data
        assert "fileSize" in view_data
        assert isinstance(view_data["fileSize"], int)
        assert view_data["fileSize"] > 0
        assert "expiresAt" in view_data
        # ISO 8601 字符串
        assert isinstance(view_data["expiresAt"], str)
        assert "T" in view_data["expiresAt"]

    async def test_data_carries_export_id_and_download_url(
        self, db_session: AsyncSession, file_storage: MockFileStorage, monkeypatch
    ) -> None:
        """data（LLM 视角）含 exportId + rowCount + downloadUrl。

        LLM 引导用户点击下载时需要 downloadUrl 字面量；
        rowCount 已在 data 中，保持现有兼容行为。
        """
        monkeypatch.setattr(
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
        result = await user_export(ctx, reason="QA export data")

        assert "exportId" in result.data
        assert "rowCount" in result.data
        assert "downloadUrl" in result.data
        assert result.data["downloadUrl"].endswith("/download")


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
            "app.modules.system.user.export_service.get_file_storage",
            lambda: file_storage,
        )
        ctx = _make_ctx(db_session)
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
        assert task.operator_id == 1  # _make_ctx 的 MagicMock user_id=1
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
