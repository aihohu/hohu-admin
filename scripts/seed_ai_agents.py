# ruff: noqa: T201

"""
AI Agent 内置数据填充

按 code upsert：已存在则 UPDATE name/description/display_order（保留部署方自定义的
enabled / system_prompt / model_preference），不存在则 INSERT 完整行。
集中定义内置 Agent 的初始配置。

新插入行按发布状态设置 enabled；已存在行保留部署方 enabled 值。
system_prompt="" 留给部署方填业务领域知识，model_preference=None 用统一模型 selector。

Usage:
    cd hohu-admin
    python scripts/seed_ai_agents.py
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# AI 模型的复合外键指向 sys_user/sys_role/sys_tenant，mapper 配置期强制解析
# 目标表；独立运行脚本必须先注册这些模型，否则 NoReferencedTableError。
import app.modules.system.models  # noqa: E402, F401
from app.core.config import settings
from app.core.id_generator import next_id
from app.modules.ai.constants import PUBLISHED_AGENT_CODES
from app.modules.ai.models.agent import AiAgent

# 内置 Agent 定义。
# display_order 决定 UI 列表顺序；shared 必须存在（file.parse 等 tool 归属它）
AGENT_SEED = [
    {
        "code": "shared",
        "name": "通用工具助手",
        "description": (
            "处理已上传 Excel/CSV 文件的结构化解析，不承载跨业务模块能力。"
            "典型 query：'解析这个 CSV 文件'、'读取 Excel 的表头和前三行'。"
            "边界：用户、角色和部门业务分别归对应专用 Agent。"
        ),
        "display_order": 1,
    },
    {
        "code": "user_mgmt",
        "name": "用户管理助手",
        "description": (
            "管理用户资料、导入导出以及完整部门和角色集合授权。"
            "典型 query：'调整用户所属部门'、'更新用户角色集合'。"
            "边界：角色定义和部门结构分别归 role_mgmt 与 dept_mgmt。"
        ),
        "display_order": 2,
    },
    {
        "code": "role_mgmt",
        "name": "角色权限助手",
        "description": (
            "管理受限委派角色的定义、菜单完整集合和 Agent 完整集合。"
            "典型 query：'为角色配置菜单权限'、'给角色绑定可委派助手'。"
            "边界：用户成员调整归 user_mgmt；角色删除仍走传统页面。"
        ),
        "display_order": 3,
    },
    {
        "code": "config_mgmt",
        "name": "系统配置助手",
        "description": (
            "系统配置助手尚未发布，当前不提供配置、字典或菜单工具。"
            "未来典型 query：'查询 AI 相关配置'、'列出性别字典选项'。"
            "边界：发布前保持禁用；用户和角色业务分别归 user_mgmt 与 role_mgmt。"
        ),
        "display_order": 4,
    },
    {
        "code": "dept_mgmt",
        "name": "部门管理助手",
        "description": (
            "管理可见范围内的部门资料、状态和组织树移动。"
            "典型 query：'移动部门节点'、'更新部门负责人和状态'。"
            "边界：用户归属调整归 user_mgmt；部门删除仍走传统页面。"
        ),
        "display_order": 5,
    },
    {
        "code": "provider_mgmt",
        "name": "AI Provider 助手",
        "description": (
            "AI Provider 助手尚未发布，当前不提供 Provider 或模型管理工具。"
            "未来典型 query：'添加 OpenAI Provider'、'测试模型连通性'。"
            "边界：发布前保持禁用；具体业务对话归已发布的业务 Agent。"
        ),
        "display_order": 6,
    },
    {
        "code": "job_mgmt",
        "name": "定时任务管理助手",
        "description": (
            "定时任务管理助手尚未发布，当前不提供 cron 或执行日志工具。"
            "未来典型 query：'修改任务执行时间'、'暂停数据同步任务'。"
            "边界：发布前保持禁用；shared 仅负责已上传文件解析。"
        ),
        "display_order": 7,
    },
]


async def seed_ai_agents_in_session(db: AsyncSession) -> tuple[int, int]:
    """幂等写入内置 Agent；调用方负责事务边界。"""
    existing_result = await db.execute(select(AiAgent.code))
    existing_codes = set(existing_result.scalars().all())
    inserted = 0
    updated = 0
    for item in AGENT_SEED:
        if item["code"] in existing_codes:
            # 保留 enabled / system_prompt / model_preference 等部署方配置。
            existing = (
                await db.execute(select(AiAgent).where(AiAgent.code == item["code"]))
            ).scalar_one()
            existing.name = item["name"]
            existing.description = item["description"]
            existing.display_order = item["display_order"]
            updated += 1
            print(f"  update: {item['code']} ({item['name']})")
            continue

        db.add(
            AiAgent(
                agent_id=next_id(),
                code=item["code"],
                name=item["name"],
                description=item["description"],
                display_order=item["display_order"],
                enabled=item["code"] in PUBLISHED_AGENT_CODES,
                is_builtin=True,
                system_prompt="",
                model_preference=None,
            )
        )
        inserted += 1
        print(f"  insert: {item['code']} ({item['name']})")
    await db.flush()
    return inserted, updated


async def seed_ai_agents() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        inserted, updated = await seed_ai_agents_in_session(db)
        await db.commit()
        print(f"\nDone: {inserted} inserted, {updated} updated.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_ai_agents())
