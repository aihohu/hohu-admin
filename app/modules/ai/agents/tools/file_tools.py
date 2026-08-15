"""file 模块的 AI Tool。

``file.parse`` 工具约束：
  - agent=SHARED_AGENT_CODE（运行时必须由 shared Agent 精确调用）
  - required_perms=("ai:file:parse",)，必须显式授权
  - risk=low（纯读解析，无副作用）
  - default_enabled=False，由部署方通过 ai:enabled_tools 显式启用
  - accepts_file=Excel/CSV MIME 列表

返回值仅含结构化摘要（rows/columns/preview/parser/file_size），raw bytes 永不进 LLM。
"""

from dataclasses import asdict

from app.modules.ai.agents.gateway.result import (
    ResultProjection,
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.file_access import (
    AI_CHAT_MIME_TYPES_BY_EXTENSION,
    FileAccessPolicy,
    chat_or_private_upload_root,
    load_protected_file,
)
from app.modules.ai.agents.tools.file_parser import (
    SUPPORTED_MIME_TYPES,
    FileParseResult,
    parse_file_bytes,
)
from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE, AiToolMeta
from app.modules.ai.constants import AI_FILE_PARSE_PERMISSION
from app.modules.ai.core.context import AiToolContext


def _accepted_mime_types() -> tuple[str, ...]:
    """按字母序输出，便于 LLM schema 稳定 + lint diff 友好"""
    return tuple(sorted(SUPPORTED_MIME_TYPES))


_FILE_PARSE_ACCESS_POLICY = FileAccessPolicy(
    allowed_business_types=frozenset({"ai-chat", "ai-chat-private", "user-import"}),
    mime_types_by_extension=AI_CHAT_MIME_TYPES_BY_EXTENSION,
    max_bytes=10 * 1024 * 1024,
    storage_root_resolver=chat_or_private_upload_root,
)


@ai_tool(
    AiToolMeta(
        name="file.parse",
        agent=SHARED_AGENT_CODE,
        required_perms=(AI_FILE_PARSE_PERMISSION,),
        risk="low",
        default_enabled=False,
        accepts_file=_accepted_mime_types(),
        summary=(
            "Parse uploaded Excel/CSV → {rows, columns, preview[3]}. "
            "Pass file_id. Raw bytes never enter LLM."
        ),
        readonly=True,
        idempotent=True,
        result_view="plain_json",
    )
)
async def file_parse(
    ctx: AiToolContext,
    file_id: str,
    hint: str = "",  # noqa: ARG001  审计可见，不参与解析逻辑
) -> ToolResult:
    """解析用户上传的文件，返回结构化摘要（rows / columns / 前 3 行预览）

    Args:
        file_id: 文件 ID（sys_file.file_id 的字符串形式，Snowflake JSON 字符串化）
        hint: 用途提示（如 "用户批量导入模板"），仅用于审计，不参与解析

    Returns:
        ToolResult：data 给 LLM（{rows, columns, preview, parser, file_size}，
        cell 已 stringify），ui 给前端 plain_json 兜底渲染（无 chip 跳转 —
        文件预览自包含，无模块页可去）。

    Raises:
        BusinessRuleException: AI_FILE_NOT_FOUND / AI_FILE_TYPE_NOT_ALLOWED /
            AI_FILE_TOO_LARGE / AI_FILE_PATH_INVALID
    """
    protected = await load_protected_file(
        ctx,
        file_id,
        policy=_FILE_PARSE_ACCESS_POLICY,
    )
    result: FileParseResult = await parse_file_bytes(
        protected.data,
        protected.mime_type,
    )
    parsed = asdict(result)
    rows_count = int(parsed.get("rows", 0))
    columns = parsed.get("columns", [])
    preview = parsed.get("preview", [])
    return ToolResult.success(
        data=parsed,
        projection=ResultProjection(
            subject_refs=({"type": "file", "id": str(file_id)},)
        ),
        ui=UIResult(
            view_type="plain_json",
            view_data={
                "rows": rows_count,
                "columns": columns,
                "preview": preview,
            },
            audit={"rows_parsed": rows_count},
            label_key="ai.tool.file.parse.result",
            label_params={"rows": rows_count},
        ),
    )
