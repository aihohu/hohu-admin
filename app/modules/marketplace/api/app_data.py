"""[LOCAL-ONLY] 动态数据 CRUD API

部署在本地 HoHu，操作 app_data_* 业务表。云市场不接触用户数据。
如按云端与本地职责拆分，本接口归入 local/app_data.py。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：通用动态数据 API（spec 6.2）
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context, get_current_user
from app.modules.marketplace.capability import require_marketplace_http_capability
from app.modules.marketplace.exceptions import AppNotFoundException
from app.modules.marketplace.lowcode.data_api_service import DataApiService
from app.modules.marketplace.lowcode.schema_introspection import table_exists
from app.modules.marketplace.lowcode.type_mapping import make_table_name
from app.modules.marketplace.models import AppVersion
from app.modules.marketplace.service.app_service import app_service
from app.modules.system.models.user import User

router = APIRouter(dependencies=[Depends(require_marketplace_http_capability)])
_data_api = DataApiService()

# query_params 中保留给框架/分页/排序的键，不进入 filters dict
_RESERVED_QUERY_KEYS = {"current", "size", "order_by"}


async def _resolve_table_and_schema(
    db: AsyncSession, *, slug: str, model: str, tenant: TenantContext
) -> tuple[str, dict | None, dict]:
    """解析表名 + 该 model 的 data_schema + 全量 manifest

    Manifest returned so callers (e.g. list endpoint) can read sibling models
    + relations for belongs_to expansion (decision #79).
    """
    app_obj = await app_service.get_by_slug(db, slug=slug, tenant=tenant)
    if app_obj.current_version_id is None:
        raise AppNotFoundException(slug=f"{slug} (no published version)")
    version = await db.scalar(
        select(AppVersion).where(
            AppVersion.id == app_obj.current_version_id,
            AppVersion.app_id == app_obj.id,
        )
    )
    if version is None:
        raise AppNotFoundException(slug=slug)
    manifest = version.manifest or {}

    if model and model != "_":
        table_name = make_table_name(slug, model)
        models_arr = manifest.get("models") or []
        for m in models_arr:
            if m.get("key") == model:
                return table_name, m.get("data_schema"), manifest
        return table_name, None, manifest
    table_name = make_table_name(slug)
    return table_name, manifest.get("data_schema"), manifest


def _parse_filters(query_params) -> tuple[dict | None, str | None]:
    """从 Request.query_params 抽出 filters dict + order_by

    保留 `field__op` 语义（spec 6.2 / 决策 #75）；剔除 current/size/order_by。
    """
    filters: dict = {}
    order_by: str | None = None
    for key, value in query_params.multi_items():
        if key in _RESERVED_QUERY_KEYS:
            if key == "order_by":
                order_by = value
            continue
        filters[key] = value
    return (filters or None), order_by


@router.post("/{slug}/{model}", response_model=ResponseModel[dict], summary="创建记录")
async def create_record(
    slug: str,
    model: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    table_name, schema, _manifest = await _resolve_table_and_schema(
        db, slug=slug, model=model, tenant=tenant
    )
    if not await table_exists(db, table_name):
        raise AppNotFoundException(slug=f"table {table_name}")
    record = await _data_api.create(
        db,
        table_name=table_name,
        data=data,
        user_id=current_user.user_id,
        tenant=tenant,
        data_schema=schema,
    )
    await db.commit()
    return ResponseModel.success(data=record)


@router.get(
    "/{slug}/{model}",
    response_model=ResponseModel[PageResult[dict]],
    summary="分页查询（支持 ?field__op=value 过滤，spec 6.2 / 决策 #75）",
)
async def list_records(
    slug: str,
    model: str,
    request: Request,
    current: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    table_name, data_schema, manifest = await _resolve_table_and_schema(
        db, slug=slug, model=model, tenant=tenant
    )
    if not await table_exists(db, table_name):
        raise AppNotFoundException(slug=f"table {table_name}")
    filters, order_by = _parse_filters(request.query_params)
    result = await _data_api.list(
        db,
        table_name=table_name,
        current=current,
        size=size,
        filters=filters,
        tenant=tenant,
        order_by=order_by,
        slug=slug,
        data_schema=data_schema,
        models=manifest.get("models"),
    )
    return ResponseModel.success(data=result)


@router.get(
    "/{slug}/{model}/{record_id}",
    response_model=ResponseModel[dict],
    summary="获取单条",
)
async def get_record(
    slug: str,
    model: str,
    record_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    table_name, _schema, _manifest = await _resolve_table_and_schema(
        db, slug=slug, model=model, tenant=tenant
    )
    record = await _data_api.get(
        db, table_name=table_name, record_id=record_id, tenant=tenant
    )
    return ResponseModel.success(data=record)


@router.put(
    "/{slug}/{model}/{record_id}",
    response_model=ResponseModel[dict],
    summary="更新",
)
async def update_record(
    slug: str,
    model: str,
    record_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    table_name, _schema, _manifest = await _resolve_table_and_schema(
        db, slug=slug, model=model, tenant=tenant
    )
    record = await _data_api.update(
        db,
        table_name=table_name,
        record_id=record_id,
        data=data,
        user_id=current_user.user_id,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(data=record)


@router.delete(
    "/{slug}/{model}/{record_id}",
    response_model=ResponseModel[None],
    summary="删除",
)
async def delete_record(
    slug: str,
    model: str,
    record_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    table_name, _schema, _manifest = await _resolve_table_and_schema(
        db, slug=slug, model=model, tenant=tenant
    )
    await _data_api.delete(
        db, table_name=table_name, record_id=record_id, tenant=tenant
    )
    await db.commit()
    return ResponseModel.success()
