"""``scripts/check_ai_tools.py`` 静态检查测试。

构造违规 meta + 函数签名，验证 check_xxx 函数能正确检出。
不依赖 Registry（直接构造 RegisteredTool dataclass）。
"""

# ruff: noqa: PLC0415

import importlib

import pytest

from app.modules.ai.agents.tools.meta import SENSITIVE_INPUT_BLOCKLIST, AiToolMeta
from app.modules.ai.agents.tools.registry import RegisteredTool
from scripts.check_ai_tools import (
    EXPECTED_BUILTIN_TOOL_NAMES,
    SUMMARY_MAX_UNICODE_CHARS,
    check_accepts_file_mime_valid,
    check_args_summary_fields_not_sensitive,
    check_blocklist_field_must_be_sensitive,
    check_destructive_requires_hitl,
    check_dry_run_tool_must_implement_hook,
    check_file_param_requires_protected_loader,
    check_gateway_only_tool_not_llm_visible,
    check_high_risk_requires_dry_run,
    check_prepared_binding_valid,
    check_scope_param_requires_check,
    check_sensitive_input_not_in_signature,
    check_summary_length_limit,
    load_all_tools,
)


def _make_reg(
    *,
    name: str = "test.tool",
    agent: str = "user_mgmt",
    risk: str = "low",
    hitl_always: bool = False,
    dry_run_supported: bool = False,
    sensitive_input: tuple[str, ...] = (),
    summary: str = "test summary",
    accepts_file: tuple[str, ...] = (),
    dry_run_fn=None,
    args_summary_fields: tuple[str, ...] = (),
    interaction_flow: str = "direct",
    prepared_execute_tool: str | None = None,
    llm_visible: bool = True,
) -> RegisteredTool:
    """构造 RegisteredTool，meta 字段可定制

    agent 默认 'user_mgmt'（业务模块），需 scope check。SHARED_AGENT_CODE 在
    specific 测试中显式传入（验证豁免规则）。
    """
    meta = AiToolMeta(
        name=name,
        agent=agent,
        summary=summary,
        required_perms=(),
        risk=risk,
        hitl_always=hitl_always,
        dry_run_supported=dry_run_supported,
        sensitive_input=sensitive_input,
        accepts_file=accepts_file,
        args_summary_fields=args_summary_fields,
        interaction_flow=interaction_flow,
        prepared_execute_tool=prepared_execute_tool,
        llm_visible=llm_visible,
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
    """sensitive_input 字段禁止出现在函数签名中。"""

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
    """命中敏感字段黑名单的参数必须声明为 sensitive_input。"""

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
    """destructive risk 应显式声明 hitl_always。"""

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
    """high risk 应提供 dry_run_fn。"""

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

    def test_bound_gateway_execute_uses_frozen_preview_instead(self) -> None:
        reg = _make_reg(
            name="user.import_execute",
            risk="high",
            hitl_always=True,
            llm_visible=False,
        )

        assert (
            check_high_risk_requires_dry_run(
                reg,
                prepared_execute_names={"user.import_execute"},
            )
            == []
        )


class TestPreparedGatewayOnlyContract:
    def test_valid_prepared_binding_passes_static_checks(self) -> None:
        prepare = _make_reg(
            name="user.import_preview",
            interaction_flow="prepared",
            prepared_execute_tool="user.import_execute",
        )
        execute = _make_reg(
            name="user.import_execute",
            risk="high",
            hitl_always=True,
            llm_visible=False,
        )

        tools = [prepare, execute]
        assert check_prepared_binding_valid(tools) == []
        assert check_gateway_only_tool_not_llm_visible(tools) == []

    def test_missing_execute_target_fails_static_check(self) -> None:
        prepare = _make_reg(
            name="user.import_preview",
            interaction_flow="prepared",
            prepared_execute_tool="user.import_execute",
        )

        violations = check_prepared_binding_valid([prepare])

        assert [item.check for item in violations] == ["prepared_binding_valid"]

    def test_visible_execute_target_fails_static_check(self) -> None:
        prepare = _make_reg(
            name="user.import_preview",
            interaction_flow="prepared",
            prepared_execute_tool="user.import_execute",
        )
        execute = _make_reg(
            name="user.import_execute",
            risk="high",
            hitl_always=True,
            llm_visible=True,
        )

        violations = check_gateway_only_tool_not_llm_visible([prepare, execute])

        assert [item.check for item in violations] == [
            "gateway_only_tool_not_llm_visible"
        ]

    def test_execute_without_forced_confirmation_fails_static_check(self) -> None:
        prepare = _make_reg(
            name="user.import_preview",
            interaction_flow="prepared",
            prepared_execute_tool="user.import_execute",
        )
        execute = _make_reg(
            name="user.import_execute",
            risk="high",
            hitl_always=False,
            llm_visible=False,
        )

        violations = check_prepared_binding_valid([prepare, execute])

        assert [item.check for item in violations] == ["prepared_binding_valid"]


class TestScopeParamRequiresCheck:
    """签名含 *_id 或 *_ids 参数时必须调用 ensure_targets_in_scope。"""

    def test_no_scope_param_no_violation(self) -> None:
        reg = _make_reg()
        fn_src = "async def _tool(ctx, status): pass"
        assert check_scope_param_requires_check(reg, fn_src) == []

    def test_job_exemption_is_exact_to_job_agent(self) -> None:
        reg = _make_reg(agent="user_mgmt")
        fn_src = "async def _tool(ctx, job_id: str): return {}"

        violations = check_scope_param_requires_check(reg, fn_src)

        assert len(violations) == 1
        assert "job_id" in violations[0].detail

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
        """ctx 参数不应被视为 scope 参数。"""
        reg = _make_reg()
        fn_src = "async def _tool(ctx): pass"
        assert check_scope_param_requires_check(reg, fn_src) == []

    def test_shared_agent_exempt(self) -> None:
        """SHARED_AGENT_CODE 的非业务资源参数可豁免通用 scope check。"""
        reg = _make_reg(agent="shared")
        fn_src = "async def _tool(ctx, file_id: str): return {}"
        assert check_scope_param_requires_check(reg, fn_src) == []

    def test_job_id_uses_global_permission_scope(self) -> None:
        reg = _make_reg(agent="job_mgmt")
        fn_src = "async def _tool(ctx, job_id: str): return {}"

        assert check_scope_param_requires_check(reg, fn_src) == []


class TestFileParamRequiresProtectedLoader:
    """file_id 是受保护资源，shared tools 也不例外。"""

    def test_file_id_with_shared_loader_has_no_violation(self) -> None:
        reg = _make_reg()
        fn_src = """
async def _tool(ctx, file_id: str):
    return await load_protected_file(ctx, file_id, policy=POLICY)
"""
        assert check_file_param_requires_protected_loader(reg, fn_src) == []

    def test_file_id_with_business_wrapper_has_no_violation(self) -> None:
        reg = _make_reg()
        fn_src = """
async def _tool(ctx, file_id: str):
    return await _load_file_bytes(ctx, file_id)
"""
        assert check_file_param_requires_protected_loader(reg, fn_src) == []

    def test_file_id_with_bare_query_is_rejected(self) -> None:
        reg = _make_reg(agent="shared")
        fn_src = """
async def _tool(ctx, file_id: str):
    record = await ctx.db.get(File, int(file_id))
    return Path(record.file_path).read_bytes()
"""
        violations = check_file_param_requires_protected_loader(reg, fn_src)

        assert len(violations) == 1
        assert violations[0].check == "file_param_requires_protected_loader"

    def test_similar_helper_name_does_not_bypass_check(self) -> None:
        reg = _make_reg(agent="shared")
        fn_src = """
async def _tool(ctx, file_id: str):
    return await unsafe_load_protected_file(ctx, file_id)
"""

        violations = check_file_param_requires_protected_loader(reg, fn_src)

        assert len(violations) == 1
        assert violations[0].check == "file_param_requires_protected_loader"

    def test_tool_without_file_id_is_ignored(self) -> None:
        reg = _make_reg()
        fn_src = "async def _tool(ctx, user_id: int): return {}"

        assert check_file_param_requires_protected_loader(reg, fn_src) == []


class TestBuiltinScanSurface:
    def test_all_20_mandatory_builtins_are_loaded(self) -> None:
        names = {tool.meta.name for tool in load_all_tools()}

        assert len(EXPECTED_BUILTIN_TOOL_NAMES) == 22
        assert EXPECTED_BUILTIN_TOOL_NAMES <= names

    def test_mandatory_module_import_failure_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = importlib.import_module

        def fail_job_module(name: str):
            if name == "app.modules.job.ai_tools":
                raise ImportError("broken mandatory module")
            return real_import(name)

        monkeypatch.setattr(
            "scripts.check_ai_tools.importlib.import_module",
            fail_job_module,
        )

        with pytest.raises(ImportError, match="broken mandatory module"):
            load_all_tools()


class TestAcceptsFileMimeValid:
    """accepts_file 中的 MIME 必须在解析器覆盖范围内。"""

    def test_no_accepts_file_no_violation(self) -> None:
        reg = _make_reg(accepts_file=())
        assert check_accepts_file_mime_valid(reg) == []

    def test_valid_csv_mime_no_violation(self) -> None:
        reg = _make_reg(accepts_file=("text/csv", "text/plain"))
        assert check_accepts_file_mime_valid(reg) == []

    def test_valid_xlsx_mime_no_violation(self) -> None:
        reg = _make_reg(
            accepts_file=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )
        assert check_accepts_file_mime_valid(reg) == []

    def test_invalid_mime_violation(self) -> None:
        reg = _make_reg(accepts_file=("application/vnd.msexcel",))  # typo
        violations = check_accepts_file_mime_valid(reg)
        assert len(violations) == 1
        assert violations[0].check == "accepts_file_mime_valid"
        assert "application/vnd.msexcel" in violations[0].detail

    def test_partial_invalid_mime_violation(self) -> None:
        """混合合法/非法 MIME，仅非法的报错（detail 的 invalid list 不含合法项）"""
        reg = _make_reg(accepts_file=("text/csv", "image/png"))
        violations = check_accepts_file_mime_valid(reg)
        assert len(violations) == 1
        assert "image/png" in violations[0].detail
        # invalid list 应只含 image/png，不含 text/csv
        # detail 形如 "...invalid ['image/png']，已知 parser 覆盖: [...'text/csv'...]"
        # 取 invalid list 段（"含未支持的 MIME [" 到 "]，"之间）单独断言
        detail = violations[0].detail
        invalid_segment = detail.split("含未支持的 MIME ")[1].split("，")[0]
        assert "image/png" in invalid_segment
        assert "text/csv" not in invalid_segment


class TestSummaryLengthLimit:
    """summary 不得超过 100 个 Unicode 字符。"""

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
    """args_summary_fields 不得包含敏感字段名。"""

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
        词边界检查应放过该字段，与 GLOBAL_OUTPUT_BLOCKLIST 的逻辑一致。"""
        reg = _make_reg(args_summary_fields=("csrf_token", "pagination_token"))
        # 注意：csrf_token 不应被检出（'token' 是 SENSITIVE_INPUT_BLOCKLIST 里的项，
        # 但 csrf_token 既不是完全相等 'token'，也不是 'token_xxx' 前缀模式，
        # 而是后缀。按 word-boundary 应放过）
        violations = check_args_summary_fields_not_sensitive(reg)
        # 当前实现用 startswith(bl + "_") 检前缀，csrf_token 不命中
        assert violations == []

    """dry_run_supported=True 时必须实现 ``_dry_run_<tool>``。"""

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
