"""数据库迁移执行器：CREATE / ALTER / DROP 表（spec 6.4.1 + 14.0.1）"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.marketplace.lowcode.schema_comparator import compare_schemas
from app.modules.marketplace.lowcode.schema_introspection import (
    introspect_table,
    table_exists,
)
from app.modules.marketplace.lowcode.type_mapping import (
    json_schema_to_pg_type,
    pg_type_to_sql,
)


class MigrationRunner:
    """执行低代码 app_data_* 表的 DDL 迁移"""

    async def create_table(
        self, db: AsyncSession, *, table_name: str, data_schema: dict
    ) -> None:
        """CREATE TABLE：系统字段 + 用户字段 + 索引"""
        sys_columns = [
            "id BIGSERIAL PRIMARY KEY",
            "tenant_id BIGINT NOT NULL DEFAULT 0",
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "created_by BIGINT",
            "updated_by BIGINT",
        ]

        properties = data_schema.get("properties", {})
        required = set(data_schema.get("required", []))

        user_columns = []
        for field_name, field_def in properties.items():
            col_def = json_schema_to_pg_type(field_def)
            type_sql = pg_type_to_sql(col_def)
            nullable_sql = "NOT NULL" if field_name in required else "NULL"
            default_sql = _format_default(field_def.get("default"))
            column_def = f"{field_name} {type_sql} {nullable_sql}".strip()
            if default_sql:
                column_def = f"{column_def} {default_sql}"
            user_columns.append(column_def)

        all_columns = sys_columns + user_columns
        columns_sql = ",\n  ".join(all_columns)

        create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} (\n  {columns_sql}\n)"
        await db.execute(text(create_sql))

        # 索引（PG 不支持 CREATE TABLE 内联 INDEX 语法）
        await db.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{table_name}_tenant_id "
                f"ON {table_name} (tenant_id)"
            )
        )
        await db.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{table_name}_created_at "
                f"ON {table_name} (created_at)"
            )
        )

    async def apply_upgrade(
        self, db: AsyncSession, *, table_name: str, new_data_schema: dict
    ) -> None:
        """升级表结构：表不存在则建；存在则 introspect + compare + apply diff"""
        actual = await introspect_table(db, table_name)
        if actual is None:
            await self.create_table(
                db, table_name=table_name, data_schema=new_data_schema
            )
            return

        expected_props = new_data_schema.get("properties", {})
        diff = compare_schemas(actual, expected_props)

        for op in diff.add_columns:
            await db.execute(text(op.to_sql(table_name)))
        for op in diff.alter_columns:
            await db.execute(text(op.to_sql(table_name)))

    async def drop_table(self, db: AsyncSession, *, table_name: str) -> None:
        """DROP TABLE IF EXISTS"""
        if await table_exists(db, table_name):
            await db.execute(text(f"DROP TABLE {table_name}"))

    async def get_table_names_for_app(
        self, db: AsyncSession, *, app_slug: str
    ) -> list[str]:
        """列出某 app 的所有物理表（前缀 app_data_{slug}）"""
        pattern = f"app_data_{app_slug}%"
        stmt = text(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name LIKE :pattern
        """
        )
        result = await db.execute(stmt, {"pattern": pattern})
        return [r[0] for r in result]


def _format_default(default: object | None) -> str:
    """把 Python 值 → SQL DEFAULT 字符串"""
    if default is None:
        return ""
    if isinstance(default, bool):
        return f"DEFAULT {str(default).upper()}"
    if isinstance(default, str):
        return f"DEFAULT '{default}'"
    return f"DEFAULT {default}"
