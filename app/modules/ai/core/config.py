from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ChatDeps:
    """AI Agent 依赖注入类型"""

    user_id: int
    db: AsyncSession
