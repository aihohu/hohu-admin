"""Permission detail hash 规范化与计算。

JSONB 序列化顺序不确定（PG 内部可能重排键），直接 hash 不可靠。
本模块用规范化序列化（sort_keys + 紧凑分隔符 + 不转义中文）+ SHA-256。
同时返回规范化 JSON 字符串本身，便于未来 Hash 算法迁移时回填。
"""

import hashlib
import json


def compute_detail_hash(detail: dict) -> tuple[str, str]:
    """规范化 detail 字典并计算 SHA-256 哈希。

    Args:
        detail: 权限详情字典（如 {"path": "/api/users", "method": "GET"}）

    Returns:
        (hash_hex, canonical_json)：
        - hash_hex：64 字符小写 SHA-256
        - canonical_json：规范化 JSON 字符串（审计字段，存 detail_canonical）

    特征:
        - 键按字典序排序（嵌套字典递归排序）
        - 分隔符紧凑（无空格）
        - 中文不转义（ensure_ascii=False）
        - list 顺序保留（不排序）
    """
    canonical = json.dumps(
        detail,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    hash_hex = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hash_hex, canonical
