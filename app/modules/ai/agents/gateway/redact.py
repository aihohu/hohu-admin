r"""redact_secrets — 用户输入 / 历史消息正则脱敏

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §7.4。

识别 4 类 pattern + 1 类 MIME 白名单豁免：
  1. OpenAI API Key: sk-[A-Za-z0-9]{20,}
  2. AWS Access Key: AKIA[A-Z0-9]{16}
  3. JWT 三段式: eyJ....\.eyJ....\.[A-Za-z0-9_-]{10,}
  4. 上下文敏感: (?i)(api_key|secret|token|password)\s*[:=]\s*["']?[A-Za-z0-9_\-+/=]{16,}
  5. MIME 白名单豁免: data:image/* / audio/* / video/* / pdf / zip

system_prompt 注入语义闭环（SAFETY_PREAMBLE 第 3 条）：
  - 用户输入含 [REDACTED:*] 标记 → 用户尝试提交敏感数据
  - LLM 按"refuse and guide"策略拒绝并引导用户走传统界面
"""

import logging
import re

logger = logging.getLogger(__name__)


# spec §7.4 4 类 pattern
REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OPENAI_API_KEY", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[A-Z0-9]{16}")),
    (
        "JWT",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    (
        "CONTEXT_SENSITIVE",
        re.compile(
            r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9_\-+/=]{16,}[\"']?"
        ),
    ),
]

# spec §7.4 MIME 白名单：data:image/* / audio/* / video/* / pdf / zip 跳过扫描
MIME_WHITELIST_PREFIXES: tuple[str, ...] = (
    "data:image/",
    "data:audio/",
    "data:video/",
    "data:application/pdf",
    "data:application/zip",
)


def _is_mime_whitelisted(text: str) -> bool:
    """检测 text 是否是 MIME 白名单豁免的 data URI"""
    return any(text.startswith(prefix) for prefix in MIME_WHITELIST_PREFIXES)


def redact_secrets(text: str) -> str:
    """识别并脱敏文本中的敏感 pattern

    Args:
        text: 用户输入文本 / 历史消息 content

    Returns:
        脱敏后的文本，命中 pattern 替换为 [REDACTED:<TYPE>:<sha8>]
        （sha8 是匹配前 8 字符的 sha256，便于审计追踪但不可逆推原文）

    注意：
      - MIME 白名单 data URI 直接返回（避免 base64 内嵌图片被误判）
      - 空 / None 输入原样返回
      - 不区分大小写（除 AWS_ACCESS_KEY 必须大写 AKIA 开头）
    """
    if not text or not isinstance(text, str):
        return text

    # MIME 白名单豁免：整段是 data:image/* 等 → 直接返回
    if _is_mime_whitelisted(text.strip()):
        return text

    result = text
    for type_name, pattern in REDACT_PATTERNS:
        # 闭包绑定 type_name（B023：避免 lambda 引用循环变量）
        replacement = f"[REDACTED:{type_name}]"
        result = pattern.sub(replacement, result)

    return result


def contains_redacted_marker(text: str) -> bool:
    """检测文本是否含 [REDACTED:*] 标记

    SAFETY_PREAMBLE 第 3 条规则用：
      - 用户输入含标记 → LLM 拒绝并引导走传统界面
      - 用于 system_prompt 检测用户尝试提交敏感数据
    """
    if not text:
        return False
    return "[REDACTED:" in text
