"""开发者中心 API：上传应用包 / 我的应用列表"""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.models import App
from app.modules.marketplace.schemas.app import AppOut, VersionOut
from app.modules.marketplace.service.developer_service import developer_service
from app.modules.system.models.user import User

router = APIRouter()


@router.post(
    "/upload",
    response_model=ResponseModel[VersionOut],
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
    version = await developer_service.submit_version(
        db,
        manifest_json=manifest_json,
        file_content=content,
        filename=file.filename or "upload.zip",
        user_id=current_user.user_id,
        username=current_user.username,
    )
    await db.commit()
    return ResponseModel.success(data=VersionOut.model_validate(version))


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
