"""job 模块的 AI Tool。

job.update_cron: 改调度 cron 表达式（白名单字段，经 JobAiUpdate schema 强制过滤）
  - risk=high（改调度 = 改执行行为）
  - hitl_always=True（调度变更必须 HITL）
  - dry_run_supported=True（dry_run 显示当前 cron vs 拟变更）

AI 不允许修改的字段：job_key / run_on_enable / timeout /
max_retries / concurrent — JobAiUpdate schema 在 update_for_ai 入口兜底丢弃。
"""

# ruff: noqa: PLC0415  inline import 避免循环

from typing import Any

from app.modules.ai.agents.gateway.result import ToolResult, UIResult
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
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="detail_card",
    )
)
async def job_update_cron(
    ctx: AiToolContext, *, job_id: int, cron_expression: str
) -> ToolResult:
    """通过字段白名单更新任务 cron 表达式。

    Args:
        ctx: AiToolContext
        job_id: 任务 ID
        cron_expression: 新 cron 表达式（如 '*/5 * * * *'）

    Returns:
        ToolResult：data 给 LLM（{ok, job_id, new_cron}），ui 给前端 detail_card
        渲染（title + 任务 ID/新 cron 两个字段 + before/after 审计用于合规追溯）。
    """
    # 先读旧 cron，做 before/after 审计（cron 变更必须留合规轨迹）
    old_job = await job_service.get_by_id(ctx.db, job_id)
    old_cron = old_job.cron_expression or ""

    data = JobAiUpdate(job_id=job_id, cron_expression=cron_expression)
    job = await job_service.update_for_ai(
        ctx.db, data, current_user=str(ctx.user.user_id)
    )
    new_cron = job.cron_expression or ""
    job_id_str = str(job.job_id)
    return ToolResult.success(
        data={"ok": True, "job_id": job_id_str, "new_cron": new_cron},
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": "定时任务 cron 已更新",
                "fields": [
                    {"label": "ai.tool.field.jobId", "value": job_id_str},
                    {"label": "ai.tool.field.newCron", "value": new_cron},
                ],
            },
            audit={"job_id": job_id_str, "before": old_cron, "after": new_cron},
            label_key="ai.tool.job.update_cron.result",
        ),
    )


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
        summary_key="page.ai.chat.confirmUpdateCronSummary",
        summary_params={"oldCron": old_cron, "newCron": cron_expression},
    )
