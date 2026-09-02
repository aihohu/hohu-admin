"""Forbidden URLs Guardrail。

管理员配置 `system_config.ai:guardrail:forbidden_urls`（JSON 字符串数组，域名级别），
用户输入命中后**整条消息拦截**（不进 LLM，emit AiErrorEvent 短路）。

与 keyword_blocklist / forbidden_topics 区别：
  - 黑名单是**域名**（注册级，不含 path），如 `["competitor.com", "malicious.org"]`
  - 用户输入中可能含完整 URL（`https://www.competitor.com/article/123`），
    需 regex 提取域名后比对（精确 + 后缀匹配）

匹配规则：
  1. regex 提取用户输入中所有 URL（三种形态）：
     - `https?://域名/...`
     - `www.域名/...`
     - 裸域名（`example.com` / `sub.example.com`）
  2. 对每个提取出的域名，检查是否命中黑名单：
     - 精确匹配：`evil.com` == `evil.com`
     - 后缀匹配：`evil.com` 命中 `sub.evil.com`（黑名单是注册级，子域也算）
     - 但 `evil.com.txt`（合法 .txt 域名）不命中（后缀匹配必须是域边界）

设计（与 keyword_blocklist 同模式）：
  - 进程内缓存 60s
  - 多语言支持：URL 本身是 ASCII，但周围文本可含中文
"""

import json
import logging
import re
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext
from app.modules.system.service.config_service import config_service

logger = logging.getLogger(__name__)

CONFIG_KEY = "ai:guardrail:forbidden_urls"
_CACHE_TTL_SEC = 60

# 进程内缓存（模块级单例）
_cache: dict[int, tuple[list[str], float]] = {}
_cache_generation = 0

# URL 提取 regex：
# - group 1: https?:// 后的域名（含 port 可选）
# - group 2: www. 开头的域名
# - group 3: 裸域名（a-z0-9-.+ 开头，至少一个 dot，TLD 2-10 字符）
# 用 MULTILINE / ASCII-only（URL 标准）
_URL_PATTERN = re.compile(
    r"(?:https?://([a-zA-Z0-9\-._:]+))"
    r"|(?:www\.([a-zA-Z0-9\-._]+))"
    r"|((?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,10})",
    re.MULTILINE,
)


async def load_forbidden_urls(
    db: AsyncSession, *, tenant: TenantContext, force_refresh: bool = False
) -> list[str]:
    """从 sys_config 读 forbidden_urls（缓存 60s）

    Returns:
        域名黑名单列表（小写，去 path / 协议），空 list 表示无限制
    """
    cached = _cache.get(tenant.tenant_id)
    if not force_refresh and cached is not None:
        value, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL_SEC:
            return value

    generation = _cache_generation
    raw = await config_service.get_value(db, CONFIG_KEY, tenant=tenant)
    parsed: list[str] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for x in data:
                    if not isinstance(x, str) or not x.strip():
                        continue
                    # 规范化：去协议 / path / port，仅保留域名
                    cleaned = _normalize_domain(x.strip())
                    if cleaned:
                        parsed.append(cleaned.lower())
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "invalid ai:guardrail:forbidden_urls json, ignoring",
                extra={"error": str(e)},
            )

    if generation == _cache_generation:
        _cache[tenant.tenant_id] = (parsed, time.time())
    return parsed


def invalidate_forbidden_urls_cache() -> None:
    """显式清缓存（ConfigService.update 改 ai:guardrail:* 后调）"""
    global _cache_generation

    _cache_generation += 1
    _cache.clear()


def _normalize_domain(raw: str) -> str:
    """从用户配置项中提取注册级域名（去 protocol / path / port / query）"""
    # 去 protocol
    cleaned = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    # 去 path / query / fragment（保留域名 + 可选 port）
    cleaned = re.split(r"[/?#]", cleaned, maxsplit=1)[0]
    # 去 port
    cleaned = cleaned.split(":")[0]
    # 去 userinfo（user:pass@）
    if "@" in cleaned:
        cleaned = cleaned.split("@")[-1]
    return cleaned.strip().lower()


def _extract_domains(text: str) -> list[str]:
    """从用户输入文本中提取所有 URL 的域名段（小写）"""
    if not text:
        return []
    domains: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        # 三种 group 任一非空
        for group in match.groups():
            if group:
                # 去 port（仅留域名）
                domain = group.split(":")[0].lower()
                if domain:
                    domains.append(domain)
                break
    return domains


def _matches_blocklist(domain: str, blocklist: list[str]) -> str | None:
    """检查单个域名是否命中黑名单（精确或后缀匹配）

    后缀匹配要求"域边界"——`evil.com` 命中 `sub.evil.com`，但不命中 `evil.com.txt`。

    Returns:
        命中的黑名单域名（第一个匹配项），未命中返回 None
    """
    for blocked in blocklist:
        if domain == blocked:
            return blocked
        # 后缀匹配：domain 必须以 ".blocked" 结尾（域边界）
        if domain.endswith("." + blocked):
            return blocked
    return None


def check_forbidden_urls(text: str, blocklist: list[str]) -> list[str]:
    """检查文本中是否含 forbidden_urls 命中的域名

    Args:
        text: 用户输入文本
        blocklist: 已加载的 forbidden_urls（小写，已规范化）

    Returns:
        命中的（domain, blocked_pattern）元组列表，空表示 OK
    """
    if not text or not blocklist:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for domain in _extract_domains(text):
        matched = _matches_blocklist(domain, blocklist)
        if matched and matched not in seen:
            hits.append(matched)
            seen.add(matched)
    return hits
