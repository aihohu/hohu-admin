"""job 模块的 AI Tool — spec §11.3

job.update_cron: 改调度 cron 表达式（白名单字段，经 JobAiUpdate schema 强制过滤）
  - risk=high（改调度 = 改执行行为）
  - hitl_always=True（调度变更必须 HITL）
  - dry_run_supported=True（dry_run 显示当前 cron vs 拟变更）

不能 AI 操作的字段（spec §11.3 表格禁止）：job_key / run_on_enable / timeout /
max_retries / concurrent — JobAiUpdate schema 在 update_for_ai 入口兜底丢弃。
"""

# ruff: noqa: PLC0415  inline import 避免循环

from typing import Any

from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import AiToolContext
from app.modules.job.schemas.job import JobAiUpdate
from app.modules.job.service.job_service import job_service


@ai_tool(
    AiToolMeta(
        name="job.update_cron",
        agent="job_mgmt",
        summary="Update job cron expression → {'ok': true}. HITL required.",
        required_perms=("system:job:edit",),
        risk="high",
        hitl_always=True,
        dry_run_supported=True,
    )
)
async def job_update_cron(
    ctx: AiToolContext, *, job_id: int, cron_expression: str
) -> dict[str, Any]:
    """更新任务 cron 表达式（白名单字段，spec §11.3）

    Args:
        ctx: AiToolContext
        job_id: 任务 ID
        cron_expression: 新 cron 表达式（如 '*/5 * * * *'）

    Returns:
        {"ok": True, "job_id": job_id, "new_cron": "..."}
    """
    data = JobAiUpdate(job_id=job_id, cron_expression=cron_expression)
    job = await job_service.update_for_ai(
        ctx.db, data, current_user=str(ctx.user.user_id)
    )
    return {
        "ok": True,
        "job_id": str(job.job_id),
        "new_cron": job.cron_expression or "",
    }


# ============ dry_run 函数（命名约定 _dry_run_job_update_cron） ============


async def _dry_run_job_update_cron(
    ctx: AiToolContext, *, job_id: int, cron_expression: str
) -> Any:
    """dry_run：返回当前 job 状态 + 拟变更，让用户在 HITL 抽屉里对比

    不实际改 DB，只读当前 job 然后包装 dry_run 结果。
    """
    from app.modules.ai.agents.hitl.constants import DryRunResult

    job = await job_service.get_by_id(ctx.db, job_id)
    old_cron = job.cron_expression or "(none)"
    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将 cron 从 '{old_cron}' 改为 '{cron_expression}'",
    )
