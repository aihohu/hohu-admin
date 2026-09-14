"""通用动态数据 CRUD（spec 6.2）"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import InvalidParameterException, NotFoundException
from app.core.tenant import TenantContext
from app.modules.marketplace.capability import require_marketplace_capability
from app.modules.marketplace.exceptions import AppErrorCode
from app.modules.marketplace.lowcode.identifiers import (
    SYSTEM_FIELDS,
    quote_identifier,
    quote_table_name,
    validate_manifest_identifier,
)
from app.modules.marketplace.lowcode.schema_introspection import (
    introspect_table,
)
from app.modules.marketplace.lowcode.type_mapping import make_table_name

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


def _collect_belongs_to(
    data_schema: dict | None, models: list | None, model_key: str | None = None
) -> list[dict]:
    """Parse ``belongs_to`` relations from the manifest.

    Priority: explicit model.relations[] takes precedence over field-level x-ref.
    Returns list of {model, foreign_key, label_field?} dicts.
    """
    out: list[dict] = []
    seen_fk: set[str] = set()

    # 1. Explicit relations[] (top-level data_schema OR each model in models[])
    schemas_to_check: list[tuple[dict, list]] = []
    if models:
        for m in models:
            if isinstance(m, dict) and (model_key is None or m.get("key") == model_key):
                schemas_to_check.append(
                    (m.get("data_schema") or {}, m.get("relations") or [])
                )
    elif data_schema:
        # Single-table mode: relations live at top-level (rare but spec allows)
        schemas_to_check.append((data_schema, data_schema.get("relations") or []))

    for _, rels in schemas_to_check:
        for rel in rels:
            if not isinstance(rel, dict) or rel.get("type") != "belongs_to":
                continue
            fk = rel.get("foreign_key")
            target = rel.get("model")
            if not fk or not target:
                continue
            if fk in seen_fk:
                continue
            seen_fk.add(fk)
            out.append(
                {
                    "model": target,
                    "foreign_key": fk,
                    "label_field": rel.get("label_field"),
                }
            )

    # 2. Field-level x-ref (fallback when no explicit relations declaration)
    field_schemas: list[dict] = []
    if models:
        for m in models:
            if isinstance(m, dict) and (model_key is None or m.get("key") == model_key):
                field_schemas.append(m.get("data_schema") or {})
    elif data_schema:
        field_schemas.append(data_schema)

    for schema in field_schemas:
        for fname, fdef in (schema.get("properties") or {}).items():
            if not isinstance(fdef, dict):
                continue
            target = fdef.get("x-ref")
            if not target or fname in seen_fk:
                continue
            seen_fk.add(fname)
            out.append(
                {
                    "model": target,
                    "foreign_key": fname,
                    "label_field": fdef.get("x-ref-label"),
                }
            )

    return out


def _resolve_relation_label_field(
    column_types: dict[str, str], requested: object, *, table_name: str
) -> str | None:
    """Resolve a relation label only from columns that physically exist."""
    if requested is not None:
        label_field = validate_manifest_identifier(
            requested, label="relation label field", reject_system=True
        )
        data_type = column_types.get(label_field)
        if data_type is None:
            raise InvalidParameterException(
                f"关联表 {table_name} 不存在 label 字段：{label_field}",
                error_code=AppErrorCode.FILTER_UNKNOWN_FIELD,
            )
        if data_type not in _TEXT_TYPES:
            raise InvalidParameterException(
                f"关联字段 {table_name}.{label_field} 不是字符串列",
                error_code=AppErrorCode.FILTER_OP_TYPE_MISMATCH,
            )
        return label_field

    return next(
        (name for name, data_type in column_types.items() if data_type in _TEXT_TYPES),
        None,
    )


async def _column_types(db: AsyncSession, table_name: str) -> dict[str, str]:
    """Return {column_name: pg_data_type} for a table; empty if table missing."""
    info = await introspect_table(db, table_name)
    if info is None:
        return {}
    return {c.column_name: c.data_type for c in info.columns}


async def _validate_payload_fields(
    db: AsyncSession,
    *,
    table_name: str,
    data: dict,
    data_schema: dict | None,
) -> dict[str, str]:
    """Require body keys in the manifest/physical-column intersection."""
    quote_table_name(table_name)
    column_types = await _column_types(db, table_name)
    if not column_types:
        raise NotFoundException(resource_type=f"表 {table_name}")
    if data_schema is None:
        # Internal callers may rely on introspection only. HTTP runtime always
        # supplies the validated installed manifest schema.
        manifest_fields = set(column_types) - SYSTEM_FIELDS
    else:
        properties = data_schema.get("properties")
        if not isinstance(properties, dict):
            raise InvalidParameterException("data_schema.properties 无效")
        manifest_fields = {
            validate_manifest_identifier(
                name, label="data_schema field", reject_system=True
            )
            for name in properties
        }

    requested = set(data)
    system_fields = requested & SYSTEM_FIELDS
    if system_fields:
        raise InvalidParameterException(
            f"系统字段不允许由客户端写入：{sorted(system_fields)}"
        )
    allowed = manifest_fields & set(column_types)
    unknown = requested - allowed
    if unknown:
        raise InvalidParameterException(f"未知或未授权字段：{sorted(unknown)}")
    return column_types


def _coerce_numeric_strings(data: dict, column_types: dict[str, str]) -> dict:
    """Convert digit-string values to int for integer/bigint columns.

    Frontend sends Snowflake IDs as strings (JS BigInt precision); asyncpg
    rejects str→BIGINT auto-bind. Coerce here so FK fields round-trip cleanly.
    Leaves string columns, JSONB, dates, etc. untouched.
    """
    out = dict(data)
    for key, value in out.items():
        if not isinstance(value, str):
            continue
        col_type = column_types.get(key)
        if col_type not in _NUMERIC_TYPES:
            continue
        # Integer columns: only coerce pure-digit strings (with optional leading -)
        stripped = value.strip()
        if stripped and (stripped.lstrip("-").isdigit()):
            out[key] = int(stripped)
        # Else: leave as-is (PG will raise if it really can't bind)
    return out


class DataApiService:
    """通用动态数据 CRUD（所有 app_data_* 表共用）"""

    async def create(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        data: dict,
        user_id: int,
        tenant: TenantContext,
        data_schema: dict | None = None,
    ) -> dict:
        require_marketplace_capability(tenant)
        quoted_table = quote_table_name(table_name)
        column_types = await _validate_payload_fields(
            db,
            table_name=table_name,
            data=data,
            data_schema=data_schema,
        )
        # 校验 required
        if data_schema:
            required = data_schema.get("required", [])
            for f in required:
                if f not in data:
                    raise InvalidParameterException(f"缺少必填字段：{f}")

        now = datetime.now(UTC)
        full_data = {
            **data,
            "tenant_id": tenant.tenant_id,
            "created_by": user_id,
            "updated_by": user_id,
            "created_at": now,
            "updated_at": now,
        }
        # Coerce string IDs → int for numeric columns (Snowflake IDs arrive as
        # strings from frontend to dodge JS BigInt precision loss; asyncpg
        # rejects str→BIGINT auto-bind). Then JSONB-serialize dict/list.
        coerced = _coerce_numeric_strings(full_data, column_types)
        bound_data = {k: _serialize_for_bind(v) for k, v in coerced.items()}

        columns = list(bound_data.keys())
        quoted_columns = [
            quote_identifier(column, label="insert column") for column in columns
        ]
        placeholders = [f":{column}" for column in columns]
        sql = text(
            f"INSERT INTO {quoted_table} ({', '.join(quoted_columns)}) "
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
        tenant: TenantContext,
        order_by: str | None = None,
        slug: str | None = None,
        data_schema: dict | None = None,
        models: list | None = None,
        model_key: str | None = None,
    ) -> PageResult:
        require_marketplace_capability(tenant)
        quoted_table = quote_table_name(table_name)
        # 拿列类型 map（决策 #76：列存在性 + 类型匹配校验）
        table_info = await introspect_table(db, table_name)
        if table_info is None:
            raise NotFoundException(resource_type=f"表 {table_name}")
        column_types = {c.column_name: c.data_type for c in table_info.columns}

        where_clauses = [f'{quoted_table}."tenant_id" = :tenant_id']
        params: dict[str, Any] = {"tenant_id": tenant.tenant_id}

        if filters:
            for key, value in filters.items():
                field, op = _parse_filter_key(key)
                _validate_field_op(field, op, column_types)
                self._apply_filter(
                    field,
                    op,
                    value,
                    where_clauses,
                    params,
                    column_types,
                    qualifier=quoted_table,
                )

        where_sql = " AND ".join(where_clauses)

        count_sql = text(f"SELECT COUNT(*) FROM {quoted_table} WHERE {where_sql}")
        total = (await db.execute(count_sql, params)).scalar() or 0

        # Parse order tokens. Detects <fk>_label tokens (belongs_to JOIN sort,
        # decision #79) and builds LEFT JOIN clauses; falls back to direct
        # column sort for real fields. _build_order_clause was the previous
        # entry point but didn't have relations context, so the logic moved
        # inline here.
        relations = (
            _collect_belongs_to(data_schema, models, model_key)
            if slug and (data_schema or models)
            else []
        )
        fk_to_rel = {r["foreign_key"]: r for r in relations}

        sort_clauses: list[str] = []
        join_clauses: list[str] = []
        label_suffix = "_label"
        always_allowed_system = {
            "id",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        }

        if order_by:
            for sort_index, raw in enumerate(order_by.split(",")):
                token = raw.strip()
                if not token:
                    continue
                direction = "DESC" if token.startswith("-") else "ASC"
                field = token.lstrip("-").strip()

                if field.endswith(label_suffix):
                    fk_field = field[: -len(label_suffix)]
                    if fk_field in fk_to_rel:
                        if fk_field not in column_types:
                            raise InvalidParameterException(
                                f"关联外键字段不存在：{fk_field}",
                                error_code=AppErrorCode.FILTER_UNKNOWN_FIELD,
                            )
                        rel = fk_to_rel[fk_field]
                        target_table = make_table_name(slug, rel["model"])
                        quoted_target = quote_table_name(target_table)
                        target_info = await introspect_table(db, target_table)
                        if target_info is None:
                            raise InvalidParameterException(
                                f"关联表不存在：{target_table}",
                                error_code=AppErrorCode.FILTER_UNKNOWN_FIELD,
                            )
                        target_column_types = {
                            column.column_name: column.data_type
                            for column in target_info.columns
                        }
                        label_field = _resolve_relation_label_field(
                            target_column_types,
                            rel.get("label_field"),
                            table_name=target_table,
                        )
                        if not label_field:
                            raise InvalidParameterException(
                                f"关联表 {target_table} 无字符串列，无法按 label 排序",
                                error_code=AppErrorCode.FILTER_OP_TYPE_MISMATCH,
                            )
                        alias = f"sort_{sort_index}"
                        quoted_alias = quote_identifier(alias, label="sort alias")
                        quoted_fk = quote_identifier(
                            fk_field, label="relation foreign key"
                        )
                        quoted_label = quote_identifier(
                            label_field, label="relation label field"
                        )
                        join_clauses.append(
                            f"LEFT JOIN {quoted_target} {quoted_alias} "
                            f'ON {quoted_table}.{quoted_fk} = {quoted_alias}."id" '
                            f'AND {quoted_table}."tenant_id" = '
                            f'{quoted_alias}."tenant_id"'
                        )
                        sort_clauses.append(
                            f"{quoted_alias}.{quoted_label} {direction} NULLS LAST"
                        )
                        continue

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
                quoted_field = quote_identifier(field, label="order field")
                sort_clauses.append(f"{quoted_table}.{quoted_field} {direction}")

        if not sort_clauses:
            sort_clauses = [f'{quoted_table}."created_at" DESC']
        order_clause = ", ".join(sort_clauses)
        joins_sql = " ".join(join_clauses)

        offset = (current - 1) * size
        # SELECT table_name.* (not *) when JOINs present — avoid ambiguous
        # column errors (e.g., both tables have `id`).
        select_clause = f"{quoted_table}.*" if joins_sql else "*"
        list_sql = text(
            f"SELECT {select_clause} FROM {quoted_table} {joins_sql} "
            f"WHERE {where_sql} ORDER BY {order_clause} "
            f"LIMIT :limit OFFSET :offset"
        )
        params["limit"] = size
        params["offset"] = offset
        result = await db.execute(list_sql, params)
        records = [dict(row._mapping) for row in result.fetchall()]

        # Auto-expand belongs_to relations (decision #79): pull related label
        # from target table, write back as <fk_field>_label on each record.
        if relations:
            await self._expand_belongs_to(
                db,
                records=records,
                slug=slug,
                data_schema=data_schema,
                models=models,
                model_key=model_key,
                tenant=tenant,
            )

        return PageResult(records=records, total=total, current=current, size=size)

    @staticmethod
    async def _expand_belongs_to(
        db: AsyncSession,
        *,
        records: list[dict],
        slug: str,
        data_schema: dict | None,
        models: list | None,
        model_key: str | None,
        tenant: TenantContext,
    ) -> None:
        """Merge related label field onto each record (mutates in place).

        Relation declarations are read from these sources in priority order:
        1. model.relations[] (explicit, takes precedence)
        2. field-level `x-ref` + optional `x-ref-label`

        Emits `record[<fk_field>_label]`. Skips silently if target table missing
        or no FK values present.
        """
        if not records:
            return

        relations = _collect_belongs_to(data_schema, models, model_key)
        if not relations:
            return

        for rel in relations:
            fk_values = {
                r[rel["foreign_key"]]
                for r in records
                if r.get(rel["foreign_key"]) is not None
            }
            if not fk_values:
                continue

            target_table = make_table_name(slug, rel["model"])
            quoted_target = quote_table_name(target_table)
            target_info = await introspect_table(db, target_table)
            if target_info is None:
                continue

            # Resolve label_field: explicit > first string column on target > '#<id>'
            target_column_types = {
                column.column_name: column.data_type for column in target_info.columns
            }
            label_field = _resolve_relation_label_field(
                target_column_types,
                rel.get("label_field"),
                table_name=target_table,
            )
            if not label_field:
                for record in records:
                    fk = record.get(rel["foreign_key"])
                    record[f"{rel['foreign_key']}_label"] = (
                        f"#{fk}" if fk is not None else ""
                    )
                continue
            quoted_label = quote_identifier(
                label_field, label="relation label field", reject_system=True
            )

            # Batch SELECT id, label_field FROM target WHERE id IN (...)
            placeholders: list[str] = []
            params: dict[str, Any] = {}
            for i, v in enumerate(fk_values):
                pk = f"fk_{i}"
                placeholders.append(f":{pk}")
                params[pk] = v
            select_sql = text(
                f'SELECT "id", {quoted_label} FROM {quoted_target} '
                f'WHERE "tenant_id" = :tenant_id '
                f'AND "id" IN ({", ".join(placeholders)})'
            )
            params["tenant_id"] = tenant.tenant_id
            rows = (await db.execute(select_sql, params)).fetchall()
            label_map = {row.id: getattr(row, label_field) for row in rows}

            for r in records:
                fk = r.get(rel["foreign_key"])
                if fk is None:
                    r[f"{rel['foreign_key']}_label"] = ""
                    continue
                if fk in label_map:
                    r[f"{rel['foreign_key']}_label"] = label_map[fk]
                elif label_field:
                    # FK exists but target row missing (e.g. deleted parent)
                    r[f"{rel['foreign_key']}_label"] = ""
                else:
                    # label_field resolved to None (target has no string columns)
                    r[f"{rel['foreign_key']}_label"] = f"#{fk}"

    @staticmethod
    def _apply_filter(
        field: str,
        op: str,
        value: Any,
        where_clauses: list[str],
        params: dict[str, Any],
        column_types: dict[str, str] | None = None,
        *,
        qualifier: str | None = None,
    ) -> None:
        """把单个 filter 追加到 where + params（参数化，禁 raw f-string 值）

        gte/lte 在数值/日期列上用 CAST 强转，因为 query param 总是 string，
        PG 严格类型检查会拒绝 `integer >= text`。
        """
        param_key = f"filter_{field}_{op}"
        col_type = (column_types or {}).get(field, "")
        cast_sql = _cast_for_range_op(col_type)
        quoted_field = quote_identifier(field, label="filter field")
        column_sql = f"{qualifier}.{quoted_field}" if qualifier else quoted_field

        if op == "eq":
            where_clauses.append(f"{column_sql} = :{param_key}")
            params[param_key] = value
        elif op == "contains":
            where_clauses.append(f"{column_sql} ILIKE :{param_key}")
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
            where_clauses.append(f"{column_sql} IN ({', '.join(placeholders)})")
        elif op == "gte":
            placeholder = (
                f"CAST(:{param_key} AS {cast_sql})" if cast_sql else f":{param_key}"
            )
            where_clauses.append(f"{column_sql} >= {placeholder}")
            params[param_key] = value
        elif op == "lte":
            placeholder = (
                f"CAST(:{param_key} AS {cast_sql})" if cast_sql else f":{param_key}"
            )
            where_clauses.append(f"{column_sql} <= {placeholder}")
            params[param_key] = value
        elif op == "has":
            # JSONB array contains: column ? value (PG jsonb ? operator)
            where_clauses.append(
                f"cast({column_sql} as jsonb) ? cast(:{param_key} as text)"
            )
            params[param_key] = str(value)

    async def get(
        self,
        db: AsyncSession,
        *,
        table_name: str,
        record_id: int,
        tenant: TenantContext,
    ) -> dict:
        require_marketplace_capability(tenant)
        quoted_table = quote_table_name(table_name)
        sql = text(
            f'SELECT * FROM {quoted_table} WHERE "id" = :id '
            f'AND "tenant_id" = :tenant_id'
        )
        result = await db.execute(sql, {"id": record_id, "tenant_id": tenant.tenant_id})
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
        user_id: int,
        tenant: TenantContext,
        data_schema: dict | None = None,
    ) -> dict:
        require_marketplace_capability(tenant)
        quoted_table = quote_table_name(table_name)
        column_types = await _validate_payload_fields(
            db,
            table_name=table_name,
            data=data,
            data_schema=data_schema,
        )
        clean_data = dict(data)
        clean_data["updated_at"] = datetime.now(UTC)
        clean_data["updated_by"] = user_id

        if not clean_data:
            raise InvalidParameterException("没有可更新的字段")

        # Coerce numeric strings (e.g., Snowflake FK IDs from frontend) → int
        coerced = _coerce_numeric_strings(clean_data, column_types)
        # JSONB 列需 json.dumps
        bound_data = {k: _serialize_for_bind(v) for k, v in coerced.items()}

        set_parts = [
            f"{quote_identifier(key, label='update column')} = :{key}"
            for key in bound_data
        ]
        set_sql = ", ".join(set_parts)

        sql = text(
            f"UPDATE {quoted_table} SET {set_sql} "
            f'WHERE "id" = :record_id AND "tenant_id" = :tenant_id RETURNING *'
        )
        params = {
            **bound_data,
            "record_id": record_id,
            "tenant_id": tenant.tenant_id,
        }
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
        tenant: TenantContext,
    ) -> None:
        require_marketplace_capability(tenant)
        quoted_table = quote_table_name(table_name)
        sql = text(
            f'DELETE FROM {quoted_table} WHERE "id" = :id AND "tenant_id" = :tenant_id'
        )
        result = await db.execute(sql, {"id": record_id, "tenant_id": tenant.tenant_id})
        if result.rowcount == 0:
            raise NotFoundException(resource_type="记录")


data_api_service = DataApiService()
