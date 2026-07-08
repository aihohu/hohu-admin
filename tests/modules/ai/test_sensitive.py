"""敏感数据脱敏单元测试（serialize_for_llm + _scrub_fields + redact_secrets）

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §7.3 / §7.4。
"""

# ruff: noqa: ARG001

from pydantic import BaseModel

from app.modules.ai.agents.gateway.redact import (
    REDACT_PATTERNS,
    contains_redacted_marker,
    redact_secrets,
)
from app.modules.ai.agents.gateway.sensitive import (
    GLOBAL_OUTPUT_BLOCKLIST,
    _scrub_fields,
    serialize_for_llm,
)

# ============ _scrub_fields ============


class TestScrubFields:
    def test_exact_match_removed(self) -> None:
        result = _scrub_fields(
            {"password": "abc", "name": "alice"}, GLOBAL_OUTPUT_BLOCKLIST
        )
        assert result == {"name": "alice"}

    def test_substring_match_removed(self) -> None:
        """password_hash 命中 password（子串匹配）"""
        result = _scrub_fields(
            {"password_hash": "$2b$12$...", "name": "alice"},
            GLOBAL_OUTPUT_BLOCKLIST,
        )
        assert result == {"name": "alice"}

    def test_case_insensitive(self) -> None:
        """Password / PASSWORD / password 都命中"""
        result = _scrub_fields(
            {"Password": "x", "API_KEY": "y", "Token": "z", "name": "alice"},
            GLOBAL_OUTPUT_BLOCKLIST,
        )
        assert result == {"name": "alice"}

    def test_nested_dict_recursive(self) -> None:
        result = _scrub_fields(
            {"user": {"name": "alice", "api_key": "sk-xxx"}, "ok": True},
            GLOBAL_OUTPUT_BLOCKLIST,
        )
        assert result == {"user": {"name": "alice"}, "ok": True}

    def test_list_of_dicts_recursive(self) -> None:
        result = _scrub_fields(
            {"items": [{"name": "a", "token": "t1"}, {"name": "b", "secret": "s2"}]},
            GLOBAL_OUTPUT_BLOCKLIST,
        )
        assert result == {"items": [{"name": "a"}, {"name": "b"}]}

    def test_non_dict_returns_unchanged(self) -> None:
        assert _scrub_fields("string", GLOBAL_OUTPUT_BLOCKLIST) == "string"
        assert _scrub_fields(42, GLOBAL_OUTPUT_BLOCKLIST) == 42
        assert _scrub_fields(None, GLOBAL_OUTPUT_BLOCKLIST) is None

    def test_clean_payload_passes_through(self) -> None:
        payload = {"name": "alice", "age": 30, "dept": {"name": "工程部"}}
        result = _scrub_fields(payload, GLOBAL_OUTPUT_BLOCKLIST)
        assert result == payload


# ============ serialize_for_llm ============


class TestSerializeForLlm:
    def test_dict_with_declared_sensitive_output(self) -> None:
        """业务方声明 sensitive_output + 全局黑名单兜底"""
        result = serialize_for_llm(
            sensitive_output=("my_secret_field",),
            raw_result={"name": "alice", "my_secret_field": "xxx", "password": "abc"},
        )
        assert result == {"name": "alice"}

    def test_basemodel_serialized(self) -> None:
        class UserOut(BaseModel):
            name: str
            password_hash: str
            api_key: str | None = None

        result = serialize_for_llm(
            sensitive_output=(),
            raw_result=UserOut(name="alice", password_hash="$2b$12", api_key="sk-x"),
        )
        assert result == {"name": "alice"}

    def test_list_of_dicts_scrubbed(self) -> None:
        result = serialize_for_llm(
            sensitive_output=(),
            raw_result=[
                {"name": "a", "token": "t1"},
                {"name": "b", "api_key": "k2"},
            ],
        )
        assert result == [{"name": "a"}, {"name": "b"}]

    def test_scalar_returns_unchanged(self) -> None:
        """标量无字段名，无法 scrub"""
        assert serialize_for_llm((), 42) == 42
        assert serialize_for_llm((), "hello") == "hello"
        assert serialize_for_llm((), None) is None

    def test_list_of_scalars_returns_unchanged(self) -> None:
        assert serialize_for_llm((), ["a", "b", "c"]) == ["a", "b", "c"]

    def test_global_blocklist_takes_precedence(self) -> None:
        """即使业务方未声明 sensitive_output，全局黑名单也兜底"""
        result = serialize_for_llm(
            sensitive_output=(),
            raw_result={"name": "x", "password": "y"},
        )
        assert result == {"name": "x"}

    def test_private_key_field_scrubbed(self) -> None:
        """spec 提到 PrivateKey 等格式（敏感字段）"""
        result = serialize_for_llm(
            sensitive_output=(),
            raw_result={"name": "x", "private_key": "-----BEGIN RSA PRIVATE KEY-----"},
        )
        assert result == {"name": "x"}


# ============ redact_secrets ============


class TestRedactSecrets:
    def test_openai_api_key_redacted(self) -> None:
        # OpenAI pattern: sk- + 至少 20 个字符
        text = "我的 key 是 sk-abcd1234efgh5678ijkl9876"
        result = redact_secrets(text)
        assert "[REDACTED:OPENAI_API_KEY]" in result
        assert "sk-abcd" not in result

    def test_aws_access_key_redacted(self) -> None:
        text = "AWS Key: AKIAIOSFODNN7EXAMPLE"
        result = redact_secrets(text)
        assert "[REDACTED:AWS_ACCESS_KEY]" in result
        assert "AKIA" not in result

    def test_jwt_redacted(self) -> None:
        jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3ODkw.pix2lKqFp3-abCdEfGh"
        text = f"token: {jwt}"
        result = redact_secrets(text)
        assert "[REDACTED:JWT]" in result
        assert "eyJhbGci" not in result

    def test_context_sensitive_password(self) -> None:
        text = "password: MySecretPassword12345"
        result = redact_secrets(text)
        assert "[REDACTED:CONTEXT_SENSITIVE]" in result
        assert "MySecretPassword12345" not in result

    def test_context_sensitive_api_key(self) -> None:
        text = "api_key=ABCDEF1234567890ABCD"
        result = redact_secrets(text)
        assert "[REDACTED:CONTEXT_SENSITIVE]" in result

    def test_mime_whitelist_image_skipped(self) -> None:
        """spec §7.4: data:image/* 不被扫描"""
        text = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."
        result = redact_secrets(text)
        assert result == text

    def test_mime_whitelist_pdf_skipped(self) -> None:
        text = "data:application/pdf;base64,JVBERi0xLjQKJcfs..."
        result = redact_secrets(text)
        assert result == text

    def test_clean_text_unchanged(self) -> None:
        text = "你好，请帮我查询用户列表"
        assert redact_secrets(text) == text

    def test_empty_input_unchanged(self) -> None:
        assert redact_secrets("") == ""
        assert redact_secrets(None) is None  # type: ignore[arg-type]

    def test_multiple_patterns_in_same_text(self) -> None:
        """一段文本含多个敏感 pattern 全部脱敏"""
        text = (
            "OpenAI: sk-abc12345defgh67890ijklmnop\n"
            "AWS: AKIAIOSFODNN7EXAMPLE\n"
            "DB: password=SuperSecretPass123"
        )
        result = redact_secrets(text)
        assert "[REDACTED:OPENAI_API_KEY]" in result
        assert "[REDACTED:AWS_ACCESS_KEY]" in result
        assert "[REDACTED:CONTEXT_SENSITIVE]" in result


# ============ contains_redacted_marker ============


class TestContainsRedactedMarker:
    def test_contains_marker(self) -> None:
        assert contains_redacted_marker("[REDACTED:OPENAI_API_KEY]") is True

    def test_no_marker(self) -> None:
        assert contains_redacted_marker("正常文本") is False

    def test_empty_text(self) -> None:
        assert contains_redacted_marker("") is False

    def test_redact_produces_marker(self) -> None:
        """脱敏后的文本含标记，SAFETY_PREAMBLE 第 3 条规则可识别"""
        redacted = redact_secrets("key: sk-abc12345defgh67890ijklmnop")
        assert contains_redacted_marker(redacted) is True


# ============ REDACT_PATTERNS 完整性 ============


class TestRedactPatterns:
    def test_four_patterns_defined(self) -> None:
        """spec §7.4 4 类 pattern"""
        names = {name for name, _ in REDACT_PATTERNS}
        assert names == {
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY",
            "JWT",
            "CONTEXT_SENSITIVE",
        }
