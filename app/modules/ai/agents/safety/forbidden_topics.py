"""Forbidden Topics Guardrail。

管理员配置 `system_config.ai:guardrail:forbidden_topics`（JSON 字符串数组），
用户输入命中后**整条消息拦截**（不进 LLM，emit AiErrorEvent 短路）。

与 keyword_blocklist 区别：
  - keyword_blocklist: 精确敏感词（公司代号 / 商标 / 内部黑话）
  - forbidden_topics: 宽泛主题词（政治 / 宗教 / 竞品对比 / 投资建议）

实现上两者都是大小写不敏感子串匹配，但语义不同 + 错误码不同（用户文案区分）。

设计（与 keyword_blocklist 同模式）：
  - 进程内缓存 60s（避免每次 chat 都查 DB），通过 `invalidate_forbidden_topics_cache`
    管理员改配置后立即生效（ConfigService.update 时调）
  - 命中规则：大小写不敏感子串匹配
  - 多语言支持：topics 字符串本身含中文 / 英文都行
"""

import json
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext
from app.modules.system.service.config_service import config_service

logger = logging.getLogger(__name__)

CONFIG_KEY = "ai:guardrail:forbidden_topics"
_CACHE_TTL_SEC = 60

# 进程内缓存（模块级单例）
_cache: dict[int, tuple[list[str], float]] = {}


async def load_forbidden_topics(
    db: AsyncSession, *, tenant: TenantContext, force_refresh: bool = False
) -> list[str]:
    """从 sys_config 读 forbidden_topics（缓存 60s）

    Args:
        db: 用于查 sys_config 的 session
        force_refresh: True 跳过缓存（管理员改配置后调）

    Returns:
        topics 字符串列表（空 list 表示无禁话题；查 sys_config 失败也返回空）
    """
    cached = _cache.get(tenant.tenant_id)
    if not force_refresh and cached is not None:
        value, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL_SEC:
            return value

    raw = await config_service.get_value(db, CONFIG_KEY, tenant=tenant)
    parsed: list[str] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                parsed = [
                    str(x).lower() for x in data if isinstance(x, str) and x.strip()
                ]
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "invalid ai:guardrail:forbidden_topics json, ignoring",
                extra={"error": str(e)},
            )

    _cache[tenant.tenant_id] = (parsed, time.time())
    return parsed


def invalidate_forbidden_topics_cache() -> None:
    """显式清缓存（ConfigService.update 改 ai:guardrail:* 后调）"""
    _cache.clear()


def check_topics(text: str, topics: list[str]) -> list[str]:
    """检查文本命中哪些 forbidden_topics（大小写不敏感子串匹配）

    Args:
        text: 用户输入文本
        topics: 已加载的 topics（小写）

    Returns:
        命中的 topic 列表（空表示 OK）
    """
    if not text or not topics:
        return []
    lowered = text.lower()
    return [topic for topic in topics if topic in lowered]
