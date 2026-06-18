"""查询 information_schema 拿物理表结构（spec 14.0.1）"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ColumnInfo:
    """information_schema.columns 单行"""

    column_name: str
    data_type: str
    character_maximum_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    is_nullable: bool
    column_default: str | None


@dataclass
class TableExistsResult:
    """物理表的当前结构"""

    table_name: str
    columns: list[ColumnInfo]


async def table_exists(db: AsyncSession, table_name: str) -> bool:
    """检查表是否存在"""
    stmt = text(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :name
        )
    """
    )
    result = await db.execute(stmt, {"name": table_name})
    return bool(result.scalar())


async def introspect_table(
    db: AsyncSession, table_name: str
) -> TableExistsResult | None:
    """查询表的列定义，表不存在返回 None"""
    if not await table_exists(db, table_name):
        return None

    stmt = text(
        """
        SELECT column_name, data_type, character_maximum_length,
               numeric_precision, numeric_scale, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :name
        ORDER BY ordinal_position
    """
    )
    result = await db.execute(stmt, {"name": table_name})
    columns = []
    for row in result:
        columns.append(
            ColumnInfo(
                column_name=row.column_name,
                data_type=row.data_type,
                character_maximum_length=row.character_maximum_length,
                numeric_precision=row.numeric_precision,
                numeric_scale=row.numeric_scale,
                is_nullable=(row.is_nullable == "YES"),
                column_default=row.column_default,
            )
        )
    return TableExistsResult(table_name=table_name, columns=columns)
