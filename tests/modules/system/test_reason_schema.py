"""ReasonSchema 与业务 reason 一致性校验测试。

覆盖四个核心边界：
- test_reason_required_validation：缺失 / 空 / 全空白 → ValidationError
- test_reason_max_length_256：超 256 字符 → ValidationError
- test_reason_mismatch_between_preview_and_execute_rejected：一致性校验
- test_reason_consistent_passes：一致时不抛
"""

import pytest
from pydantic import ValidationError

from app.core.exceptions import BusinessRuleException
from app.modules.system.schemas.user_transfer import ReasonSchema
from app.modules.system.service.user_import_state import validate_reason_consistency


class _SampleRequest(ReasonSchema):
    """样本：业务 schema 多继承 ReasonSchema 验证 mixin 行为。"""

    extra: str = "x"


class TestReasonSchema:
    def test_valid_reason_passes(self):
        req = _SampleRequest(reason="2026年8月 HR 入职名单同步")
        assert req.reason == "2026年8月 HR 入职名单同步"

    def test_reason_required_validation(self):
        with pytest.raises(ValidationError):
            _SampleRequest(reason="")

    def test_reason_whitespace_only_rejected(self):
        with pytest.raises(ValidationError):
            _SampleRequest(reason="   ")

    def test_reason_missing_rejected(self):
        with pytest.raises(ValidationError):
            _SampleRequest()  # type: ignore[call-arg]

    def test_reason_max_length_256(self):
        with pytest.raises(ValidationError):
            _SampleRequest(reason="x" * 257)

    def test_reason_min_length_1(self):
        req = _SampleRequest(reason="x")
        assert req.reason == "x"

    def test_reason_max_length_boundary(self):
        req = _SampleRequest(reason="x" * 256)
        assert len(req.reason) == 256


class TestReasonConsistency:
    def test_consistent_reasons_pass(self):
        validate_reason_consistency("HR 同步", "HR 同步")

    def test_inconsistent_reasons_rejected(self):
        with pytest.raises(BusinessRuleException) as exc:
            validate_reason_consistency("HR 同步", "ERP 推送")
        assert exc.value.error_code == "AI_IMPORT_REASON_MISMATCH"

    def test_case_sensitive_comparison(self):
        """reason 区分大小写（审计字符串精确匹配）。"""
        with pytest.raises(BusinessRuleException):
            validate_reason_consistency("HR 同步", "hr 同步")

    def test_whitespace_difference_rejected(self):
        """前后空格差异视为不一致（防用户绕过一致性）。"""
        with pytest.raises(BusinessRuleException):
            validate_reason_consistency("HR 同步", "HR 同步 ")
