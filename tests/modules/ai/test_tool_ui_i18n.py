"""Built-in tool UI labels must be semantic locale keys, not final Chinese copy."""

from pathlib import Path


def test_builtin_tool_ui_metadata_does_not_embed_known_chinese_field_labels():
    root = Path(__file__).parents[3]
    sources = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "app/modules/system/ai_tools.py",
            "app/modules/job/ai_tools.py",
        )
    )

    for label in (
        '"label": "名称"',
        '"label": "编码"',
        '"label": "状态"',
        '"label": "用户名"',
        '"label": "昵称"',
        '"label": "导出批次 ID"',
        '"label": "导出行数"',
        '"label": "文件大小"',
        '"label": "过期时间"',
        '"label": "任务 ID"',
        '"label": "新 cron"',
    ):
        assert label not in sources
