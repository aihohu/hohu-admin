"""ai_config helper 单元测试（spec §11.2 / §5.4 SR-17）

覆盖 get_ai_config_int / get_ai_config_str / get_ai_config_str_list 三个 helper。
Redis 用 db_session fixture 提供的真实 DB（清 ai:* sys_config 行隔离）。
"""

# ruff: noqa: ARG001, PLC0415

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前清 ai_config 模块级缓存"""
    from app.modules.ai.agents.safety import ai_config

    ai_config._cache.clear()
    yield
    ai_config._cache.clear()


class TestGetAiConfigInt:
    async def test_returns_default_when_key_missing(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_int

        result = await get_ai_config_int(db_session, "ai:nonexistent:key", 42)
        assert result == 42

    async def test_returns_value_when_key_exists(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_int

        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value="100"),
        ):
            result = await get_ai_config_int(db_session, "ai:test:int", 42)
        assert result == 100

    async def test_invalid_int_falls_back_to_default(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_int

        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value="not_a_number"),
        ):
            result = await get_ai_config_int(db_session, "ai:test:bad_int", 99)
        assert result == 99


class TestGetAiConfigStrList:
    """v1.5+ SR-17：JSON 数组解析（ai:enabled_tools 用）"""

    async def test_returns_default_when_key_missing(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        result = await get_ai_config_str_list(
            db_session, "ai:nonexistent:list", ["fallback"]
        )
        assert result == ["fallback"]

    async def test_parses_valid_json_array(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        raw = json.dumps(["file.parse", "provider.export"])
        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value=raw),
        ):
            result = await get_ai_config_str_list(db_session, "ai:enabled_tools", [])
        assert result == ["file.parse", "provider.export"]

    async def test_invalid_json_falls_back_to_default(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value="not json{"),
        ):
            result = await get_ai_config_str_list(
                db_session, "ai:enabled_tools", ["default_tool"]
            )
        assert result == ["default_tool"]

    async def test_non_list_json_falls_back_to_default(self, db_session) -> None:
        """JSON 是 dict / str / int 而非 list → 回退 default"""
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value='{"key": "value"}'),
        ):
            result = await get_ai_config_str_list(
                db_session, "ai:enabled_tools", ["default"]
            )
        assert result == ["default"]

    async def test_list_with_non_str_elements_falls_back(self, db_session) -> None:
        """list 含 int / null 等非 str 元素 → 回退 default（防 silent 数据污染）"""
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value='["ok", 123, null]'),
        ):
            result = await get_ai_config_str_list(
                db_session, "ai:enabled_tools", ["default"]
            )
        assert result == ["default"]

    async def test_empty_array_returns_empty(self, db_session) -> None:
        """合法空数组 '[]' → 返回 []（不是 default）"""
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=AsyncMock(return_value="[]"),
        ):
            result = await get_ai_config_str_list(
                db_session, "ai:enabled_tools", ["default"]
            )
        assert result == []

    async def test_cache_hits_within_ttl(self, db_session) -> None:
        """60s TTL 内重复调用走缓存（不重复查 DB）"""
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        mock_get = AsyncMock(return_value='["cached_tool"]')
        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=mock_get,
        ):
            r1 = await get_ai_config_str_list(db_session, "ai:test:cache", [])
            r2 = await get_ai_config_str_list(db_session, "ai:test:cache", [])
        assert r1 == ["cached_tool"]
        assert r2 == ["cached_tool"]
        # 第二次走缓存，DB 只查 1 次
        assert mock_get.call_count == 1

    async def test_force_refresh_bypasses_cache(self, db_session) -> None:
        from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list

        mock_get = AsyncMock(return_value='["fresh"]')
        with patch(
            "app.modules.ai.agents.safety.ai_config.config_service.get_value",
            new=mock_get,
        ):
            await get_ai_config_str_list(db_session, "ai:test:force", [])
            await get_ai_config_str_list(
                db_session, "ai:test:force", [], force_refresh=True
            )
        assert mock_get.call_count == 2


class TestInvalidateCache:
    def test_clears_only_matching_prefix(self) -> None:
        from app.modules.ai.agents.safety import ai_config
        from app.modules.ai.agents.safety.ai_config import invalidate_ai_config_cache

        ai_config._cache["ai:foo"] = (1, 0.0)
        ai_config._cache["ai:bar"] = (2, 0.0)
        ai_config._cache["other:key"] = (3, 0.0)

        invalidate_ai_config_cache("ai:foo")

        assert "ai:foo" not in ai_config._cache
        assert "ai:bar" in ai_config._cache
        assert "other:key" in ai_config._cache

    def test_default_prefix_clears_all_ai(self) -> None:
        from app.modules.ai.agents.safety import ai_config
        from app.modules.ai.agents.safety.ai_config import invalidate_ai_config_cache

        ai_config._cache["ai:foo"] = (1, 0.0)
        ai_config._cache["ai:bar"] = (2, 0.0)
        ai_config._cache["other:key"] = (3, 0.0)

        invalidate_ai_config_cache()  # 默认 prefix="ai:"

        assert "ai:foo" not in ai_config._cache
        assert "ai:bar" not in ai_config._cache
        assert "other:key" in ai_config._cache
