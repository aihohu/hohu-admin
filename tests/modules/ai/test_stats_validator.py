"""stats_validator 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.5 / §9.6。
"""

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.stats_validator import (
    validate_field_in_whitelist,
    validate_filters_in_whitelist,
    validate_group_by_in_whitelist,
)


def _make_meta(
    allowed_filters: tuple[str, ...] = ("status", "user_gender"),
    allowed_group_by: tuple[str, ...] = ("user_gender", "status"),
) -> AiToolMeta:
    return AiToolMeta(
        name="user.stats",
        agent="user_mgmt",
        summary="x",
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=allowed_filters,
        allowed_group_by=allowed_group_by,
        max_groups=20,
    )


class TestValidateFiltersInWhitelist:
    def test_none_returns_empty_dict(self) -> None:
        """filters=None → {}（业务函数统一处理）"""
        result = validate_filters_in_whitelist(_make_meta(), None)
        assert result == {}

    def test_empty_dict_returns_empty(self) -> None:
        result = validate_filters_in_whitelist(_make_meta(), {})
        assert result == {}

    def test_all_in_whitelist_returns_unchanged(self) -> None:
        filters = {"status": "1", "user_gender": "2"}
        result = validate_filters_in_whitelist(_make_meta(), filters)
        assert result is filters

    def test_disallowed_field_raises(self) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            validate_filters_in_whitelist(
                _make_meta(), {"phone": "13800000000", "status": "1"}
            )
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"
        assert "phone" in exc_info.value.message

    def test_empty_whitelist_rejects_any_filters(self) -> None:
        """meta.allowed_filters=() 但用户传了 filters → 全部越界"""
        meta = _make_meta(allowed_filters=())
        with pytest.raises(BusinessRuleException) as exc_info:
            validate_filters_in_whitelist(meta, {"status": "1"})
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"

    def test_empty_whitelist_allows_empty_filters(self) -> None:
        meta = _make_meta(allowed_filters=())
        assert validate_filters_in_whitelist(meta, {}) == {}
        assert validate_filters_in_whitelist(meta, None) == {}


class TestValidateGroupByInWhitelist:
    def test_none_returns_first_in_whitelist(self) -> None:
        """group_by=None → 默认第一个，业务函数友好"""
        result = validate_group_by_in_whitelist(_make_meta(), None)
        assert result == "user_gender"  # allowed_group_by[0]

    def test_in_whitelist_returns_unchanged(self) -> None:
        assert validate_group_by_in_whitelist(_make_meta(), "status") == "status"

    def test_disallowed_raises(self) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            validate_group_by_in_whitelist(_make_meta(), "phone")
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"

    def test_empty_whitelist_raises(self) -> None:
        meta = _make_meta(allowed_group_by=())
        with pytest.raises(BusinessRuleException, match="不支持 group_by"):
            validate_group_by_in_whitelist(meta, None)


class TestValidateFieldInWhitelist:
    def test_in_whitelist_returns_unchanged(self) -> None:
        assert validate_field_in_whitelist(_make_meta(), "status") == "status"

    def test_disallowed_raises(self) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            validate_field_in_whitelist(_make_meta(), "user_phone")
        assert exc_info.value.error_code == "AI_STATS_FIELD_NOT_ALLOWED"

    def test_empty_whitelist_raises(self) -> None:
        meta = _make_meta(allowed_group_by=())
        with pytest.raises(BusinessRuleException):
            validate_field_in_whitelist(meta, "status")
