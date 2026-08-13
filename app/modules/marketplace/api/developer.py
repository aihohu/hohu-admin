"""[CLOUD-ONLY] 开发者上传 / 我的应用列表

部署在云市场。本地 HoHu 不挂此 router——本地开发走 /marketplace/local/upload。
如按云端与本地职责拆分，本接口归入 cloud/developer.py。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：开发者中心 API：上传应用包 / 我的应用列表
"""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.models import App
from app.modules.marketplace.schemas.app import AppOut, VersionOut, VersionUploadOut
from app.modules.marketplace.service.developer_service import developer_service
from app.modules.system.models.user import User

router = APIRouter()


@router.post(
    "/upload",
    response_model=ResponseModel[VersionUploadOut],
    summary="上传应用包（创建新版本）",
    dependencies=[Depends(require_permissions("marketplace:develop"))],
)
async def upload_app(
    file: UploadFile = File(...),
    manifest_json: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content = await file.read()
    version, review_id = await developer_service.submit_version(
        db,
        manifest_json=manifest_json,
        file_content=content,
        filename=file.filename or "upload.zip",
        user_id=current_user.user_id,
        username=current_user.user_name,
    )
    await db.commit()
    data = VersionOut.model_validate(version).model_dump()
    data["review_id"] = review_id
    return ResponseModel.success(data=data)


@router.get(
    "/my-apps",
    response_model=ResponseModel[list[AppOut]],
    summary="我的应用",
    dependencies=[Depends(require_permissions("marketplace:develop"))],
)
async def my_apps(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        select(App)
        .where(App.author_id == current_user.user_id)
        .order_by(App.created_at.desc())
    )
    result = await db.execute(stmt)
    return ResponseModel.success(
        data=[AppOut.model_validate(a) for a in result.scalars().all()]
    )
