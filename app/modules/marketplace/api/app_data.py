"""通用动态数据 API（spec 6.2）"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.exceptions import AppNotFoundException
from app.modules.marketplace.lowcode.data_api_service import DataApiService
from app.modules.marketplace.lowcode.schema_introspection import table_exists
from app.modules.marketplace.lowcode.type_mapping import make_table_name
from app.modules.marketplace.models import AppVersion
from app.modules.marketplace.service.app_service import app_service
from app.modules.system.models.user import User

router = APIRouter()
_data_api = DataApiService()


async def _resolve_table_and_schema(
    db: AsyncSession, *, slug: str, model: str
) -> tuple[str, dict | None]:
    """解析表名 + 该 model 的 data_schema"""
    app_obj = await app_service.get_by_slug(db, slug=slug)
    if app_obj.current_version_id is None:
        raise AppNotFoundException(slug=f"{slug} (no published version)")
    version = await db.get(AppVersion, app_obj.current_version_id)
    if version is None:
        raise AppNotFoundException(slug=slug)
    manifest = version.manifest or {}

    if model and model != "_":
        table_name = make_table_name(slug, model)
        models_arr = manifest.get("models") or []
        for m in models_arr:
            if m.get("key") == model:
                return table_name, m.get("data_schema")
        return table_name, None
    table_name = make_table_name(slug)
    return table_name, manifest.get("data_schema")


@router.post("/{slug}/{model}", response_model=ResponseModel[dict], summary="创建记录")
async def create_record(
    slug: str,
    model: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    table_name, schema = await _resolve_table_and_schema(db, slug=slug, model=model)
    if not await table_exists(db, table_name):
        raise AppNotFoundException(slug=f"table {table_name}")
    record = await _data_api.create(
        db,
        table_name=table_name,
        data=data,
        tenant_id=0,
        user_id=current_user.user_id,
        data_schema=schema,
    )
    await db.commit()
    return ResponseModel.success(data=record)


@router.get(
    "/{slug}/{model}",
    response_model=ResponseModel[PageResult[dict]],
    summary="分页查询",
)
async def list_records(
    slug: str,
    model: str,
    current: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
):
    table_name, _ = await _resolve_table_and_schema(db, slug=slug, model=model)
    if not await table_exists(db, table_name):
        raise AppNotFoundException(slug=f"table {table_name}")
    result = await _data_api.list(
        db,
        table_name=table_name,
        current=current,
        size=size,
        filters=None,
        tenant_id=0,
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
):
    table_name, _ = await _resolve_table_and_schema(db, slug=slug, model=model)
    record = await _data_api.get(
        db, table_name=table_name, record_id=record_id, tenant_id=0
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
):
    table_name, _ = await _resolve_table_and_schema(db, slug=slug, model=model)
    record = await _data_api.update(
        db,
        table_name=table_name,
        record_id=record_id,
        data=data,
        tenant_id=0,
        user_id=current_user.user_id,
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
):
    table_name, _ = await _resolve_table_and_schema(db, slug=slug, model=model)
    await _data_api.delete(db, table_name=table_name, record_id=record_id, tenant_id=0)
    await db.commit()
    return ResponseModel.success()
