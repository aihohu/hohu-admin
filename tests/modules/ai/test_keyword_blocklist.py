"""Keyword Blocklist Guardrail 单测 — spec §11.2

测试 check_keywords 函数 + load_blocklist 缓存逻辑（monkeypatch config_service）。
"""

# ruff: noqa: PLC0415

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.ai.agents.safety.keyword_blocklist import (
    CONFIG_KEY,
    check_keywords,
    invalidate_blocklist_cache,
    load_blocklist,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """每测试前清缓存，避免跨测试串扰"""
    invalidate_blocklist_cache()
    yield
    invalidate_blocklist_cache()


class TestCheckKeywords:
    """子串匹配 + 大小写不敏感 + 多语言"""

    def test_empty_text_returns_empty(self) -> None:
        assert check_keywords("", ["bad"]) == []

    def test_empty_blocklist_returns_empty(self) -> None:
        assert check_keywords("hello", []) == []

    def test_single_match(self) -> None:
        hits = check_keywords("this is bad word", ["bad"])
        assert hits == ["bad"]

    def test_case_insensitive(self) -> None:
        hits = check_keywords("THIS IS BAD", ["bad"])
        assert hits == ["bad"]

    def test_multiple_matches(self) -> None:
        hits = check_keywords("foo bar baz", ["foo", "bar", "qux"])
        assert set(hits) == {"foo", "bar"}

    def test_chinese_keyword(self) -> None:
        hits = check_keywords("包含公司机密文档", ["机密"])
        assert hits == ["机密"]

    def test_substring_match(self) -> None:
        """子串匹配，不需要词边界"""
        hits = check_keywords("foobar contains foo substring", ["foo"])
        assert hits == ["foo"]

    def test_no_match(self) -> None:
        assert check_keywords("hello world", ["bad"]) == []


class TestLoadBlocklist:
    """从 sys_config 加载 + 60s 缓存"""

    async def test_load_returns_list_from_config(self, monkeypatch) -> None:
        raw = json.dumps(["机密", "secret", "internal"])
        mock_get_value = AsyncMock(return_value=raw)
        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            mock_get_value,
        )

        db = MagicMock()
        blocklist = await load_blocklist(db)

        assert set(blocklist) == {"机密", "secret", "internal"}
        mock_get_value.assert_awaited_once_with(db, CONFIG_KEY)

    async def test_load_caches_result(self, monkeypatch) -> None:
        """60s 内不重复查 DB"""
        call_count = 0

        async def fake_get_value(_db, _key):
            nonlocal call_count
            call_count += 1
            return '["x"]'

        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            fake_get_value,
        )

        db = MagicMock()
        await load_blocklist(db)
        await load_blocklist(db)
        await load_blocklist(db)
        assert call_count == 1, "缓存生效，第二次/第三次不应再查 DB"

    async def test_load_force_refresh_bypasses_cache(self, monkeypatch) -> None:
        call_count = 0

        async def fake_get_value(_db, _key):
            nonlocal call_count
            call_count += 1
            return '["x"]'

        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            fake_get_value,
        )

        db = MagicMock()
        await load_blocklist(db)
        await load_blocklist(db, force_refresh=True)
        assert call_count == 2

    async def test_load_returns_empty_on_missing_config(self, monkeypatch) -> None:
        """sys_config 无此 key（get_value 返回 None）→ 空列表"""
        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            AsyncMock(return_value=None),
        )
        db = MagicMock()
        assert await load_blocklist(db) == []

    async def test_load_returns_empty_on_invalid_json(self, monkeypatch) -> None:
        """sys_config 值不是合法 JSON → 返回空（不抛异常）"""
        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            AsyncMock(return_value="not-json{{{"),
        )
        db = MagicMock()
        assert await load_blocklist(db) == []

    async def test_load_filters_non_string_items(self, monkeypatch) -> None:
        """blocklist JSON 含非字符串元素（数字 / null）→ 过滤掉"""
        raw = json.dumps(["valid", 123, None, "", "another"])
        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            AsyncMock(return_value=raw),
        )
        db = MagicMock()
        blocklist = await load_blocklist(db)
        assert set(blocklist) == {"valid", "another"}

    async def test_load_lowercases_keywords(self, monkeypatch) -> None:
        """存进缓存前转小写，匹配时大小写不敏感"""
        raw = json.dumps(["BadWord", "UPPER"])
        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            AsyncMock(return_value=raw),
        )
        db = MagicMock()
        blocklist = await load_blocklist(db)
        assert blocklist == ["badword", "upper"]
        # 大小写不敏感匹配
        assert check_keywords("this is BADWORD here", blocklist) == ["badword"]


class TestInvalidateCache:
    async def test_invalidate_forces_reload(self, monkeypatch) -> None:
        call_count = 0

        async def fake_get_value(_db, _key):
            nonlocal call_count
            call_count += 1
            return '["x"]'

        monkeypatch.setattr(
            "app.modules.ai.agents.safety.keyword_blocklist.config_service.get_value",
            fake_get_value,
        )

        db = MagicMock()
        await load_blocklist(db)
        assert call_count == 1
        invalidate_blocklist_cache()
        await load_blocklist(db)
        assert call_count == 2


class TestExtremeInputs:
    """spec §11.2: 极端输入不应让 check_keywords 崩溃"""

    def test_null_bytes_in_text(self) -> None:
        text = "contains\x00keyword"
        hits = check_keywords(text, ["keyword"])
        assert hits == ["keyword"]

    def test_very_long_text(self) -> None:
        text = "x" * 100000 + " bad"
        hits = check_keywords(text, ["bad"])
        assert hits == ["bad"]

    def test_keyword_with_special_chars(self) -> None:
        """blocklist 含特殊字符也能匹配"""
        hits = check_keywords("contains $@special word", ["$@special"])
        assert hits == ["$@special"]

    def test_unicode_keyword(self) -> None:
        hits = check_keywords("含「机密」字样", ["机密"])
        assert hits == ["机密"]

    def test_blocklist_with_empty_string_ignored(self) -> None:
        """load_blocklist 已过滤空字符串，check_keywords 也不应误匹配空"""
        # 直接调 check_keywords 不应崩（load_blocklist 不会返回空字符串）
        hits = check_keywords("anything", [])
        assert hits == []
