"""file 模块的 AI Tool — spec §16 v1.5+ SR-24

按 spec §16.4 实现 file.parse tool：
  - agent=SHARED_AGENT_CODE（任何登录用户直通，不绑定业务 Agent）
  - required_perms=()（与 SHARED_AGENT_CODE 配对，spec §5.4 豁免）
  - risk=low（纯读解析，无副作用）
  - default_enabled=False（spec §5.4 SR-17：默认禁用，部署方显式加 ai:enabled_tools 启用）
  - accepts_file=Excel/CSV MIME 列表

返回值仅含结构化摘要（rows/columns/preview/parser/file_size），raw bytes 永不进 LLM。
"""

from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.file_parser import (
    SUPPORTED_MIME_TYPES,
    FileParseResult,
    parse_file,
)
from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE, AiToolMeta
from app.modules.ai.core.context import AiToolContext
from app.modules.system.models.file import File


def _accepted_mime_types() -> tuple[str, ...]:
    """按字母序输出，便于 LLM schema 稳定 + lint diff 友好"""
    return tuple(sorted(SUPPORTED_MIME_TYPES))


@ai_tool(
    AiToolMeta(
        name="file.parse",
        agent=SHARED_AGENT_CODE,
        required_perms=(),
        risk="low",
        default_enabled=False,
        accepts_file=_accepted_mime_types(),
        summary=(
            "Parse uploaded Excel/CSV → {rows, columns, preview[3]}. "
            "Pass file_id. Raw bytes never enter LLM."
        ),
        readonly=True,
    )
)
async def file_parse(
    ctx: AiToolContext,
    file_id: str,
    hint: str = "",  # noqa: ARG001  审计可见，不参与解析逻辑
) -> dict:
    """解析用户上传的文件，返回结构化摘要（rows / columns / 前 3 行预览）

    Args:
        file_id: 文件 ID（sys_file.file_id 的字符串形式，Snowflake JSON 字符串化）
        hint: 用途提示（如 "用户批量导入模板"），仅用于审计，不参与解析

    Returns:
        FileParseResult 的 dict 形式（cell 已 stringify）

    Raises:
        BusinessRuleException: AI_FILE_NOT_FOUND / AI_FILE_TYPE_UNSUPPORTED / AI_FILE_TOO_LARGE
    """
    try:
        file_id_int = int(file_id)
    except (TypeError, ValueError) as e:
        raise BusinessRuleException(
            f"file_id 格式无效: {file_id!r}",
            error_code="AI_FILE_ID_INVALID",
        ) from e

    stmt = select(File).where(
        File.file_id == file_id_int,
        File.del_flag == "0",
    )
    file_record = (await ctx.db.execute(stmt)).scalars().first()
    if file_record is None:
        raise BusinessRuleException(
            f"文件不存在: file_id={file_id}",
            error_code="AI_FILE_NOT_FOUND",
        )

    # sys_file.file_path 是相对路径（"uploads/2026/.../xxx.xlsx"），相对 cwd
    # （fastapi dev / uvicorn 项目根启动）。与 file_service._delete_disk_file 同逻辑。
    result: FileParseResult = await parse_file(
        file_path=Path(file_record.file_path),
        mime_type=file_record.mime_type or "",
    )
    return asdict(result)
