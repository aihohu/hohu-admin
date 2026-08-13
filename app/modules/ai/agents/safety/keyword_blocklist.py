"""Keyword Blocklist Guardrail。

管理员配置 `system_config.ai:guardrail:keyword_blocklist`（JSON 字符串数组），
用户输入命中后**整条消息拦截**（不进 LLM，emit AiErrorEvent 短路）。

用途：项目自定义敏感词（公司机密 / 商标 / 内部代号 / 政治敏感词等），
它比 injection_detector 更宽松；命中内容不一定是攻击，也可能只是合规限制。

设计：
  - 进程内缓存 60s（避免每次 chat 都查 DB），通过 `invalidate_blocklist_cache`
    管理员改配置后立即生效（ConfigService.update 时调）
  - 命中规则：大小写不敏感的子串匹配
  - 多语言支持：blocklist 字符串本身含中文 / 英文都行

未含（留 v2+）：
  - LLM 输出检测（流式拦截，需在 produce_pydantic 阶段过滤 text-delta）
  - regex pattern 支持（MVP 仅子串匹配）
"""

import json
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.service.config_service import config_service

logger = logging.getLogger(__name__)

CONFIG_KEY = "ai:guardrail:keyword_blocklist"
_CACHE_TTL_SEC = 60

# 进程内缓存（模块级单例）
_cached_blocklist: list[str] | None = None
_cached_at: float = 0.0


async def load_blocklist(db: AsyncSession, *, force_refresh: bool = False) -> list[str]:
    """从 sys_config 读 blocklist（缓存 60s）

    Args:
        db: 用于查 sys_config 的 session
        force_refresh: True 跳过缓存（管理员改配置后调）

    Returns:
        blocklist 字符串列表（空 list 表示无拦截词；查 sys_config 失败也返回空）
    """
    global _cached_blocklist, _cached_at

    if not force_refresh and _cached_blocklist is not None:
        if time.time() - _cached_at < _CACHE_TTL_SEC:
            return _cached_blocklist

    raw = await config_service.get_value(db, CONFIG_KEY)
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
                "invalid ai:guardrail:keyword_blocklist json, ignoring",
                extra={"error": str(e)},
            )

    _cached_blocklist = parsed
    _cached_at = time.time()
    return parsed


def invalidate_blocklist_cache() -> None:
    """显式清缓存（ConfigService.update 改 ai:guardrail:* 后调）"""
    global _cached_blocklist, _cached_at
    _cached_blocklist = None
    _cached_at = 0.0


def check_keywords(text: str, blocklist: list[str]) -> list[str]:
    """检查文本命中哪些 blocklist 词（大小写不敏感子串匹配）

    Args:
        text: 用户输入或 AI 输出文本
        blocklist: 已加载的 blocklist（小写）

    Returns:
        命中的 keyword 列表（空表示 OK）
    """
    if not text or not blocklist:
        return []
    lowered = text.lower()
    return [kw for kw in blocklist if kw in lowered]
