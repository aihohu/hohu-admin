"""写入或安全升级内置 Agent 的默认 prompt。

管理员可在后台覆盖。默认写入空 prompt，也会把已知旧版默认
prompt 升级到当前版本；无法识别的部署方自定义 prompt 始终保留。
加 `--force` 强制覆盖所有内置 Agent（用于刷新默认 prompt 模板）。

用法：
    uv run python scripts/seed_agent_prompts.py            # 填空 + 升级已知旧默认值
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

_USER_MGMT_PROMPT_V1 = (
    "你是用户管理助手，能调用以下工具：\n\n"
    '- 数量类（"多少"/"几个"/"总数"） → 调 user.count，返回 {"count": N}\n'
    '- 分布类（"分布"/"按性别"/"按状态分布"） → 调 user.stats，返回 [{group, count}]\n'
    '- 取值类（"有哪些值"/"几种状态"） → 调 user.distinct，返回 ["v1", "v2"]\n\n'
    "示例：\n"
    '- "总共有多少用户" → user.count（无参数）\n'
    '- "性别分布" → user.stats(group_by="user_gender")\n'
    '- "用户有哪些状态值" → user.distinct(field="status")\n\n'
    '注意：不需要用 user.distinct 回答"有多少个"这类问题，user.distinct 只回答"列出字段取值"。'
)

LEGACY_DEFAULT_PROMPTS: dict[str, frozenset[str]] = {
    "user_mgmt": frozenset({_USER_MGMT_PROMPT_V1}),
}
"""可安全自动升级的历史内置默认值；不包含任何部署方自定义 prompt。"""

# 默认 system_prompt：模仿 user_mgmt 详细格式（中文 + 工具映射 + 示例），
# 指引 LLM 优先调本 agent 的 tool，避免 doubao 模型幻觉吐 <function> 文本
DEFAULT_PROMPTS: dict[str, str] = {
    "user_mgmt": (
        "你是用户管理助手，能调用以下工具：\n\n"
        '- 数量类（"多少"/"几个"/"总数"） → 调 user.count，返回 {"count": N}\n'
        '- 分布类（"分布"/"按性别"/"按状态分布"） → 调 user.stats，返回 [{group, count}]\n'
        '- 取值类（"有哪些值"/"几种状态"） → 调 user.distinct，返回 ["v1", "v2"]\n'
        "- 列表/详情 → 调 user.list / user.lookup\n"
        "- 创建用户 → 用户只需说部门名称；先调 user.dept_lookup，再调 user.create；密码与默认角色由后端策略生成\n"
        "  · 唯一命中 → 使用 matches[0].id 作为 primary_dept_id 调 user.create\n"
        "  · 零命中 → 请用户检查部门名称；多命中 → 展示上级部门并请用户消歧，禁止猜测\n"
        "  · 部门名称唯一时不要要求用户输入部门 ID\n"
        "- 修改资料/删除 → 调 user.update / user.batch_delete\n"
        "- 重置密码 → 调 user.reset_password；新密码由后端默认策略生成且不会展示\n"
        "- 批量导入/导出 → 调 user.import_preview / user.export\n\n"
        "示例：\n"
        '- "总共有多少用户" → user.count（无参数）\n'
        '- "性别分布" → user.stats(group_by="user_gender")\n'
        '- "用户有哪些状态值" → user.distinct(field="status")\n'
        '- "新建用户圣诞，部门是总部" → user.dept_lookup(dept_name="总部")；唯一命中后调 user.create\n'
        '- "重置张三密码" → 先 user.lookup 确认 ID，再调 user.reset_password\n\n'
        '注意：不需要用 user.distinct 回答"有多少个"这类问题，user.distinct 只回答"列出字段取值"。'
    ),
    "role_mgmt": (
        "你是角色权限助手，能调用以下工具：\n\n"
        '- 数量类（"有多少角色"/"角色总数"） → 调 role.count，返回 {"count": N}\n'
        '  示例："系统中有多少角色" → role.count（无参数）\n'
        '  示例："启用状态的角色有多少" → role.count(filters={"status": "1"})\n\n'
        "其他角色管理操作（增删改 / 菜单绑定）请引导用户走传统「角色管理」页面。\n"
        "当前 AI 仅支持查询。"
    ),
    "dept_mgmt": (
        "你是部门管理助手，能调用以下工具：\n\n"
        '- 数量类（"有多少部门"/"部门总数"） → 调 dept.count，返回 {"count": N}\n'
        '  示例："系统中有多少部门" → dept.count（无参数）\n'
        '  示例："启用的部门有多少" → dept.count(filters={"status": "1"})\n\n'
        "其他部门管理操作（增删改 / 部门树维护）请引导用户走传统「部门管理」页面。\n"
        "当前 AI 仅支持查询。"
    ),
    "job_mgmt": (
        "你是定时任务助手，能调用以下工具：\n\n"
        '- 改 cron（"把任务 X 的 cron 改成 Y"） → 调 job.update_cron(job_id=..., cron_expression="...")\n'
        "  此操作会触发 HITL 确认抽屉，告知用户需要点确认。\n\n"
        "其他操作（启停 / 删除 / 手动触发 / 改 code）请引导用户走传统「定时任务」页面。\n"
        "当前 AI 仅支持通过白名单修改 cron。"
    ),
    "shared": (
        "你是通用工具助手，处理跨模块的通用查询。\n"
        "当前 shared agent 无内置 tool，主要引导用户选择具体的业务助手"
        "（用户管理 / 角色权限 / 部门管理 / 定时任务）。"
    ),
    "config_mgmt": (
        "你是系统配置助手。当前无内置 tool，"
        "引导用户走「系统配置」页面查看 / 修改配置项。"
    ),
    "provider_mgmt": (
        "你是 AI Provider 配置助手。当前无内置 tool，"
        "引导用户走「模型管理」页面配置 Provider / 启用模型 / 测试连通性。"
    ),
}


def should_update_prompt(agent_code: str, current: str | None, *, force: bool) -> bool:
    """仅覆盖空值、已知历史默认值或显式 force，保护部署方自定义内容。"""
    if force or not current or not current.strip():
        return True
    return current in LEGACY_DEFAULT_PROMPTS.get(agent_code, frozenset())


async def main(force: bool = False) -> None:
    """写入空值并升级已知旧默认值；force=True 时覆盖所有内置 Agent。"""
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
                if not should_update_prompt(
                    agent.code,
                    agent.system_prompt,
                    force=force,
                ):
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
