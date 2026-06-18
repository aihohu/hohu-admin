"""开发者中心 API：上传应用包 / 我的应用列表"""

import json
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import ResponseModel
from app.core.exceptions import AuthorizationException
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.models import App
from app.modules.marketplace.schemas.app import AppOut, VersionOut
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.permission_service import permission_service
from app.modules.marketplace.service.review_service import review_service
from app.modules.marketplace.service.upload_service import upload_service
from app.modules.marketplace.service.version_service import version_service
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
    # 1. 解析 manifest
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as e:
        raise AppInvalidManifestException(f"manifest JSON 解析失败：{e}")

    # 2. 校验 manifest（slug 正则 + required+default）
    version_service.validate_manifest(manifest)

    # 3. 上传文件（SHA-256 + zip 校验）
    content = await file.read()
    upload_result = await upload_service.save(
        file_obj=BytesIO(content),
        filename=file.filename or f"{manifest['slug']}-{manifest['version']}.zip",
        slug=manifest["slug"],
        version=manifest["version"],
    )

    # 4. 查或建 app 记录
    existing = await db.execute(select(App).where(App.slug == manifest["slug"]))
    app = existing.scalar_one_or_none()
    if app is None:
        # 首次上传：新建 app
        app = await app_service.create(
            db,
            name=manifest["name"],
            slug=manifest["slug"],
            type=manifest["type"],
            category=manifest["category"],
            description=manifest.get("description"),
            author_id=current_user.user_id,
            author_name=current_user.username,
            homepage=manifest.get("homepage"),
            license=manifest.get("license"),
            status="reviewing",
        )
    else:
        # 已有 app：检查权限（必须是 owner）
        if app.author_id != current_user.user_id:
            raise AuthorizationException("无权上传到他人应用")

    # 5. 同步 tags_text（用于搜索）
    tags = manifest.get("marketplace", {}).get("tags", [])
    app.tags_text = " ".join(tags) if tags else None

    # 6. 创建 version 记录
    version = await version_service.create(
        db,
        app_id=app.id,
        version=manifest["version"],
        manifest=manifest,
        file_url=upload_result["file_url"],
        file_hash=upload_result["file_hash"],
        file_size=upload_result["file_size"],
        changelog=manifest.get("marketplace", {}).get("changelog"),
    )

    # 7. 同步权限声明
    permissions = manifest.get("permissions", [])
    if permissions:
        await permission_service.bulk_insert(db, app_id=app.id, permissions=permissions)

    # 8. 创建 review 记录（pending）
    await review_service.create_pending(
        db,
        app_id=app.id,
        version_id=version.id,
        rule_check_result={
            "manifest_valid": True,
            "file_size": upload_result["file_size"],
        },
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
