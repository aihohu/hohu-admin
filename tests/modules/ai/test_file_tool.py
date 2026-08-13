"""file.parse AI Tool 集成测试。

测试 tool 注册 / meta 字段 / 端到端调用（真实文件 + DB）。

db_session fixture 用 SAVEPOINT 回滚，sys_file 插入不真正落库。
tmp_path 写真实 .csv / .xlsx 文件，测完自动清理。
"""

# ruff: noqa: ARG001, PLC0415

import asyncio
import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.file_parser import SUPPORTED_MIME_TYPES
from app.modules.ai.agents.tools.file_tools import file_parse
from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE, AiToolMeta
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.core.context import AiToolContext, DataScopeContext
from app.modules.system.models.file import File

# ============ fixture ============


_FILE_ID = 9001


def _make_ctx(db: AsyncSession, *, meta: AiToolMeta | None = None) -> AiToolContext:
    """构造 AiToolContext，tool_meta 默认用 file.parse 真实 meta"""
    if meta is None:
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None, "file.parse 未注册，先调 load_builtin_tools()"
        meta = reg.meta
    data_scope = DataScopeContext(
        accessible_dept_ids=None,
        accessible_user_scope=None,
        filters=[],
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms=set(),
        db=db,
        data_scope=data_scope,
        trace_id="tr_test_file",
        tool_meta=meta,
        tenant_id=0,
    )


async def _add_file_record(
    db: AsyncSession,
    *,
    file_id: int = _FILE_ID,
    file_path: str,
    mime_type: str,
) -> File:
    path = Path(file_path)
    file_size = (await asyncio.to_thread(path.stat)).st_size
    file_record = File(
        file_id=file_id,
        original_name="test.csv",
        file_name=str(file_id),
        file_path=file_path,
        file_url=f"/uploads/{file_id}",
        file_size=file_size,
        file_ext=path.suffix,
        mime_type=mime_type,
        business_type="ai-chat",
        owner_user_id=1,
        tenant_id=0,
        del_flag="0",
    )
    db.add(file_record)
    await db.flush()
    return file_record


def _make_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


@pytest.fixture(autouse=True)
def public_upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ai-chat files are resolved below the public upload root."""
    load_builtin_tools()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))


# ============ Registry / meta ============


class TestFileParseRegistry:
    """file.parse 注册到 Registry 且元数据完整。"""

    def test_tool_registered(self) -> None:
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.name == "file.parse"

    def test_meta_agent_is_shared(self) -> None:
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.agent == SHARED_AGENT_CODE

    def test_meta_required_perms_empty(self) -> None:
        """shared agent 且权限为空时允许任何登录用户调用。"""
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.required_perms == ()

    def test_meta_risk_is_low(self) -> None:
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.risk == "low"

    def test_meta_default_enabled_false(self) -> None:
        """工具默认禁用，加入 ai:enabled_tools 后启用。"""
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.default_enabled is False

    def test_meta_readonly_true(self) -> None:
        """file.parse 是纯读解析，readonly=True 不走 chip 跳转"""
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.readonly is True

    def test_meta_result_view_plain_json(self) -> None:
        """file.parse 显式声明 result_view=plain_json。"""
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.result_view == "plain_json"

    def test_meta_no_chip_target(self) -> None:
        """file.parse 是纯读但无 chip_target — 文件预览自包含，无模块页可去"""
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert reg.meta.chip_target is None

    def test_meta_accepts_file_covers_supported_mimes(self) -> None:
        """accepts_file 必须覆盖所有 parser 支持的 MIME"""
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert set(reg.meta.accepts_file) == set(SUPPORTED_MIME_TYPES)

    def test_meta_summary_under_100_chars(self) -> None:
        reg = ToolRegistry.get().find("file.parse")
        assert reg is not None
        assert len(reg.meta.summary) <= 100


# ============ 端到端：file_parse() 函数 ============


class TestFileParseFunction:
    """file_parse() 端到端：DB 查 sys_file + 调真实 parser"""

    async def test_parse_csv_file_end_to_end(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        path = tmp_path / "users.csv"
        _make_csv(path, [["name", "email"], ["alice", "a@x.com"], ["bob", "b@x.com"]])
        await _add_file_record(
            db_session,
            file_path=str(path),
            mime_type="text/csv",
        )
        ctx = _make_ctx(db_session)
        result = await file_parse(ctx, file_id=str(_FILE_ID), hint="用户导入")
        # ToolResult: data 给 LLM（含 parser/rows/columns/preview 全字段）
        assert result.ok is True
        assert result.data["parser"] == "CsvParser"
        assert result.data["rows"] == 2
        assert result.data["columns"] == ["name", "email"]
        assert result.data["preview"][0] == {"name": "alice", "email": "a@x.com"}
        # UIResult: ui 给前端 plain_json 渲染
        assert result.ui is not None
        assert result.ui.view_type == "plain_json"
        assert result.ui.view_data["rows"] == 2
        assert result.ui.view_data["columns"] == ["name", "email"]
        assert result.ui.view_data["preview"][0] == {
            "name": "alice",
            "email": "a@x.com",
        }
        # 审计字段（行数）
        assert result.ui.audit == {"rows_parsed": 2}
        assert result.ui.label_key == "ai.tool.file.parse.result"
        assert result.ui.label_params == {"rows": 2}

    async def test_parse_file_not_found_raises(self, db_session: AsyncSession) -> None:
        ctx = _make_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await file_parse(ctx, file_id="999999")
        assert exc_info.value.error_code == "AI_FILE_NOT_FOUND"

    async def test_parse_invalid_file_id_raises(self, db_session: AsyncSession) -> None:
        ctx = _make_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await file_parse(ctx, file_id="abc")
        assert exc_info.value.error_code == "AI_FILE_ID_INVALID"

    async def test_parse_unsupported_mime_raises(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """DB 有 file 但 MIME 不在 allowlist → AI_FILE_TYPE_NOT_ALLOWED"""
        path = tmp_path / "img.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        await _add_file_record(
            db_session,
            file_path=str(path),
            mime_type="image/png",
        )
        ctx = _make_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await file_parse(ctx, file_id=str(_FILE_ID))
        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_parse_deleted_file_skipped(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """del_flag='1' 的文件视为不存在"""
        path = tmp_path / "deleted.csv"
        _make_csv(path, [["a"], ["1"]])
        file_record = File(
            file_id=_FILE_ID,
            original_name="deleted.csv",
            file_name=str(_FILE_ID),
            file_path=str(path),
            file_url=f"/uploads/{_FILE_ID}",
            file_size=10,
            file_ext=".csv",
            mime_type="text/csv",
            business_type="ai-chat",
            owner_user_id=1,
            tenant_id=0,
            del_flag="1",
        )
        db_session.add(file_record)
        await db_session.flush()
        ctx = _make_ctx(db_session)
        with pytest.raises(BusinessRuleException) as exc_info:
            await file_parse(ctx, file_id=str(_FILE_ID))
        assert exc_info.value.error_code == "AI_FILE_NOT_FOUND"

    @pytest.mark.parametrize(
        ("owner_user_id", "tenant_id"),
        [(2, 0), (1, 9), (None, 0)],
    )
    async def test_parse_cross_owner_tenant_and_legacy_owner_are_hidden(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
        owner_user_id: int | None,
        tenant_id: int,
    ) -> None:
        path = tmp_path / f"scope-{owner_user_id}-{tenant_id}.csv"
        _make_csv(path, [["a"], ["1"]])
        record = await _add_file_record(
            db_session,
            file_path=str(path),
            mime_type="text/csv",
        )
        record.owner_user_id = owner_user_id
        record.tenant_id = tenant_id
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc_info:
            await file_parse(_make_ctx(db_session), file_id=str(_FILE_ID))

        assert exc_info.value.error_code == "AI_FILE_NOT_FOUND"
        assert exc_info.value.message == "文件不存在或不可访问"

    async def test_parse_rejects_wrong_business_type(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        path = tmp_path / "avatar.csv"
        _make_csv(path, [["a"], ["1"]])
        record = await _add_file_record(
            db_session,
            file_path=str(path),
            mime_type="text/csv",
        )
        record.business_type = "avatar"
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc_info:
            await file_parse(_make_ctx(db_session), file_id=str(_FILE_ID))

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_parse_user_import_uses_private_root(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        private_root = tmp_path / "private"
        private_root.mkdir()
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        path = private_root / "import.csv"
        _make_csv(path, [["a"], ["1"]])
        record = await _add_file_record(
            db_session,
            file_path=str(path),
            mime_type="text/csv",
        )
        record.business_type = "user-import"
        await db_session.flush()

        result = await file_parse(_make_ctx(db_session), file_id=str(_FILE_ID))

        assert result.data["rows"] == 1
