# ruff: noqa: T201

"""
AI Agent 内置数据填充

按 code 去重，只插入数据库中不存在的 Agent，已存在则跳过（可安全重复执行）。
对应 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §10.1。

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
        "description": "文件解析、系统级统计等通用工具；任何登录用户直通，无需 role_ai_agent 绑定",
        "display_order": 1,
    },
    {
        "code": "user_mgmt",
        "name": "用户管理助手",
        "description": "用户 / 部门 / 角色查询与维护，含统计聚合（user.count / user.stats / user.distinct）",
        "display_order": 2,
    },
    {
        "code": "role_mgmt",
        "name": "角色权限助手",
        "description": "角色 CRUD、菜单绑定（destructive 操作走 HITL）",
        "display_order": 3,
    },
    {
        "code": "config_mgmt",
        "name": "系统配置助手",
        "description": "系统配置 / 字典数据查询与编辑",
        "display_order": 4,
    },
    {
        "code": "dept_mgmt",
        "name": "部门管理助手",
        "description": "部门树查询 / 维护，含统计聚合（dept.stats）",
        "display_order": 5,
    },
    {
        "code": "provider_mgmt",
        "name": "AI Provider 助手",
        "description": "AI Provider 配置管理 / 连通性测试",
        "display_order": 6,
    },
    {
        "code": "job_mgmt",
        "name": "定时任务助手",
        "description": "定时任务查询 / 启停 / 改 cron（不含 code 字段，§11.3）",
        "display_order": 7,
    },
]


async def seed_ai_agents() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        existing_codes_result = await db.execute(select(AiAgent.code))
        existing_codes = set(existing_codes_result.scalars().all())

        inserted = 0
        for item in AGENT_SEED:
            if item["code"] in existing_codes:
                print(f"  skip: {item['code']} ({item['name']})")
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
            print(f"  + {item['code']} ({item['name']})")

        if inserted:
            await db.commit()
            print(f"\nInserted {inserted} agents.")
        else:
            print("\nAll agents already exist.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_ai_agents())
