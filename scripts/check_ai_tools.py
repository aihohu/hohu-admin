"""Tool 接入合规静态检查 — spec §12.4

pre-commit + CI 双跑。零 DB 依赖（启动时不调 validate_on_startup）。

9 项检查（spec §12.4）：
  ✅ static-only（不查 DB）：
    1. sensitive_input_not_in_signature
    2. blocklist_field_must_be_sensitive
    3. destructive_requires_hitl
    4. high_risk_requires_dry_run
    7. scope_param_requires_check（ast 解析函数体）
    8. summary_length_limit
    9. dry_run_tool_must_implement_hook
  ⏭️ startup-only（validate_on_startup 已覆盖，本脚本跳过）：
    5. agent_must_exist_in_registry
    6. perms_must_exist_in_menu

用法：
  uv run python scripts/check_ai_tools.py
  # 退出码：0 全过 / 1 有违规
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

# Windows 默认 GBK 编码无法输出 emoji/中文，强制 utf-8（pre-commit hook 兼容）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 项目根
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.modules.ai.agents.tools.file_parser import (  # noqa: E402
    SUPPORTED_MIME_TYPES,
)
from app.modules.ai.agents.tools.meta import (  # noqa: E402
    SENSITIVE_INPUT_BLOCKLIST,
    SHARED_AGENT_CODE,
)
from app.modules.ai.agents.tools.registry import (  # noqa: E402
    RegisteredTool,
    ToolRegistry,
)

# spec §5.5: summary 上限
SUMMARY_MAX_UNICODE_CHARS = 100


class Violation:
    """单条违规"""

    def __init__(
        self, tool_name: str, check: str, detail: str, severity: str = "error"
    ) -> None:
        self.tool_name = tool_name
        self.check = check
        self.detail = detail
        self.severity = severity  # error / warning

    def __str__(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.tool_name} | {self.check}: {self.detail}"
        )


# ============ 9 项检查 ============


def check_sensitive_input_not_in_signature(
    reg: RegisteredTool, fn_src: str | None
) -> list[Violation]:
    """spec §7.2: sensitive_input 字段禁止出现在函数签名"""
    if not reg.meta.sensitive_input:
        return []
    if fn_src is None:
        return []
    violations: list[Violation] = []
    # 简化：用 ast 解析函数定义，找参数名
    try:
        tree = ast.parse(fn_src)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        param_names: list[str] = []
        args = node.args
        param_names.extend(a.arg for a in args.args)
        param_names.extend(a.arg for a in args.kwonlyargs)
        if args.vararg:
            param_names.append(args.vararg.arg)
        if args.kwarg:
            param_names.append(args.kwarg.arg)
        for sens in reg.meta.sensitive_input:
            if sens in param_names:
                violations.append(
                    Violation(
                        reg.meta.name,
                        "sensitive_input_not_in_signature",
                        f"字段 '{sens}' 出现在函数签名参数中（应在 ctx.secrets 或后端策略生成）",
                    )
                )
    return violations


def check_blocklist_field_must_be_sensitive(
    reg: RegisteredTool, fn_src: str | None
) -> list[Violation]:
    """spec §7.2: 命中 SENSITIVE_INPUT_BLOCKLIST 的字段必须声明 sensitive_input"""
    if fn_src is None:
        return []
    try:
        tree = ast.parse(fn_src)
    except SyntaxError:
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        param_names: list[str] = []
        args = node.args
        param_names.extend(a.arg for a in args.args)
        param_names.extend(a.arg for a in args.kwonlyargs)
        for param in param_names:
            if (
                param in SENSITIVE_INPUT_BLOCKLIST
                and param not in reg.meta.sensitive_input
            ):
                violations.append(
                    Violation(
                        reg.meta.name,
                        "blocklist_field_must_be_sensitive",
                        f"参数 '{param}' 命中 SENSITIVE_INPUT_BLOCKLIST 但未声明 sensitive_input",
                    )
                )
    return violations


def check_destructive_requires_hitl(reg: RegisteredTool) -> list[Violation]:
    """spec §5.3: destructive risk 必须走 HITL（classify_execution_mode 已强制，
    但显式声明 hitl_always=True 提高可读性 / 防矩阵规则改动后破坏语义）"""
    if reg.meta.risk == "destructive" and not reg.meta.hitl_always:
        return [
            Violation(
                reg.meta.name,
                "destructive_requires_hitl",
                "risk=destructive 应显式声明 hitl_always=True（即使矩阵已强制 HITL）",
                severity="warning",
            )
        ]
    return []


def check_high_risk_requires_dry_run(reg: RegisteredTool) -> list[Violation]:
    """spec §5.3: high risk 应有 dry_run_fn（否则 count=None 保守降级 HITL，
    影响单行修改场景的体验）"""
    if reg.meta.risk != "high":
        return []

    # dry_run_fn 在 validate_on_startup 时统一解析（commit 51d732f），装饰器执行
    # 期 reg.dry_run_fn 还是 None。static check 时主动 sys.modules 查找一次，
    # 与 check_dry_run_tool_must_implement_hook 同款（spec §5.1）。
    def _has_dry_run_fn() -> bool:
        if reg.dry_run_fn is not None:
            return True
        import sys  # noqa: PLC0415

        module = sys.modules.get(reg.fn.__module__)
        if module is None:
            return False
        fn_name = f"_dry_run_{reg.meta.name.replace('.', '_')}"
        return hasattr(module, fn_name)

    if reg.meta.dry_run_supported and not _has_dry_run_fn():
        return [
            Violation(
                reg.meta.name,
                "high_risk_requires_dry_run",
                "risk=high + dry_run_supported=True 但未实现 _dry_run_<tool>",
            )
        ]
    if not reg.meta.dry_run_supported and not _has_dry_run_fn():
        return [
            Violation(
                reg.meta.name,
                "high_risk_requires_dry_run",
                "risk=high 建议声明 dry_run_supported=True 并实现 _dry_run_<tool>"
                "（否则 count=None 保守降级 HITL，单行修改也走 HITL 影响体验）",
                severity="warning",
            )
        ]
    return []


def check_scope_param_requires_check(
    reg: RegisteredTool, fn_src: str | None
) -> list[Violation]:
    """spec §6.2: 签名含 *_id / *_ids 参数必须调 ensure_targets_in_scope

    豁免：
      - SHARED_AGENT_CODE（file.parse 等通用 tool 无 data_scope 概念）
      - file_id：sys_file 资源不属于业务 data_scope（任何有 system:user:import 权限
        的用户都可读自己上传的 file_id；data_scope 控制的是 file 内的目标用户，
        在后续业务调用 dry_run_import_users / batch_create 内部强制）。
    """
    if reg.meta.agent == SHARED_AGENT_CODE:
        return []
    if fn_src is None:
        return []
    try:
        tree = ast.parse(fn_src)
    except SyntaxError:
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope_params = [
            a.arg
            for a in node.args.args + node.args.kwonlyargs
            if (a.arg.endswith("_id") or a.arg.endswith("_ids"))
            and a.arg != "file_id"  # sys_file 资源不属于业务 data_scope
        ]
        if not scope_params:
            continue
        # ctx 是首个参数（约定），跳过
        scope_params = [p for p in scope_params if p != "ctx"]
        if not scope_params:
            continue
        # 检查函数体是否调用 ensure_targets_in_scope
        body_src = ast.unparse(node)
        if "ensure_targets_in_scope" not in body_src:
            violations.append(
                Violation(
                    reg.meta.name,
                    "scope_param_requires_check",
                    f"签名含 *_id/*_ids 参数 {scope_params} 但未调 ensure_targets_in_scope",
                )
            )
    return violations


def check_summary_length_limit(reg: RegisteredTool) -> list[Violation]:
    """spec §5.1: summary ≤ 100 Unicode chars"""
    if len(reg.meta.summary) > SUMMARY_MAX_UNICODE_CHARS:
        return [
            Violation(
                reg.meta.name,
                "summary_length_limit",
                f"summary 长度 {len(reg.meta.summary)} 超过 {SUMMARY_MAX_UNICODE_CHARS} 字符",
            )
        ]
    return []


def check_args_summary_fields_not_sensitive(reg: RegisteredTool) -> list[Violation]:
    """spec §9.2 SR-18: args_summary_fields 白名单不得含敏感字段

    防业务方误把 'password' / 'api_key' 等字段加入 args_summary_fields，
    导致敏感值落库到 ai_operation_log.args_summary。
    """
    violations: list[Violation] = []
    for field_name in reg.meta.args_summary_fields:
        # word-boundary 检查（与 §7.3 sensitive.py GLOBAL_OUTPUT_BLOCKLIST 同逻辑）：
        # 命中完全相等 / 前缀（password_hash → password）
        if field_name in SENSITIVE_INPUT_BLOCKLIST or any(
            field_name.startswith(bl + "_") or field_name == bl
            for bl in SENSITIVE_INPUT_BLOCKLIST
        ):
            violations.append(
                Violation(
                    reg.meta.name,
                    "args_summary_fields_not_sensitive",
                    f"args_summary_fields 字段 '{field_name}' 命中 SENSITIVE_INPUT_BLOCKLIST，"
                    f"敏感值会落库到 ai_operation_log.args_summary",
                )
            )
    return violations


def check_accepts_file_mime_valid(reg: RegisteredTool) -> list[Violation]:
    """spec §16 SR-24: accepts_file 中声明的 MIME 必须在 parser 覆盖范围内

    防 typo（application/vnd.ms-excel 写错成 application/vnd.msexcel）+
    防漂移（parser 改了 MIME 但 tool meta 忘同步）。
    """
    if not reg.meta.accepts_file:
        return []
    invalid = [mt for mt in reg.meta.accepts_file if mt not in SUPPORTED_MIME_TYPES]
    if invalid:
        return [
            Violation(
                reg.meta.name,
                "accepts_file_mime_valid",
                f"accepts_file 含未支持的 MIME {invalid}，"
                f"已知 parser 覆盖: {sorted(SUPPORTED_MIME_TYPES)}",
            )
        ]
    return []


def check_dry_run_tool_must_implement_hook(reg: RegisteredTool) -> list[Violation]:
    """spec §5.1: dry_run_supported=True 必须有 _dry_run_<tool>

    dry_run_fn 在 validate_on_startup 时统一解析（commit 51d732f），装饰器执行
    期 reg.dry_run_fn 还是 None。本检查直接 sys.modules 查找 _dry_run_<tool>
    函数存在性（static check 时所有模块已加载完）。
    """
    if not reg.meta.dry_run_supported:
        return []
    # 已经被 validate_on_startup 解析过（运行时）→ 直接通过
    if reg.dry_run_fn is not None:
        return []
    # 静态检查路径：从 fn.__module__ 查找 _dry_run_<tool> 函数
    import sys  # noqa: PLC0415

    module_name = reg.fn.__module__
    module = sys.modules.get(module_name)
    if module is None:
        return [
            Violation(
                reg.meta.name,
                "dry_run_tool_must_implement_hook",
                f"dry_run_supported=True 但模块 {module_name!r} 未加载（无法查找 _dry_run_<tool>）",
            )
        ]
    fn_name = f"_dry_run_{reg.meta.name.replace('.', '_')}"
    if not hasattr(module, fn_name):
        return [
            Violation(
                reg.meta.name,
                "dry_run_tool_must_implement_hook",
                f"dry_run_supported=True 但同模块未定义 async def {fn_name}（命名约定：name='user.create' → _dry_run_user_create）",
            )
        ]
    return []


# ============ 主流程 ============


def load_all_tools() -> list[RegisteredTool]:
    """加载所有 @ai_tool 注册的 tool 到 Registry"""
    # 扫描所有 ai_tools.py 文件
    candidates = [
        "app.modules.system.ai_tools",
        "app.modules.ai.agents.tools.file_tools",
    ]
    for mod_name in candidates:
        try:
            importlib.import_module(mod_name)
        except ImportError:
            pass
    return ToolRegistry.get().all()


def get_fn_source(reg: RegisteredTool) -> str | None:
    """获取业务函数源码（用于 ast 分析）"""
    try:
        return inspect.getsource(reg.fn)
    except (TypeError, OSError):
        return None


def run_all_checks() -> list[Violation]:
    """跑所有 static-only 检查"""
    tools = load_all_tools()
    violations: list[Violation] = []

    for reg in tools:
        fn_src = get_fn_source(reg)
        violations.extend(check_sensitive_input_not_in_signature(reg, fn_src))
        violations.extend(check_blocklist_field_must_be_sensitive(reg, fn_src))
        violations.extend(check_destructive_requires_hitl(reg))
        violations.extend(check_high_risk_requires_dry_run(reg))
        violations.extend(check_scope_param_requires_check(reg, fn_src))
        violations.extend(check_summary_length_limit(reg))
        violations.extend(check_args_summary_fields_not_sensitive(reg))
        violations.extend(check_accepts_file_mime_valid(reg))
        violations.extend(check_dry_run_tool_must_implement_hook(reg))

    return violations


def main() -> int:
    violations = run_all_checks()
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    if not violations:
        print(f"✅ All {len(ToolRegistry.get().all())} tools passed 8 static checks")
        return 0

    for v in violations:
        print(v)
    print()
    print(f"Total: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
