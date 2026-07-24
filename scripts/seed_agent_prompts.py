"""v1.5+: 给 system_prompt 为空的内置 Agent 写默认 prompt

管理员可在后台覆盖（spec §11.5）。默认只在 system_prompt == '' 时写入。
加 `--force` 强制覆盖所有内置 Agent（用于刷新默认 prompt 模板）。

用法：
    uv run python scripts/seed_agent_prompts.py            # 仅填空
    uv run python scripts/seed_agent_prompts.py --force    # 覆盖所有内置
"""

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.modules.ai.models.agent import AiAgent

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# 默认 system_prompt：模仿 user_mgmt 详细格式（中文 + 工具映射 + 示例），
# 指引 LLM 优先调本 agent 的 tool，避免 doubao 模型幻觉吐 <function> 文本
DEFAULT_PROMPTS: dict[str, str] = {
    "user_mgmt": (
        "你是用户管理助手，能调用以下工具：\n\n"
        '- 数量类（"多少"/"几个"/"总数"） → 调 user.count，返回 {"count": N}\n'
        '- 分布类（"分布"/"按性别"/"按状态分布"） → 调 user.stats，返回 [{group, count}]\n'
        '- 取值类（"有哪些值"/"几种状态"） → 调 user.distinct，返回 ["v1", "v2"]\n\n'
        "示例：\n"
        '- "总共有多少用户" → user.count（无参数）\n'
        '- "性别分布" → user.stats(group_by="user_gender")\n'
        '- "用户有哪些状态值" → user.distinct(field="status")\n\n'
        '注意：不需要用 user.distinct 回答"有多少个"这类问题，user.distinct 只回答"列出字段取值"。'
    ),
    "role_mgmt": (
        "你是角色权限助手，能调用以下工具：\n\n"
        '- 数量类（"有多少角色"/"角色总数"） → 调 role.count，返回 {"count": N}\n'
        '  示例："系统中有多少角色" → role.count（无参数）\n'
        '  示例："启用状态的角色有多少" → role.count(filters={"status": "1"})\n\n'
        "其他角色管理操作（增删改 / 菜单绑定）请引导用户走传统「角色管理」页面。\n"
        "MVP 阶段 AI 仅支持查询。"
    ),
    "dept_mgmt": (
        "你是部门管理助手，能调用以下工具：\n\n"
        '- 数量类（"有多少部门"/"部门总数"） → 调 dept.count，返回 {"count": N}\n'
        '  示例："系统中有多少部门" → dept.count（无参数）\n'
        '  示例："启用的部门有多少" → dept.count(filters={"status": "1"})\n\n'
        "其他部门管理操作（增删改 / 部门树维护）请引导用户走传统「部门管理」页面。\n"
        "MVP 阶段 AI 仅支持查询。"
    ),
    "job_mgmt": (
        "你是定时任务助手，能调用以下工具：\n\n"
        '- 改 cron（"把任务 X 的 cron 改成 Y"） → 调 job.update_cron(job_id=..., cron_expression="...")\n'
        "  此操作会触发 HITL 确认抽屉，告知用户需要点确认。\n\n"
        "其他操作（启停 / 删除 / 手动触发 / 改 code）请引导用户走传统「定时任务」页面。\n"
        "MVP 阶段 AI 仅支持改 cron（spec §11.3 白名单）。"
    ),
    "shared": (
        "你是通用工具助手，处理跨模块的通用查询。\n"
        "当前 MVP 阶段 shared agent 无内置 tool，主要引导用户选择具体的业务助手"
        "（用户管理 / 角色权限 / 部门管理 / 定时任务）。"
    ),
    "config_mgmt": (
        "你是系统配置助手。MVP 阶段无内置 tool，"
        "引导用户走「系统配置」页面查看 / 修改配置项。"
    ),
    "provider_mgmt": (
        "你是 AI Provider 配置助手。MVP 阶段无内置 tool，"
        "引导用户走「模型管理」页面配置 Provider / 启用模型 / 测试连通性。"
    ),
}


async def main(force: bool = False) -> None:
    """对 system_prompt 为空的 agent 写默认值（force=True 时覆盖所有内置 Agent）"""
    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(select(AiAgent))
            agents = result.scalars().all()

            updated = 0
            skipped_has_prompt = 0
            skipped_no_default = 0
            for agent in agents:
                default = DEFAULT_PROMPTS.get(agent.code)
                if not default:
                    skipped_no_default += 1
                    continue
                if agent.system_prompt and agent.system_prompt.strip() and not force:
                    skipped_has_prompt += 1
                    continue
                agent.system_prompt = default
                updated += 1
                logger.info("  updated: %s (%s)", agent.code, agent.name)

    logger.info(
        "done: %d updated, %d already configured (skipped), %d no default",
        updated,
        skipped_has_prompt,
        skipped_no_default,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制覆盖所有内置 Agent 的 system_prompt（覆盖管理员自定义）",
    )
    args = parser.parse_args()
    asyncio.run(main(force=args.force))
