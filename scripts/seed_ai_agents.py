# ruff: noqa: T201

"""
AI Agent 内置数据填充

按 code upsert：已存在则 UPDATE name/description/display_order（保留部署方自定义的
enabled / system_prompt / model_preference），不存在则 INSERT 完整行。
对应 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §7.3 + §10.1。

所有内置 Agent 默认 enabled=False（开源 TOB 默认禁用，部署方按需启用），
system_prompt="" 留给部署方填业务领域知识，model_preference=None 用全局默认。

Usage:
    cd hohu-admin
    python scripts/seed_ai_agents.py
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.modules.ai.models.agent import AiAgent

# 内置 Agent 定义（spec §10.1）
# display_order 决定 UI 列表顺序；shared 必须存在（file.parse 等 tool 归属它）
AGENT_SEED = [
    {
        "code": "shared",
        "name": "通用工具助手",
        "description": (
            "处理通用工具类请求：文件解析（Excel/CSV）、跨模块统计、不属于其他专用 Agent 的杂项。"
            "当用户问题不属于用户/角色/部门/任务/配置/Provider 任何专用领域时，选本 Agent。"
            "典型 query：'解析这个文件'、'统计系统的总体情况'。"
        ),
        "display_order": 1,
    },
    {
        "code": "user_mgmt",
        "name": "用户管理助手",
        "description": (
            "处理用户 CRUD、密码重置、账号解锁、用户状态变更、用户统计数据查询。"
            "典型 query：'重置 cs123 的密码'、'解锁已锁定的账号'、'统计启用的用户数'。"
            "边界：涉及角色/权限的归 role_mgmt；涉及部门归 dept_mgmt。"
        ),
        "display_order": 2,
    },
    {
        "code": "role_mgmt",
        "name": "角色权限助手",
        "description": (
            "处理角色 CRUD、菜单绑定、权限码分配、角色统计数据查询。"
            "典型 query：'给 role_editor 加 sys:user:export 权限'、'列出所有启用的角色'。"
            "边界：涉及用户增删的归 user_mgmt；涉及按钮权限定义的归 config_mgmt。"
        ),
        "display_order": 3,
    },
    {
        "code": "config_mgmt",
        "name": "系统配置助手",
        "description": (
            "处理系统配置、字典数据、参数查询、菜单结构查询。"
            "典型 query：'查 sys_config 里 ai 相关配置'、'列 dict_data 性别选项'。"
            "边界：涉及用户/角色业务数据的归 user_mgmt / role_mgmt；本 Agent 只管配置元数据。"
        ),
        "display_order": 4,
    },
    {
        "code": "dept_mgmt",
        "name": "部门管理助手",
        "description": (
            "处理部门 CRUD、部门树查询、部门下用户统计。"
            "典型 query：'列出研发部所有子部门'、'统计销售一部有多少人'。"
            "边界：涉及用户本身的归 user_mgmt；涉及角色权限的归 role_mgmt。"
        ),
        "display_order": 5,
    },
    {
        "code": "provider_mgmt",
        "name": "AI Provider 助手",
        "description": (
            "处理 AI Provider（OpenAI / Claude / 自托管）和模型的 CRUD、密钥验证、连通性测试。"
            "典型 query：'添加 OpenAI provider'、'测试 claude-sonnet 连通性'。"
            "边界：本 Agent 只管 Provider/模型元数据；具体对话归其它业务 Agent。"
        ),
        "display_order": 6,
    },
    {
        "code": "job_mgmt",
        "name": "定时任务管理助手",
        "description": (
            "处理定时任务（cron job）的查看、暂停、激活、cron 表达式修改、任务执行日志查询。"
            "典型 query：'修改 job_123 的 cron 为每天 8 点'、'暂停数据同步任务'。"
            "边界：一次性任务（非定时）归 shared。"
        ),
        "display_order": 7,
    },
]


async def seed_ai_agents() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        existing_result = await db.execute(select(AiAgent.code))
        existing_codes = set(existing_result.scalars().all())

        inserted = 0
        updated = 0
        for item in AGENT_SEED:
            if item["code"] in existing_codes:
                # UPDATE description / name / display_order（保留 enabled / system_prompt / model_preference）
                existing = (
                    await db.execute(
                        select(AiAgent).where(AiAgent.code == item["code"])
                    )
                ).scalar_one()
                existing.name = item["name"]
                existing.description = item["description"]
                existing.display_order = item["display_order"]
                updated += 1
                print(f"  update: {item['code']} ({item['name']})")
                continue

            agent = AiAgent(
                agent_id=next_id(),
                code=item["code"],
                name=item["name"],
                description=item["description"],
                display_order=item["display_order"],
                enabled=False,  # spec §10.1: 开源 TOB 默认禁用
                is_builtin=True,  # 内置 Agent，UI 禁止删除
                system_prompt="",  # 部署方自定义（§7.6 与硬编码 SAFETY_PREAMBLE 拼接）
                model_preference=None,  # 用全局默认 openai:gpt-4o
            )
            db.add(agent)
            inserted += 1
            print(f"  insert: {item['code']} ({item['name']})")

        await db.commit()
        print(f"\nDone: {inserted} inserted, {updated} updated.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_ai_agents())
