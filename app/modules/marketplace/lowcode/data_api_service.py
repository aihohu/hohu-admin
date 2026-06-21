"""通用动态数据 CRUD（spec 6.2）"""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import InvalidParameterException, NotFoundException

# 系统字段（不允许用户修改或过滤）
SYSTEM_FIELDS = {
    "id",
    "tenant_id",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
}


class DataApiService:
    """通用动态数据 CRUD（所有 app_data_* 表共用）"""

    async def create(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        data: dict,
        tenant_id: int,
        user_id: int,
        data_schema: dict | None = None,
    ) -> dict:
        # 校验 required
        if data_schema:
            required = data_schema.get("required", [])
            for f in required:
                if f not in data:
                    raise InvalidParameterException(f"缺少必填字段：{f}")

        now = datetime.now(UTC)
        full_data = {
            **data,
            "tenant_id": tenant_id,
            "created_by": user_id,
            "updated_by": user_id,
            "created_at": now,
            "updated_at": now,
        }

        columns = list(full_data.keys())
        placeholders = [f":{c}" for c in columns]
        sql = text(
            f"INSERT INTO {table_name} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING *"
        )
        result = await db.execute(sql, full_data)
        row = result.fetchone()
        return dict(row._mapping)

    async def list(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        current: int,
        size: int,
        filters: dict | None,
        tenant_id: int,
    ) -> PageResult:
        where_clauses = ["tenant_id = :tenant_id"]
        params: dict = {"tenant_id": tenant_id}
        if filters:
            for k, v in filters.items():
                if k not in SYSTEM_FIELDS:  # 不允许按系统字段过滤
                    where_clauses.append(f"{k} = :filter_{k}")
                    params[f"filter_{k}"] = v

        where_sql = " AND ".join(where_clauses)

        count_sql = text(f"SELECT COUNT(*) FROM {table_name} WHERE {where_sql}")
        total = (await db.execute(count_sql, params)).scalar() or 0

        offset = (current - 1) * size
        list_sql = text(
            f"SELECT * FROM {table_name} WHERE {where_sql} "
            f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        )
        params["limit"] = size
        params["offset"] = offset
        result = await db.execute(list_sql, params)
        records = [dict(row._mapping) for row in result.fetchall()]

        return PageResult(records=records, total=total, current=current, size=size)

    async def get(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        record_id: int,
        tenant_id: int,
    ) -> dict:
        sql = text(
            f"SELECT * FROM {table_name} WHERE id = :id AND tenant_id = :tenant_id"
        )
        result = await db.execute(sql, {"id": record_id, "tenant_id": tenant_id})
        row = result.fetchone()
        if row is None:
            raise NotFoundException(resource_type="记录")
        return dict(row._mapping)

    async def update(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        record_id: int,
        data: dict,
        tenant_id: int,
        user_id: int,
    ) -> dict:
        # 移除系统字段
        clean_data = {k: v for k, v in data.items() if k not in SYSTEM_FIELDS}
        clean_data["updated_at"] = datetime.now(UTC)
        clean_data["updated_by"] = user_id

        if not clean_data:
            raise InvalidParameterException("没有可更新的字段")

        set_parts = [f"{k} = :{k}" for k in clean_data.keys()]
        set_sql = ", ".join(set_parts)

        sql = text(
            f"UPDATE {table_name} SET {set_sql} "
            f"WHERE id = :record_id AND tenant_id = :tenant_id RETURNING *"
        )
        params = {**clean_data, "record_id": record_id, "tenant_id": tenant_id}
        result = await db.execute(sql, params)
        row = result.fetchone()
        if row is None:
            raise NotFoundException(resource_type="记录")
        return dict(row._mapping)

    async def delete(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        record_id: int,
        tenant_id: int,
    ) -> None:
        sql = text(
            f"DELETE FROM {table_name} WHERE id = :id AND tenant_id = :tenant_id"
        )
        result = await db.execute(sql, {"id": record_id, "tenant_id": tenant_id})
        if result.rowcount == 0:
            raise NotFoundException(resource_type="记录")


data_api_service = DataApiService()
