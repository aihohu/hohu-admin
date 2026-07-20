"""scripts/check_ai_tools.py 7 项 static-only 检查的单测 — spec §12.4

构造违规 meta + 函数签名，验证 check_xxx 函数能正确检出。
不依赖 Registry（直接构造 RegisteredTool dataclass）。
"""

# ruff: noqa: PLC0415

from app.modules.ai.agents.tools.meta import SENSITIVE_INPUT_BLOCKLIST, AiToolMeta
from app.modules.ai.agents.tools.registry import RegisteredTool
from scripts.check_ai_tools import (
    SUMMARY_MAX_UNICODE_CHARS,
    check_args_summary_fields_not_sensitive,
    check_blocklist_field_must_be_sensitive,
    check_destructive_requires_hitl,
    check_dry_run_tool_must_implement_hook,
    check_high_risk_requires_dry_run,
    check_scope_param_requires_check,
    check_sensitive_input_not_in_signature,
    check_summary_length_limit,
)


def _make_reg(
    *,
    name: str = "test.tool",
    risk: str = "low",
    hitl_always: bool = False,
    dry_run_supported: bool = False,
    sensitive_input: tuple[str, ...] = (),
    summary: str = "test summary",
    dry_run_fn=None,
    args_summary_fields: tuple[str, ...] = (),
) -> RegisteredTool:
    """构造 RegisteredTool，meta 字段可定制"""
    meta = AiToolMeta(
        name=name,
        agent="shared",
        summary=summary,
        required_perms=(),
        risk=risk,
        hitl_always=hitl_always,
        dry_run_supported=dry_run_supported,
        sensitive_input=sensitive_input,
        args_summary_fields=args_summary_fields,
    )
    return RegisteredTool(
        meta=meta, fn=lambda *_args, **_kw: None, dry_run_fn=dry_run_fn
    )


def _async_fn_with_params(*params: str) -> str:
    """生成包含指定参数的 async 函数源码"""
    sig = "ctx, " + ", ".join(params) if params else "ctx"
    return f"""
async def _tool({sig}):
    pass
"""


class TestSensitiveInputNotInSignature:
    """spec §7.2: sensitive_input 字段禁止出现在函数签名"""

    def test_clean_no_violation(self) -> None:
        reg = _make_reg(sensitive_input=("password",))
        fn_src = "async def _tool(ctx, user_id): pass"
        assert check_sensitive_input_not_in_signature(reg, fn_src) == []

    def test_violation_when_in_signature(self) -> None:
        reg = _make_reg(sensitive_input=("password",))
        fn_src = "async def _tool(ctx, password): pass"
        violations = check_sensitive_input_not_in_signature(reg, fn_src)
        assert len(violations) == 1
        assert violations[0].tool_name == "test.tool"
        assert "password" in violations[0].detail

    def test_skip_when_no_sensitive_input(self) -> None:
        reg = _make_reg(sensitive_input=())
        fn_src = "async def _tool(ctx, password): pass"
        assert check_sensitive_input_not_in_signature(reg, fn_src) == []

    def test_kwonly_param_also_checked(self) -> None:
        reg = _make_reg(sensitive_input=("api_key",))
        fn_src = "async def _tool(ctx, *, api_key): pass"
        violations = check_sensitive_input_not_in_signature(reg, fn_src)
        assert len(violations) == 1


class TestBlocklistFieldMustBeSensitive:
    """spec §7.2: 命中 SENSITIVE_INPUT_BLOCKLIST 的字段必须声明 sensitive_input"""

    def test_blocklist_field_declared_no_violation(self) -> None:
        reg = _make_reg(sensitive_input=("password",))
        fn_src = "async def _tool(ctx, password): pass"
        assert check_blocklist_field_must_be_sensitive(reg, fn_src) == []

    def test_blocklist_field_not_declared_violation(self) -> None:
        reg = _make_reg(sensitive_input=())
        fn_src = "async def _tool(ctx, password): pass"
        violations = check_blocklist_field_must_be_sensitive(reg, fn_src)
        assert len(violations) == 1
        assert "password" in violations[0].detail

    def test_normal_param_no_violation(self) -> None:
        reg = _make_reg(sensitive_input=())
        fn_src = "async def _tool(ctx, user_id): pass"
        assert check_blocklist_field_must_be_sensitive(reg, fn_src) == []

    def test_all_blocklist_fields_detected(self) -> None:
        """所有 SENSITIVE_INPUT_BLOCKLIST 字段都会被检"""
        for blocked in SENSITIVE_INPUT_BLOCKLIST:
            reg = _make_reg(sensitive_input=())
            fn_src = f"async def _tool(ctx, {blocked}): pass"
            violations = check_blocklist_field_must_be_sensitive(reg, fn_src)
            assert len(violations) == 1, f"应检出 {blocked}"


class TestDestructiveRequiresHitl:
    """spec §5.3: destructive risk 应显式声明 hitl_always"""

    def test_destructive_with_hitl_always_no_violation(self) -> None:
        reg = _make_reg(risk="destructive", hitl_always=True)
        assert check_destructive_requires_hitl(reg) == []

    def test_destructive_without_hitl_always_warns(self) -> None:
        reg = _make_reg(risk="destructive", hitl_always=False)
        violations = check_destructive_requires_hitl(reg)
        assert len(violations) == 1
        assert violations[0].severity == "warning"

    def test_non_destructive_no_violation(self) -> None:
        reg = _make_reg(risk="high", hitl_always=False)
        assert check_destructive_requires_hitl(reg) == []


class TestHighRiskRequiresDryRun:
    """spec §5.3: high risk 应有 dry_run_fn"""

    def test_high_risk_with_dry_run_no_violation(self) -> None:
        reg = _make_reg(risk="high", dry_run_supported=True, dry_run_fn=lambda: None)
        assert check_high_risk_requires_dry_run(reg) == []

    def test_high_risk_supported_but_missing_dry_run_fn(self) -> None:
        reg = _make_reg(risk="high", dry_run_supported=True, dry_run_fn=None)
        violations = check_high_risk_requires_dry_run(reg)
        assert len(violations) == 1
        assert violations[0].severity == "error"

    def test_high_risk_not_supported_missing_dry_run_warns(self) -> None:
        reg = _make_reg(risk="high", dry_run_supported=False, dry_run_fn=None)
        violations = check_high_risk_requires_dry_run(reg)
        assert len(violations) == 1
        assert violations[0].severity == "warning"

    def test_low_risk_no_check(self) -> None:
        reg = _make_reg(risk="low", dry_run_supported=False, dry_run_fn=None)
        assert check_high_risk_requires_dry_run(reg) == []


class TestScopeParamRequiresCheck:
    """spec §6.2: 签名含 *_id / *_ids 参数必须调 ensure_targets_in_scope"""

    def test_no_scope_param_no_violation(self) -> None:
        reg = _make_reg()
        fn_src = "async def _tool(ctx, status): pass"
        assert check_scope_param_requires_check(reg, fn_src) == []

    def test_scope_param_with_check_no_violation(self) -> None:
        reg = _make_reg()
        fn_src = """
async def _tool(ctx, user_id: int):
    ensure_targets_in_scope(ctx, user_ids=[user_id])
    return {}
"""
        assert check_scope_param_requires_check(reg, fn_src) == []

    def test_scope_param_without_check_violation(self) -> None:
        reg = _make_reg()
        fn_src = "async def _tool(ctx, user_id: int): return {}"
        violations = check_scope_param_requires_check(reg, fn_src)
        assert len(violations) == 1
        assert "user_id" in violations[0].detail

    def test_ids_param_also_checked(self) -> None:
        reg = _make_reg()
        fn_src = "async def _tool(ctx, user_ids: list): return {}"
        violations = check_scope_param_requires_check(reg, fn_src)
        assert len(violations) == 1

    def test_ctx_excluded_from_scope_params(self) -> None:
        """ctx 参数不应被视为 scope 参数（约定，spec §6.2）"""
        reg = _make_reg()
        fn_src = "async def _tool(ctx): pass"
        assert check_scope_param_requires_check(reg, fn_src) == []


class TestSummaryLengthLimit:
    """spec §5.1: summary ≤ 100 Unicode chars"""

    def test_short_summary_no_violation(self) -> None:
        reg = _make_reg(summary="short")
        assert check_summary_length_limit(reg) == []

    def test_at_limit_no_violation(self) -> None:
        reg = _make_reg(summary="x" * SUMMARY_MAX_UNICODE_CHARS)
        assert check_summary_length_limit(reg) == []

    def test_over_limit_violation(self) -> None:
        reg = _make_reg(summary="x" * (SUMMARY_MAX_UNICODE_CHARS + 1))
        violations = check_summary_length_limit(reg)
        assert len(violations) == 1

    def test_unicode_chars_counted(self) -> None:
        """中文字符按 Unicode 计数（1 个 = 1 字符）"""
        reg = _make_reg(summary="测" * (SUMMARY_MAX_UNICODE_CHARS + 1))
        violations = check_summary_length_limit(reg)
        assert len(violations) == 1


class TestArgsSummaryFieldsNotSensitive:
    """spec §9.2 SR-18: args_summary_fields 不得含敏感字段名"""

    def test_empty_fields_no_violation(self) -> None:
        reg = _make_reg(args_summary_fields=())
        assert check_args_summary_fields_not_sensitive(reg) == []

    def test_safe_fields_no_violation(self) -> None:
        reg = _make_reg(args_summary_fields=("user_id", "new_dept_id", "role_code"))
        assert check_args_summary_fields_not_sensitive(reg) == []

    def test_exact_password_violation(self) -> None:
        reg = _make_reg(args_summary_fields=("user_id", "password"))
        violations = check_args_summary_fields_not_sensitive(reg)
        assert len(violations) == 1
        assert violations[0].check == "args_summary_fields_not_sensitive"
        assert "password" in violations[0].detail

    def test_password_hash_prefix_violation(self) -> None:
        """password_hash 是 password 前缀扩展，应被检出"""
        reg = _make_reg(args_summary_fields=("user_id", "password_hash"))
        violations = check_args_summary_fields_not_sensitive(reg)
        assert len(violations) == 1

    def test_api_key_violation(self) -> None:
        reg = _make_reg(args_summary_fields=("api_key",))
        violations = check_args_summary_fields_not_sensitive(reg)
        assert len(violations) == 1

    def test_multiple_sensitive_fields_multiple_violations(self) -> None:
        reg = _make_reg(args_summary_fields=("password", "api_key", "token"))
        violations = check_args_summary_fields_not_sensitive(reg)
        assert len(violations) == 3

    def test_non_sensitive_token_substring_no_violation(self) -> None:
        """csrf_token 含 'token' 子串但不是 SENSITIVE_INPUT_BLOCKLIST 完全匹配，
        word-boundary 检查应放过（与 §7.3 GLOBAL_OUTPUT_BLOCKLIST 同逻辑）。"""
        reg = _make_reg(args_summary_fields=("csrf_token", "pagination_token"))
        # 注意：csrf_token 不应被检出（'token' 是 SENSITIVE_INPUT_BLOCKLIST 里的项，
        # 但 csrf_token 既不是完全相等 'token'，也不是 'token_xxx' 前缀模式，
        # 而是后缀。按 word-boundary 应放过）
        violations = check_args_summary_fields_not_sensitive(reg)
        # 当前实现用 startswith(bl + "_") 检前缀，csrf_token 不命中
        assert violations == []

    """spec §5.1: dry_run_supported=True 必须有 _dry_run_<tool>"""

    def test_supported_with_fn_no_violation(self) -> None:
        reg = _make_reg(dry_run_supported=True, dry_run_fn=lambda: None)
        assert check_dry_run_tool_must_implement_hook(reg) == []

    def test_supported_without_fn_violation(self) -> None:
        reg = _make_reg(dry_run_supported=True, dry_run_fn=None)
        violations = check_dry_run_tool_must_implement_hook(reg)
        assert len(violations) == 1

    def test_not_supported_no_check(self) -> None:
        reg = _make_reg(dry_run_supported=False, dry_run_fn=None)
        assert check_dry_run_tool_must_implement_hook(reg) == []
