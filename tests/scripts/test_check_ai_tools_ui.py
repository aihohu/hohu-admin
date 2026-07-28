"""spec 决策 3 修正：lint 强制 builtin tool 函数返回 ToolResult.success 时带 ui=。"""

import ast

import pytest

from scripts.check_ai_tools_ui import (
    CheckAiToolsUiError,
    check_function_for_missing_ui,
)


class TestCheckFunctionForMissingUi:
    def test_detects_success_without_ui(self):
        """ToolResult.success(data=...) 不带 ui → 报错。"""
        code = """
async def user_count(ctx):
    return ToolResult.success(data={"count": 5})
"""
        tree = ast.parse(code)
        fn_node = tree.body[0]  # AsyncFunctionDef
        with pytest.raises(CheckAiToolsUiError, match="missing ui="):
            check_function_for_missing_ui(fn_node, file="test.py")

    def test_passes_success_with_ui(self):
        """ToolResult.success(data=..., ui=UIResult(...)) → 通过。"""
        code = """
async def user_count(ctx):
    return ToolResult.success(
        data={"count": 5},
        ui=UIResult(view_type="plain_json", view_data={"count": 5}),
    )
"""
        tree = ast.parse(code)
        fn_node = tree.body[0]
        check_function_for_missing_ui(fn_node, file="test.py")  # no exception

    def test_ignores_non_toolresult_success(self):
        """ToolResult.failure / 其他函数 不查 ui。"""
        code = """
async def user_count(ctx):
    return ToolResult.failure("AI_X", "msg")
"""
        tree = ast.parse(code)
        fn_node = tree.body[0]
        check_function_for_missing_ui(fn_node, file="test.py")  # no exception

    def test_ignores_dict_return(self):
        """dict 返回值（executor 兼容路径）不查 ui。"""
        code = """
async def user_count(ctx):
    return {"count": 5}
"""
        tree = ast.parse(code)
        fn_node = tree.body[0]
        check_function_for_missing_ui(fn_node, file="test.py")


def test_ignores_nested_helper_function_return():
    """Nested def inside @ai_tool function returning ToolResult.success without ui= is OK.

    ast.walk would descend into nested def and false-positive; the lint should
    only check returns directly within the @ai_tool function body.
    """
    code = """
async def user_count(ctx):
    async def _retry_without_ui():
        return ToolResult.success(data={"count": 0})
    result = await _retry_without_ui()
    return ToolResult.success(
        data={"count": 5},
        ui=UIResult(view_type="plain_json", view_data={"count": 5}),
    )
"""
    tree = ast.parse(code)
    fn_node = tree.body[0]
    check_function_for_missing_ui(fn_node, file="test.py")  # no exception
