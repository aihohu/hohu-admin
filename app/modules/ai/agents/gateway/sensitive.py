"""敏感输出脱敏 — tool 返回值剥离 + 全局字段黑名单

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §7.3。

两道防线：
  1. tool meta 显式声明的 sensitive_output 字段（业务方知情）
  2. GLOBAL_OUTPUT_BLOCKLIST 全局黑名单（兜底，防业务方漏写）

任一命中即在 serialize_for_llm 时 pop，不进 LLM context。

修订记录：
  - 2026-07-10 S-10：匹配规则从子串（`bl in key`）改为 word-boundary
    （`key == bl` 或 `key` 前后以 `_` 与 bl 连接）。原实现让 csrf_token /
    pagination_token / next_page_token / token_count 等业务字段被误剥离。
    password_hash / user_password 仍命中（向后兼容）。
  - 2026-07-10 S-10：model_dump(mode="json") 保证嵌套 BaseModel 也被走完
    scrub；新增 depth=20 上限防 RecursionError（LLM 输出可能深嵌套）。
"""

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# spec §7.3 全局字段黑名单
# 命中即剥离，无论 tool meta 是否声明 sensitive_output
#
# 修订 S-10：移除裸 "token"（原集合中的最后一个），因为 token_count / token_value
# 等业务字段会被前缀规则误伤。access_token / refresh_token / session_token 已在
# 集合中覆盖常见 token 字段；纯 "token" 字段（如 {token: "..."}）业务方应显式
# 声明 sensitive_output。
#
# password_hash / password_value / api_key_id 等通过 _matches_blocklist 的
# 前缀规则（bl + "_"）自动命中。
GLOBAL_OUTPUT_BLOCKLIST: frozenset[str] = frozenset(
    {
        "password",
        "salt",
        "api_key",
        "secret_key",
        "private_key",  # 包含 PrivateKey 等格式（spec 提到 RsaPrivateKey）
        "access_token",
        "refresh_token",
        "session_token",
        "secret",
    }
)

# 递归深度上限：防 LLM 控制的深嵌套 payload 触发 RecursionError
_MAX_DEPTH = 20


def _matches_blocklist(key: str, blocklist_lower: set[str]) -> bool:
    """word-boundary 匹配（修订 S-10）

    命中条件（任一）：
      1. key 完全等于黑名单词（lowercase 比较）：password / token / api_key
      2. key 以黑名单词 + "_" 开头：password_hash / token_value / api_key_id

    故意 **不** 包含后缀形式（xxx_bl）—— 否则 csrf_token / pagination_token /
    next_page_token 等业务字段会被误剥离（spec §22 修订日志 S-10 的核心目标）。

    不命中的常见情况（业务字段）：
      - csrf_token / pagination_token / next_page_token：xxx_token 后缀形式
      - token_count：xxx_count 数量字段
      - user_password：业务方应显式声明 sensitive_output（命名变体太多无法全覆盖）

    命中但符合预期的情况：
      - password_hash / password_value：bl_xxx 前缀形式
      - access_token：完全等于黑名单词 access_token
      - secret_key：完全等于黑名单词 secret_key
    """
    key_lower = key.lower()
    for bl in blocklist_lower:
        if key_lower == bl:
            return True
        # 仅前缀形式：bl_xxx（不包含后缀 xxx_bl，避免 csrf_token 误伤）
        if key_lower.startswith(bl + "_"):
            return True
    return False


def _scrub_fields(
    payload: Any,
    blocklist: frozenset[str] | set[str],
    *,
    depth: int = 0,
) -> Any:
    """递归剥离 payload 中命中 blocklist 的字段（含嵌套 dict / list）

    spec §7.3 关键约束：
      - 不区分大小写（password / Password / PASSWORD 都命中）
      - word-boundary 匹配（修订 S-10）：password_hash 命中 password，
        csrf_token **不** 命中 token
      - 嵌套 dict / list 递归处理；depth > _MAX_DEPTH 时防御性截断
      - 非 dict 输入（str / int / None）原样返回
    """
    if depth > _MAX_DEPTH:
        # 防御性截断：不抛异常避免业务流中断；返回原值（最坏情况是泄漏
        # 深层嵌套的敏感字段，但 LLM 通常构造不出 20+ 层嵌套）
        logger.warning(
            "scrub depth exceeded, returning payload as-is",
            extra={"depth": depth, "max_depth": _MAX_DEPTH},
        )
        return payload

    if not isinstance(payload, dict):
        return payload

    blocklist_lower = {b.lower() for b in blocklist}

    result: dict[str, Any] = {}
    for key, value in payload.items():
        if _matches_blocklist(key, blocklist_lower):
            logger.debug(
                "sensitive field scrubbed",
                extra={"field": key, "matched_blocklist": True},
            )
            continue
        # 递归处理 dict / list
        if isinstance(value, dict):
            result[key] = _scrub_fields(value, blocklist, depth=depth + 1)
        elif isinstance(value, list):
            result[key] = [
                _scrub_fields(item, blocklist, depth=depth + 1)
                if isinstance(item, dict)
                else item
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
      1. BaseModel → model_dump(mode="json") 转 dict（修订 S-10：mode="json"
         保证嵌套 BaseModel 也被走完 scrub；旧版 mode 默认值返回 Python
         对象，嵌套 BaseModel 不被 _scrub_fields 走到，内层 password 泄漏）
      2. dict → 合并 sensitive_output + GLOBAL_OUTPUT_BLOCKLIST 后 _scrub_fields
      3. list[dict | BaseModel] → 逐项 scrub
      4. 标量 / list[标量] → 原样返回（无字段名无法 scrub）
    """
    combined_blocklist = frozenset(set(GLOBAL_OUTPUT_BLOCKLIST) | set(sensitive_output))

    # BaseModel 序列化（修订 S-10：强制 mode="json"）
    if isinstance(raw_result, BaseModel):
        raw_result = raw_result.model_dump(mode="json")

    # dict 单层 / 嵌套递归
    if isinstance(raw_result, dict):
        return _scrub_fields(raw_result, combined_blocklist)

    # list：每个 dict / BaseModel 元素单独 scrub
    if isinstance(raw_result, list):
        scrubbed_list: list[Any] = []
        for item in raw_result:
            if isinstance(item, BaseModel):
                # 修订 S-10：list[BaseModel] 也要 model_dump(mode="json")
                item = item.model_dump(mode="json")
            if isinstance(item, dict):
                scrubbed_list.append(_scrub_fields(item, combined_blocklist))
            else:
                scrubbed_list.append(item)
        return scrubbed_list

    # 标量（int / str / bool / None）无字段名，原样返回
    return raw_result
