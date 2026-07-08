"""风险分级判定 — spec §5.3 矩阵

输入 AiToolMeta + dry_run_count + injection_hit 标记，
输出执行模式（autonomous / HITL）。

调用方（Phase 3.2 Gateway Executor 接入）：
    dry_run_count = None
    if registered.dry_run_fn is not None:
        dr = await registered.dry_run_fn(ctx, **args)
        dry_run_count = dr.count if dr.ok else None

    mode = classify_execution_mode(meta, dry_run_count=dry_run_count, injection_hit=False)
    if mode == AiExecutionMode.HITL:
        # 走 HITL Manager 挂起 + confirmation_required SSE 事件
        ...
    else:
        # 直接执行
        ...

判定矩阵（spec §5.3）：
| risk        | dry_run_count | hitl_always | injection_hit | mode      |
|-------------|---------------|-------------|---------------|-----------|
| low         | -             | False       | False         | autonomous|
| low         | -             | True        | -             | hitl      |
| high        | ≤ 1           | False       | False         | autonomous|
| high        | > 1 / None    | False       | False         | hitl      |
| destructive | n/a           | False       | False         | hitl      |
| any         | n/a           | True        | -             | hitl      |
| any         | n/a           | -           | True          | hitl      |

注意：
  - dry_run_count=None 表示"未跑 dry_run 或 dry_run 失败"，对 high 风险保守降级到 HITL
  - injection_hit=True 永远走 HITL（spec §11.1 注入命中降级而非拒绝）
"""

from app.modules.ai.agents.tools.meta import AiToolMeta

from .constants import AiExecutionMode


def classify_execution_mode(
    meta: AiToolMeta,
    *,
    dry_run_count: int | None = None,
    injection_hit: bool = False,
) -> AiExecutionMode:
    """按 §5.3 矩阵判定执行模式

    Args:
        meta: Tool 元数据（risk / hitl_always）
        dry_run_count: dry_run 函数返回的影响行数；
            None 表示未跑 dry_run 或 dry_run 失败（保守按 HITL 处理）
            0 / 正整数 表示 dry_run 成功的影响行数
        injection_hit: 是否命中 prompt injection pattern（spec §11.1）

    Returns:
        AiExecutionMode.AUTONOMOUS 或 AiExecutionMode.HITL
    """
    # 优先级 1: hitl_always 强制 HITL（无视 risk + dry_run）
    if meta.hitl_always:
        return AiExecutionMode.HITL

    # 优先级 2: prompt injection 命中（spec §11.1 强制 HITL）
    if injection_hit:
        return AiExecutionMode.HITL

    # 优先级 3: 按 risk 分级
    if meta.risk == "destructive":
        # 破坏性操作永远 HITL（§5.3 矩阵 destructive 行）
        return AiExecutionMode.HITL

    if meta.risk == "high":
        # high + dry_run_count > 1 → HITL；count ≤ 1 → autonomous
        # count=None（未跑 dry_run 或失败）保守降级到 HITL
        if dry_run_count is None:
            return AiExecutionMode.HITL
        if dry_run_count > 1:
            return AiExecutionMode.HITL
        return AiExecutionMode.AUTONOMOUS

    # risk == "low"：autonomous
    return AiExecutionMode.AUTONOMOUS
