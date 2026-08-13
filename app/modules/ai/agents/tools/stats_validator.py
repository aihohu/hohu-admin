"""聚合 tool 白名单校验 helper

聚合字段必须由业务工具显式声明白名单：
  allowed_filters / allowed_group_by 在 @ai_tool 装饰器层校验，业务函数内
  不重复检查；越界字段直接抛 AI_STATS_FIELD_NOT_ALLOWED。

装饰器执行期拿不到运行时参数，因此业务函数在入口显式调用本 helper；
包装层也可以统一处理，但业务层保留兜底校验。
"""

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.meta import AiToolMeta


def validate_filters_in_whitelist(meta: AiToolMeta, filters: dict | None) -> dict:
    """校验 filters dict 的 key 是否在 meta.allowed_filters 白名单内

    - filters=None → 返回 {}（方便业务函数统一处理）
    - filters={} → 返回 {}（用户显式传空，合法）
    - filters 含越界字段 → 抛 BusinessRuleException(AI_STATS_FIELD_NOT_ALLOWED)

    业务函数用法：
        @ai_tool(AiToolMeta(..., allowed_filters=("status", "user_gender")))
        async def user_count(ctx, filters: dict | None = None):
            filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
            ...
    """
    if filters is None:
        return {}

    if not meta.allowed_filters:
        # 白名单为空但用户传了 filters → 全部越界
        if filters:
            raise BusinessRuleException(
                f"该 tool 不支持任何 filter 字段，拒绝 keys={sorted(filters.keys())}",
                error_code="AI_STATS_FIELD_NOT_ALLOWED",
            )
        return {}

    disallowed = set(filters.keys()) - set(meta.allowed_filters)
    if disallowed:
        raise BusinessRuleException(
            f"Filter 字段不在聚合白名单: {sorted(disallowed)}。"
            f"允许的字段: {list(meta.allowed_filters)}",
            error_code="AI_STATS_FIELD_NOT_ALLOWED",
        )
    return filters


def validate_group_by_in_whitelist(meta: AiToolMeta, group_by: str | None) -> str:
    """校验 group_by 字段是否在 meta.allowed_group_by 白名单内

    - group_by=None → 返回 meta.allowed_group_by[0]（默认第一个，业务函数友好）
    - group_by 在白名单 → 原样返回
    - 越界 → 抛 BusinessRuleException(AI_STATS_FIELD_NOT_ALLOWED)

    返回值用法：
        group_by = validate_group_by_in_whitelist(ctx.tool_meta, group_by)
        col = getattr(User, group_by)
    """
    if not meta.allowed_group_by:
        raise BusinessRuleException(
            "该 tool 不支持 group_by 操作",
            error_code="AI_STATS_FIELD_NOT_ALLOWED",
        )

    if group_by is None:
        return meta.allowed_group_by[0]

    if group_by not in meta.allowed_group_by:
        raise BusinessRuleException(
            f"group_by 字段 {group_by!r} 不在白名单。"
            f"允许的字段: {list(meta.allowed_group_by)}",
            error_code="AI_STATS_FIELD_NOT_ALLOWED",
        )
    return group_by


def validate_field_in_whitelist(
    meta: AiToolMeta, field: str, *, whitelist_attr: str = "allowed_group_by"
) -> str:
    """校验 distinct 等 tool 的 field 参数是否在白名单内

    distinct tool 复用 allowed_group_by 作 field 白名单（语义一致：可枚举的低基数字段）。
    """
    whitelist: tuple[str, ...] = getattr(meta, whitelist_attr)
    if not whitelist:
        raise BusinessRuleException(
            "该 tool 不支持此操作",
            error_code="AI_STATS_FIELD_NOT_ALLOWED",
        )
    if field not in whitelist:
        raise BusinessRuleException(
            f"field {field!r} 不在白名单。允许的字段: {list(whitelist)}",
            error_code="AI_STATS_FIELD_NOT_ALLOWED",
        )
    return field
