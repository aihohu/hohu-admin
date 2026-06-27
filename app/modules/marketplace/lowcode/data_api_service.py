"""通用动态数据 CRUD（spec 6.2）"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import InvalidParameterException, NotFoundException
from app.modules.marketplace.exceptions import AppErrorCode
from app.modules.marketplace.lowcode.schema_introspection import (
    introspect_table,
)

# 系统字段（不允许用户修改或过滤）
SYSTEM_FIELDS = {
    "id",
    "tenant_id",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
}

# Filter operator → 适用 PG data_type 集合（spec 6.2 / 决策 #75）
# information_schema.columns.data_type 是小写：'text' / 'integer' / 'jsonb' ...
_TEXT_TYPES = {"text", "character varying", "character"}
_NUMERIC_TYPES = {
    "integer",
    "bigint",
    "smallint",
    "numeric",
    "decimal",
    "real",
    "double precision",
}
_DATE_TYPES = {"date", "timestamp without time zone", "timestamp with time zone"}
_JSONB_TYPES = {"jsonb", "json"}

# op → 允许的列类型集合（None = 任意类型都可）
_OP_TYPE_RULES: dict[str, set[str] | None] = {
    "eq": None,
    "contains": _TEXT_TYPES,
    "in": None,
    "gte": _NUMERIC_TYPES | _DATE_TYPES,
    "lte": _NUMERIC_TYPES | _DATE_TYPES,
    "has": _JSONB_TYPES,
}

# 已知 op 集合（用于错误码区分 unknown op）
_KNOWN_OPS = set(_OP_TYPE_RULES.keys())


def _parse_filter_key(key: str) -> tuple[str, str]:
    """`name__contains` → ('name', 'contains')；无后缀 → ('name', 'eq')"""
    if "__" in key:
        field, op = key.rsplit("__", 1)
        return field, op
    return key, "eq"


def _serialize_for_bind(value: Any) -> Any:
    """JSONB 列接收 dict/list 时序列化为 JSON 字符串（asyncpg 不能直接绑 list/dict）"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _validate_field_op(field: str, op: str, column_types: dict[str, str]) -> None:
    """校验列存在 + 操作符类型匹配 + 非系统字段"""
    if field in SYSTEM_FIELDS:
        raise InvalidParameterException(
            f"系统字段 {field} 不允许过滤/排序",
            error_code=AppErrorCode.FILTER_SYSTEM_FIELD_FORBIDDEN,
        )
    if field not in column_types:
        raise InvalidParameterException(
            f"未知字段：{field}",
            error_code=AppErrorCode.FILTER_UNKNOWN_FIELD,
        )
    if op not in _KNOWN_OPS:
        raise InvalidParameterException(
            f"不支持的过滤操作符：{op}",
            error_code=AppErrorCode.FILTER_INVALID_OPERATOR,
        )
    allowed_types = _OP_TYPE_RULES[op]
    if allowed_types is not None and column_types[field] not in allowed_types:
        raise InvalidParameterException(
            f"字段 {field}（{column_types[field]}）不支持操作符 __{op}",
            error_code=AppErrorCode.FILTER_OP_TYPE_MISMATCH,
        )


def _cast_for_range_op(data_type: str) -> str:
    """返回 gte/lte 应用的 PG 类型 cast（用于 SQL `CAST(:v AS <type>)`）；空字符串表示不 cast。"""
    if data_type in _NUMERIC_TYPES:
        return "NUMERIC"
    if data_type in _DATE_TYPES:
        return "TIMESTAMPTZ"
    return ""


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
        # JSONB 列需 json.dumps（asyncpg 不直接绑 list/dict）
        bound_data = {k: _serialize_for_bind(v) for k, v in full_data.items()}

        columns = list(bound_data.keys())
        placeholders = [f":{c}" for c in columns]
        sql = text(
            f"INSERT INTO {table_name} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING *"
        )
        result = await db.execute(sql, bound_data)
        row = result.fetchone()
        return dict(row._mapping)

    async def list(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        current: int,
        size: int,
        filters: dict[str, Any] | None,
        tenant_id: int,
        order_by: str | None = None,
    ) -> PageResult:
        # 拿列类型 map（决策 #76：列存在性 + 类型匹配校验）
        table_info = await introspect_table(db, table_name)
        if table_info is None:
            raise NotFoundException(resource_type=f"表 {table_name}")
        column_types = {c.column_name: c.data_type for c in table_info.columns}

        where_clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"tenant_id": tenant_id}

        if filters:
            for key, value in filters.items():
                field, op = _parse_filter_key(key)
                _validate_field_op(field, op, column_types)
                self._apply_filter(
                    field, op, value, where_clauses, params, column_types
                )

        where_sql = " AND ".join(where_clauses)

        count_sql = text(f"SELECT COUNT(*) FROM {table_name} WHERE {where_sql}")
        total = (await db.execute(count_sql, params)).scalar() or 0

        order_clause = self._build_order_clause(order_by, column_types)

        offset = (current - 1) * size
        list_sql = text(
            f"SELECT * FROM {table_name} WHERE {where_sql} "
            f"ORDER BY {order_clause} LIMIT :limit OFFSET :offset"
        )
        params["limit"] = size
        params["offset"] = offset
        result = await db.execute(list_sql, params)
        records = [dict(row._mapping) for row in result.fetchall()]

        return PageResult(records=records, total=total, current=current, size=size)

    @staticmethod
    def _apply_filter(
        field: str,
        op: str,
        value: Any,
        where_clauses: list[str],
        params: dict[str, Any],
        column_types: dict[str, str] | None = None,
    ) -> None:
        """把单个 filter 追加到 where + params（参数化，禁 raw f-string 值）

        gte/lte 在数值/日期列上用 CAST 强转，因为 query param 总是 string，
        PG 严格类型检查会拒绝 `integer >= text`。
        """
        param_key = f"filter_{field}_{op}"
        col_type = (column_types or {}).get(field, "")
        cast_sql = _cast_for_range_op(col_type)

        if op == "eq":
            where_clauses.append(f"{field} = :{param_key}")
            params[param_key] = value
        elif op == "contains":
            where_clauses.append(f"{field} ILIKE :{param_key}")
            params[param_key] = f"%{value}%"
        elif op == "in":
            items = [v.strip() for v in str(value).split(",") if v.strip()]
            if not items:
                raise InvalidParameterException(
                    f"__in 操作符要求至少一个值：{field}",
                    error_code=AppErrorCode.FILTER_INVALID_OPERATOR,
                )
            placeholders = []
            for i, item in enumerate(items):
                pk = f"{param_key}_{i}"
                placeholders.append(f":{pk}")
                params[pk] = item
            where_clauses.append(f"{field} IN ({', '.join(placeholders)})")
        elif op == "gte":
            placeholder = (
                f"CAST(:{param_key} AS {cast_sql})" if cast_sql else f":{param_key}"
            )
            where_clauses.append(f"{field} >= {placeholder}")
            params[param_key] = value
        elif op == "lte":
            placeholder = (
                f"CAST(:{param_key} AS {cast_sql})" if cast_sql else f":{param_key}"
            )
            where_clauses.append(f"{field} <= {placeholder}")
            params[param_key] = value
        elif op == "has":
            # JSONB array contains: column ? value (PG jsonb ? operator)
            where_clauses.append(f"cast({field} as jsonb) ? cast(:{param_key} as text)")
            params[param_key] = str(value)

    @staticmethod
    def _build_order_clause(order_by: str | None, column_types: dict[str, str]) -> str:
        """`-created_at,name` → 'created_at DESC, name ASC'

        允许排序的系统字段：id / created_at / updated_at / created_by / updated_by。
        禁止 tenant_id（在租户内无意义，且易误导）。
        """
        always_allowed_system = {
            "id",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        }
        if not order_by:
            return "created_at DESC"

        parts: list[str] = []
        for raw in order_by.split(","):
            token = raw.strip()
            if not token:
                continue
            direction = "ASC"
            field = token
            if token.startswith("-"):
                direction = "DESC"
                field = token[1:].strip()
            # tenant_id 单独禁（其他系统字段允许排序）
            if field == "tenant_id":
                raise InvalidParameterException(
                    "系统字段 tenant_id 不允许排序",
                    error_code=AppErrorCode.FILTER_SYSTEM_FIELD_FORBIDDEN,
                )
            if field not in column_types and field not in always_allowed_system:
                raise InvalidParameterException(
                    f"未知排序字段：{field}",
                    error_code=AppErrorCode.FILTER_UNKNOWN_FIELD,
                )
            parts.append(f"{field} {direction}")

        if not parts:
            return "created_at DESC"
        return ", ".join(parts)

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

        # JSONB 列需 json.dumps
        bound_data = {k: _serialize_for_bind(v) for k, v in clean_data.items()}

        set_parts = [f"{k} = :{k}" for k in bound_data.keys()]
        set_sql = ", ".join(set_parts)

        sql = text(
            f"UPDATE {table_name} SET {set_sql} "
            f"WHERE id = :record_id AND tenant_id = :tenant_id RETURNING *"
        )
        params = {**bound_data, "record_id": record_id, "tenant_id": tenant_id}
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
