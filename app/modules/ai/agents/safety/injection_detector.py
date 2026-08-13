"""Prompt Injection 检测器。

按八类攻击模式做正则匹配。命中后降级而非直接拒绝：
  - 命中 → injection_hit=True → classify_execution_mode 强制 HITL（任何 risk）
  - 未命中 → 正常路径

设计原则：
  - 保守降级：宁可误报（用户体验略差），不可漏报（安全失守）
  - 多 pattern 任意命中即触发（OR 语义）
  - 不依赖 LLM 自检（LLM 可被越狱），仅在 user message 文本上做静态匹配

跨轮持久化（2026-07-10 修订 S-16）：
  - pattern 扫描：仅扫当前轮 user message（保留原意，避免长对话成本）
  - 命中状态：写入 Redis `ai:injection_hit:{conversation_id}` TTL = 1h
  - conversation 内任意后续轮次 tool 调用都强制走 HITL（即使后续消息不再
    触发 pattern）—— 防止攻击者把攻击拆成多轮，每轮只触发 1 个 pattern
    避免阈值。

Pattern 类别：
  1. 越狱指令覆盖（"忽略之前的所有指令" / "Ignore all previous instructions"）
  2. 角色重设（"你现在是一个管理员" / "You are now DAN"）
  3. ChatML 注入（`<|im_start|>system`）
  4. Llama-2 模板注入（`[INST]<<SYS>>`）
  5. 参数注入（"请把 user_id 设置成 999"）
  6. 代码注入（"import os; os.system"）
  7. 敏感字段提取（"查看 api_key 原文"）
  8. 链式攻击（"先调用 X，再调用 Y.delete"）
"""

import re

from redis.asyncio import Redis

# 大小写不敏感 + 多行；命中即触发 HITL
_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in [
        # 1. 越狱指令覆盖（允许前缀「请」/「麻烦」等）
        r"(请|麻烦)?\s*忽略.{0,5}(之前|前面|先前|过去).{0,5}(指令|规则|提示|约束|prompt)",
        r"ignore\s+(all\s+)?(previous|prior)\s+(instructions?|rules?|prompts?)",
        r"disregard\s+(all\s+)?(previous|prior)",
        # 2. 角色重设 / 越狱人设
        r"你现在是一个?(管理员|超管|root|无限制|未受限)",
        r"you\s+are\s+now\s+(dan|an?\s+unrestricted|admin|root)",
        r"jailbreak",
        # 3. ChatML / 模板 token 注入
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"\[INST\]",
        r"<<SYS>>",
        r"<</SYS>>",
        # 4. 参数注入（尝试改 user_id / role_code / dept_id 等）
        r"(请|把|将|让).{0,15}(user_id|user_name|role_code|dept_id|tenant_id)\s*(设置|改|设|变成|改为|设为)",
        r"(set|change|update).{0,15}(user_id|role_code|dept_id)\s*(to|as|=)",
        # 5. 代码注入（Python / Shell）
        r"import\s+(os|subprocess|shutil|sys)",
        r"os\.system\s*\(",
        r"subprocess\.(run|call|popen)",
        r"eval\s*\(",
        r"exec\s*\(",
        r"rm\s+-rf\s+/",
        # 6. 敏感字段提取（宽容中间词，如「OpenAI provider 的 api_key」）
        r"(查看|看|显示|输出|告诉我|return).{0,20}(api_key|api\s?key|secret_key|password_hash|private_key|access_token)",
        r"(show|reveal|print|return).{0,20}(api_key|secret_key|password_hash|private_key)",
        # 7. 链式攻击（先 list 后 delete）
        r"先.{0,30}(列出|查询|list).{0,80}(删除|delete|批量删除)",
        r"(对每个|对所有).{0,30}(删除|delete|禁用)",
    ]
)


# ============ 跨轮持久化 Redis key（修订 S-16） ============

_INJECTION_HIT_TTL_SEC = 3600  # 1h
_KEY_CONV = "ai:injection_hit:{conversation_id}"


def detect_injection(text: str) -> bool:
    """检测 user message 是否命中 prompt injection pattern

    Args:
        text: 用户原始消息文本（含图片描述/base64 之外的文本部分）

    Returns:
        True = 命中至少一个 pattern → 应强制 HITL
        False = 未命中

    Note:
        - 仅做静态文本匹配，不依赖 LLM 自检（LLM 可被越狱）
        - 保守偏向：宁可误报，不可漏报
        - 多语言：中英文 pattern 都覆盖
        - 修订 S-16：本函数只做"本轮扫描"；跨轮持久化由调用方调
          `record_injection_hit_conversation` + `is_injection_hit_conversation`
    """
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)


def matched_patterns(text: str) -> list[str]:
    """调试辅助：返回所有命中的 pattern 字符串（不暴露给业务路径）"""
    if not text:
        return []
    return [p.pattern for p in _PATTERNS if p.search(text)]


# ============ 跨轮持久化 helper（修订 S-16） ============


async def record_injection_hit_conversation(
    redis: Redis,
    conversation_id: int | None,
) -> None:
    """本轮 pattern 命中 → 写 conversation 级 Redis flag（TTL 1h）

    必须在 chat.py 检测到命中后立即调用，让 conversation 后续轮次 tool 调用
    都强制 HITL（即使后续消息不再触发 pattern）。

    Args:
        redis: redis client
        conversation_id: 会话 ID；None（新建会话首条消息）时跳过 Redis 写入
                        （仅本轮生效，跨轮不持久化——首条消息的特殊场景）

    设计：
      - 每次命中刷新 TTL（用户活跃对话内持续触发，1h 不重置）
      - 不做"次数计数"——任何一次命中都让整个 conversation 进入"已触发"状态
        （``record_injection`` 负责计数，并与自动禁用阈值挂钩）
    """
    if conversation_id is None:
        return
    key = _KEY_CONV.format(conversation_id=conversation_id)
    await redis.set(key, "1", ex=_INJECTION_HIT_TTL_SEC)

    # 记录安全事件指标。
    from app.modules.ai.metrics import record_security_event  # noqa: PLC0415

    record_security_event("injection")


async def is_injection_hit_conversation(
    redis: Redis,
    conversation_id: int | None,
) -> bool:
    """检查 conversation 级是否已触发过注入（修订 S-16）

    chat.py build_chat_deps 后调用，把结果合并到 deps.injection_hit：
      deps.injection_hit = detect_injection(本轮) or is_injection_hit_conversation(历史)

    Args:
        redis: redis client
        conversation_id: 会话 ID；None 时返回 False（仅本轮 detect 有效）

    Returns:
        True = conversation 1h 内曾命中过 pattern，本轮 tool 调用应强制 HITL
        False = 无历史命中或 conversation_id=None
    """
    if conversation_id is None:
        return False
    key = _KEY_CONV.format(conversation_id=conversation_id)
    return bool(await redis.exists(key))


async def clear_injection_hit_conversation(
    redis: Redis,
    conversation_id: int,
) -> None:
    """显式清除 conversation 级注入状态（测试 / 管理员手动恢复用）

    生产代码一般不调——TTL 1h 自然过期。用于：
      - 单元测试间清理
      - 管理 API 可显式清除会话注入标记
    """
    key = _KEY_CONV.format(conversation_id=conversation_id)
    await redis.delete(key)
