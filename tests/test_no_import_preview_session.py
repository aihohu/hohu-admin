"""静态防回归：ImportPreviewSession 类应无残留（spec §3.6 v2.2 P1-2 + Task 22b）。

v2.2 P1-2 决策（spec §3.6 line 2489-2510）：合并 ImportPreviewSession → ImportBatch
作为唯一 aggregate root。删除独立 ImportPreviewSession 类，所有 preview 状态/字段进
ImportBatch（status=PREVIEW_DONE / preview_token / summary_new 等）。

本文件静态扫描 ``app/`` 目录所有 .py 文件，断言无 ``ImportPreviewSession`` 字符串
残留。可作为 pre-commit hook 防止误添加回去。

不进 tests/modules/ 是因为它不属于任何业务模块，是 monorepo 级别的不变量检查。
"""

from pathlib import Path


def _app_root() -> Path:
    return Path(__file__).parent.parent / "app"


def _scan_import_preview_session() -> list[str]:
    """扫描 app/ 下所有 .py 文件，返回含 ``ImportPreviewSession`` 的文件路径列表。"""
    matches: list[str] = []
    for py in _app_root().rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "ImportPreviewSession" in text:
            matches.append(str(py.relative_to(_app_root().parent)))
    return matches


def test_no_import_preview_session_class_in_app() -> None:
    """spec §3.6 v2.2 P1-2：``ImportPreviewSession`` 已合并到 ``UserImportBatch``。

    扫描 ``app/`` 目录所有 .py 文件，不应出现 ``ImportPreviewSession`` 字符串
    （类定义、import、注释、字符串都算）。

    **反例**: 重新引入 ``class ImportPreviewSession`` → 违反 single aggregate root
    设计（spec §3.6 line 2489），状态机分散到两个表 → 跨表事务 + 一致性陷阱。
    **回归**: 本测试 grep 整个 ``app/``，任何残留都会失败。
    """
    matches = _scan_import_preview_session()
    assert not matches, (
        "Found ImportPreviewSession references (spec §3.6 v2.2 P1-2 forbids this): "
        + ", ".join(matches)
    )
