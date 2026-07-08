"""Prompt Injection 检测器 — spec §11.1 / §12.3

按 §12.3 的 8 类攻击模式做正则匹配。命中后**降级而非拒绝**：
  - 命中 → injection_hit=True → classify_execution_mode 强制 HITL（任何 risk）
  - 未命中 → 正常路径

设计原则（spec §11.1）：
  - 保守降级：宁可误报（用户体验略差），不可漏报（安全失守）
  - 多 pattern 任意命中即触发（OR 语义）
  - 不依赖 LLM 自检（LLM 可被越狱），仅在 user message 文本上做静态匹配

Pattern 来源（§12.3 INJECTION_ATTACKS）：
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
    """
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)


def matched_patterns(text: str) -> list[str]:
    """调试辅助：返回所有命中的 pattern 字符串（不暴露给业务路径）"""
    if not text:
        return []
    return [p.pattern for p in _PATTERNS if p.search(text)]
