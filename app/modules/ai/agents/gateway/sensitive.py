"""敏感输出脱敏 — tool 返回值剥离 + 全局字段黑名单

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §7.3。

两道防线：
  1. tool meta 显式声明的 sensitive_output 字段（业务方知情）
  2. GLOBAL_OUTPUT_BLOCKLIST 全局黑名单（兜底，防业务方漏写）

任一命中即在 serialize_for_llm 时 pop，不进 LLM context。
"""

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# spec §7.3 全局字段黑名单
# 命中即剥离，无论 tool meta 是否声明 sensitive_output
GLOBAL_OUTPUT_BLOCKLIST: frozenset[str] = frozenset(
    {
        "password",
        "password_hash",
        "salt",
        "api_key",
        "secret_key",
        "private_key",  # 包含 PrivateKey 等格式（spec 提到 RsaPrivateKey）
        "access_token",
        "refresh_token",
        "session_token",
        "secret",
        "token",
    }
)


def _scrub_fields(
    payload: dict[str, Any],
    blocklist: frozenset[str] | set[str],
) -> dict[str, Any]:
    """递归剥离 payload 中命中 blocklist 的字段（含嵌套 dict / list）

    spec §7.3 关键约束：
      - 不区分大小写（password / Password / PASSWORD 都命中）
      - 部分匹配（password_hash 也命中 password）—— 因为 _scrub_fields 用 substring
      - 嵌套 dict / list 递归处理
      - 非 dict 输入（str / int / None）原样返回
    """
    if not isinstance(payload, dict):
        return payload

    # 标准 blocklist 为小写，匹配时把字段名转小写做 substring 检查
    blocklist_lower = {b.lower() for b in blocklist}

    result: dict[str, Any] = {}
    for key, value in payload.items():
        key_lower = key.lower()
        # 字段名命中（含子串匹配，如 password_hash 命中 password）
        if any(bl in key_lower for bl in blocklist_lower):
            logger.debug(
                "sensitive field scrubbed",
                extra={"field": key, "matched_blocklist": True},
            )
            continue
        # 递归处理 dict / list
        if isinstance(value, dict):
            result[key] = _scrub_fields(value, blocklist)
        elif isinstance(value, list):
            result[key] = [
                _scrub_fields(item, blocklist) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value

    return result


def serialize_for_llm(
    sensitive_output: tuple[str, ...],
    raw_result: Any,
) -> Any:
    """把 tool 返回值序列化为 LLM 安全的格式（spec §7.3）

    Args:
        sensitive_output: tool meta 声明的 sensitive_output 字段（业务方知情）
        raw_result: 业务函数原始返回值（dict / BaseModel / list / 标量）

    Returns:
        脱敏后的数据（dict / list / 标量），可直接给 LLM 看

    工作流：
      1. BaseModel → model_dump() 转 dict
      2. dict → 合并 sensitive_output + GLOBAL_OUTPUT_BLOCKLIST 后 _scrub_fields
      3. list[dict] → 逐项 scrub
      4. 标量 / list[标量] → 原样返回（无字段名无法 scrub）
    """
    # 合并业务方声明 + 全局黑名单
    combined_blocklist = frozenset(set(GLOBAL_OUTPUT_BLOCKLIST) | set(sensitive_output))

    # BaseModel 序列化
    if isinstance(raw_result, BaseModel):
        raw_result = raw_result.model_dump()

    # dict 单层 / 嵌套递归
    if isinstance(raw_result, dict):
        return _scrub_fields(raw_result, combined_blocklist)

    # list：每个 dict 元素单独 scrub
    if isinstance(raw_result, list):
        return [
            _scrub_fields(item, combined_blocklist) if isinstance(item, dict) else item
            for item in raw_result
        ]

    # 标量（int / str / bool / None）无字段名，原样返回
    return raw_result
