"""spec 2026-07-16 决策 3 修正：lint 强制 builtin tool 函数返回 ToolResult.success 时带 ui=。

builtin tool 函数（@ai_tool 装饰）若返回 ToolResult.success 必须显式传 ui=，
否则 break 决策 3（业务方应当 data + ui 都填）。

检查规则（AST 静态分析）：
  - 找到 @ai_tool 装饰的 async def 函数
  - 遍历函数体，找到所有 `return ToolResult.success(...` 语句
  - 检查 keyword args 是否含 `ui=`
  - 缺失 → 报错

不查：
  - 非装饰函数（业务方可能写 helper 函数）
  - ToolResult.failure(...) 调用（错误结果不需要 ui）
  - 裸 dict / list 返回（executor 兼容路径）

用法：
    uv run python scripts/check_ai_tools_ui.py app/modules/

集成 pre-commit：
    - .pre-commit-config.yaml 加 hook，stage: commit
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Windows console defaults to gbk; force utf-8 so Chinese error messages render.
# Sibling script scripts/check_ai_tools.py uses the same pattern.
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class CheckAiToolsUiError(Exception):
    """ToolResult.success 调用缺 ui= 参数"""


# @ai_tool 装饰器识别（按名字，不导入）
_TOOL_DECORATOR_NAMES = {"ai_tool"}


def _has_ai_tool_decorator(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """检测函数是否被 @ai_tool(...) 装饰"""
    for dec in fn.decorator_list:
        # @ai_tool(...) → ast.Call(func=ast.Name(id='ai_tool'))
        # @ai_tool → ast.Name(id='ai_tool')
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id in _TOOL_DECORATOR_NAMES:
            return True
    return False


def _is_tool_result_success_call(node: ast.AST) -> bool:
    """检测 return ToolResult.success(...) 语句"""
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    # ToolResult.success
    if isinstance(func, ast.Attribute) and func.attr == "success":
        if isinstance(func.value, ast.Name) and func.value.id == "ToolResult":
            return True
    return False


def _iter_return_statements(fn: ast.AsyncFunctionDef | ast.FunctionDef):
    """遍历函数体内的 return 语句，不递归进入嵌套函数定义。

    ast.walk 会下钻到嵌套 def/async def，可能误报 builtin tool 内部
    辅助函数（如 "重试时返回精简 data" helper）的合法 return。
    这里只检查直接属于 @ai_tool 函数体的 return。
    """
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue  # skip nested function definitions
        yield from _walk_excluding_nested_funcs(child)


def _walk_excluding_nested_funcs(node: ast.AST):
    """Walk an AST node's subtree but don't descend into nested function defs."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue  # don't recurse into nested def
        yield from _walk_excluding_nested_funcs(child)


def check_function_for_missing_ui(
    fn: ast.AsyncFunctionDef | ast.FunctionDef,
    *,
    file: str,
) -> None:
    """检查单个 @ai_tool 函数内所有 ToolResult.success 调用是否带 ui=

    Raises:
        CheckAiToolsUiError: 若任一 ToolResult.success 缺 ui=
    """
    for node in _iter_return_statements(fn):
        if not _is_tool_result_success_call(node):
            continue
        call: ast.Call = node.value  # type: ignore[assignment]
        kw_names = {kw.arg for kw in call.keywords if kw.arg is not None}
        if "ui" not in kw_names:
            raise CheckAiToolsUiError(
                f"{file}:{fn.lineno}: @ai_tool function '{fn.name}' "
                f"calls ToolResult.success missing ui=. "
                f"决策 3 要求 builtin tool 同时填 data + ui。"
            )


def check_file(path: Path) -> list[CheckAiToolsUiError]:
    """检查单个 Python 文件，返回所有错误（不抛，批量收集）"""
    errors: list[CheckAiToolsUiError] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return errors  # 静默跳过（其它 lint 会报）

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not _has_ai_tool_decorator(node):
            continue
        try:
            check_function_for_missing_ui(node, file=str(path))
        except CheckAiToolsUiError as e:
            errors.append(e)
    return errors


def main(argv: list[str]) -> int:
    """CLI 入口：扫描给定路径下所有 .py 文件"""
    paths = [Path(a) for a in argv[1:]] or [Path("app/modules")]
    py_files: list[Path] = []
    for p in paths:
        if p.is_dir():
            py_files.extend(p.rglob("*.py"))
        elif p.is_file() and p.suffix == ".py":
            py_files.append(p)

    all_errors: list[CheckAiToolsUiError] = []
    for f in py_files:
        all_errors.extend(check_file(f))

    if all_errors:
        for e in all_errors:
            print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
