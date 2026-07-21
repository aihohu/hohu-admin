"""Forbidden Topics / Forbidden URLs Guardrails 单测 — spec §11.2 v1.5+ SR-23

测试 check_topics / check_forbidden_urls 函数 + load_* 缓存逻辑（monkeypatch config_service）。
"""

# ruff: noqa: PLC0415

import json
from unittest.mock import AsyncMock

import pytest

from app.modules.ai.agents.safety.forbidden_topics import (
    CONFIG_KEY as TOPICS_CONFIG_KEY,
)
from app.modules.ai.agents.safety.forbidden_topics import (
    check_topics,
    invalidate_forbidden_topics_cache,
    load_forbidden_topics,
)
from app.modules.ai.agents.safety.forbidden_urls import (
    CONFIG_KEY as URLS_CONFIG_KEY,
)
from app.modules.ai.agents.safety.forbidden_urls import (
    check_forbidden_urls,
    invalidate_forbidden_urls_cache,
    load_forbidden_urls,
)


@pytest.fixture(autouse=True)
def reset_caches():
    invalidate_forbidden_topics_cache()
    invalidate_forbidden_urls_cache()
    yield
    invalidate_forbidden_topics_cache()
    invalidate_forbidden_urls_cache()


# ============ forbidden_topics ============


class TestCheckTopics:
    """与 keyword_blocklist 同模式：子串 + 大小写不敏感"""

    def test_empty_text_no_hits(self) -> None:
        assert check_topics("", ["政治"]) == []

    def test_empty_blocklist_no_hits(self) -> None:
        assert check_topics("anything", []) == []

    def test_exact_match(self) -> None:
        assert check_topics("讨论政治问题", ["政治"]) == ["政治"]

    def test_case_insensitive(self) -> None:
        assert check_topics("Discuss POLITICS here", ["politics"]) == ["politics"]

    def test_multiple_hits(self) -> None:
        hits = check_topics("讨论政治和宗教", ["政治", "宗教"])
        assert set(hits) == {"政治", "宗教"}

    def test_no_match_returns_empty(self) -> None:
        assert check_topics("讨论天气", ["政治", "宗教"]) == []


class TestLoadForbiddenTopics:
    async def test_load_returns_lowercased_list(self, db_session) -> None:
        raw = json.dumps(["POLITICS", "政治"])
        with (
            pytest.MonkeyPatch().context() as mp,
        ):
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_topics.config_service.get_value",
                AsyncMock(return_value=raw),
            )
            result = await load_forbidden_topics(db_session)
        assert "politics" in result
        assert "政治" in result

    async def test_load_caches_within_ttl(self, db_session) -> None:
        mock_get = AsyncMock(return_value='["topic1"]')
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_topics.config_service.get_value",
                mock_get,
            )
            r1 = await load_forbidden_topics(db_session)
            r2 = await load_forbidden_topics(db_session)
        assert r1 == r2 == ["topic1"]
        assert mock_get.call_count == 1  # 第二次走缓存

    async def test_load_force_refresh_bypasses_cache(self, db_session) -> None:
        mock_get = AsyncMock(return_value='["fresh"]')
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_topics.config_service.get_value",
                mock_get,
            )
            await load_forbidden_topics(db_session)
            await load_forbidden_topics(db_session, force_refresh=True)
        assert mock_get.call_count == 2

    async def test_load_invalid_json_returns_empty(self, db_session) -> None:
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_topics.config_service.get_value",
                AsyncMock(return_value="not json{"),
            )
            result = await load_forbidden_topics(db_session)
        assert result == []

    async def test_load_non_string_elements_filtered(self, db_session) -> None:
        raw = json.dumps(["ok", 123, None, "", "good"])
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_topics.config_service.get_value",
                AsyncMock(return_value=raw),
            )
            result = await load_forbidden_topics(db_session)
        assert result == ["ok", "good"]


# ============ forbidden_urls ============


class TestCheckForbiddenUrls:
    """URL 提取 + 域名精确/后缀匹配"""

    def test_empty_text_no_hits(self) -> None:
        assert check_forbidden_urls("", ["evil.com"]) == []

    def test_empty_blocklist_no_hits(self) -> None:
        assert check_forbidden_urls("https://anything.com", []) == []

    def test_https_url_exact_match(self) -> None:
        assert check_forbidden_urls("visit https://evil.com now", ["evil.com"]) == [
            "evil.com"
        ]

    def test_http_url_match(self) -> None:
        assert check_forbidden_urls("http://evil.com/path", ["evil.com"]) == [
            "evil.com"
        ]

    def test_www_prefix_match(self) -> None:
        assert check_forbidden_urls("see www.evil.com for details", ["evil.com"]) == [
            "evil.com"
        ]

    def test_bare_domain_match(self) -> None:
        assert check_forbidden_urls("contact evil.com support", ["evil.com"]) == [
            "evil.com"
        ]

    def test_subdomain_suffix_match(self) -> None:
        """后缀匹配：sub.evil.com 命中 evil.com（注册级黑名单覆盖子域）"""
        assert check_forbidden_urls("https://sub.evil.com/article", ["evil.com"]) == [
            "evil.com"
        ]

    def test_different_tld_not_mismatched(self) -> None:
        """evil.com.txt 不应命中 evil.com（域边界）"""
        # 实际提取出的域名是 evil.com.txt，与 evil.com 不是后缀关系（domain boundary）
        result = check_forbidden_urls("https://evil.com.txt", ["evil.com"])
        # 注意：URL regex 提取 group 是 `evil.com.txt`（TLD=.txt），
        # endswith(".evil.com") 检查：'evil.com.txt'.endswith('.evil.com') = False
        # → 不命中（正确，因为 evil.com.txt 是另一个合法域名）
        assert result == []

    def test_multiple_urls_with_one_hit(self) -> None:
        text = "visit https://good.com and https://evil.com today"
        assert check_forbidden_urls(text, ["evil.com"]) == ["evil.com"]

    def test_multiple_hits_deduplicated(self) -> None:
        """同一域名多次出现只算一次命中"""
        text = "https://evil.com/a and https://evil.com/b"
        assert check_forbidden_urls(text, ["evil.com"]) == ["evil.com"]

    def test_port_in_url(self) -> None:
        """URL 含 port 时仍正确提取域名"""
        assert check_forbidden_urls("https://evil.com:8080/x", ["evil.com"]) == [
            "evil.com"
        ]

    def test_no_url_in_text(self) -> None:
        assert check_forbidden_urls("just plain text no url", ["evil.com"]) == []

    def test_case_insensitive_domain(self) -> None:
        """URL 域名大小写不敏感（HTTPS://EVIL.COM 也命中）"""
        assert check_forbidden_urls("HTTPS://EVIL.COM", ["evil.com"]) == ["evil.com"]


class TestLoadForbiddenUrls:
    async def test_load_normalizes_domains(self, db_session) -> None:
        """配置项含 protocol / path 时规范化为纯域名"""
        raw = json.dumps(["https://evil.com/path", "http://bad.org", "competitor.net"])
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_urls.config_service.get_value",
                AsyncMock(return_value=raw),
            )
            result = await load_forbidden_urls(db_session)
        assert "evil.com" in result
        assert "bad.org" in result
        assert "competitor.net" in result

    async def test_load_strips_port(self, db_session) -> None:
        raw = json.dumps(["evil.com:8080"])
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_urls.config_service.get_value",
                AsyncMock(return_value=raw),
            )
            result = await load_forbidden_urls(db_session)
        assert result == ["evil.com"]

    async def test_load_caches_within_ttl(self, db_session) -> None:
        mock_get = AsyncMock(return_value='["x.com"]')
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_urls.config_service.get_value",
                mock_get,
            )
            await load_forbidden_urls(db_session)
            await load_forbidden_urls(db_session)
        assert mock_get.call_count == 1

    async def test_load_invalid_json_returns_empty(self, db_session) -> None:
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                "app.modules.ai.agents.safety.forbidden_urls.config_service.get_value",
                AsyncMock(return_value="not json"),
            )
            result = await load_forbidden_urls(db_session)
        assert result == []


# ============ CONFIG_KEY 常量验证（防改名静默漂移） ============


def test_topics_config_key() -> None:
    assert TOPICS_CONFIG_KEY == "ai:guardrail:forbidden_topics"


def test_urls_config_key() -> None:
    assert URLS_CONFIG_KEY == "ai:guardrail:forbidden_urls"
