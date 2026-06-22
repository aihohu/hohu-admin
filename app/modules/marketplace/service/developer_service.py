"""开发者 service：编排应用上传的多个步骤（manifest 校验 + 文件保存 + 应用 upsert + 版本创建 + 权限同步 + 审核创建）"""

import json
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException
from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.models import App, AppVersion
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.permission_service import permission_service
from app.modules.marketplace.service.review_service import review_service
from app.modules.marketplace.service.upload_service import upload_service
from app.modules.marketplace.service.version_service import version_service


class DeveloperService:
    """开发者中心 service（spec 8.4）

    编排 upload 流程的多个 service 调用，保持 API 层薄。
    """

    async def submit_version(
        self,
        db: AsyncSession,
        *,
        manifest_json: str,
        file_content: bytes,
        filename: str,
        user_id: int,
        username: str,
    ) -> tuple[AppVersion, int]:
        """提交新版本：manifest 校验 → 上传 → app upsert → version create → permission sync → review create

        Raises:
            AppInvalidManifestException: manifest JSON 解析失败或校验不通过
            AuthorizationException: 上传到他人应用
        """
        # 1. 解析 manifest
        try:
            manifest = json.loads(manifest_json)
        except json.JSONDecodeError as e:
            raise AppInvalidManifestException(f"manifest JSON 解析失败：{e}")

        # 2. 校验 manifest（slug 正则 + required+default）
        version_service.validate_manifest(manifest)

        # 3. 上传文件（SHA-256 + zip 校验）
        upload_result = await upload_service.save(
            file_obj=BytesIO(file_content),
            filename=filename,
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
                author_id=user_id,
                author_name=username,
                homepage=manifest.get("homepage"),
                license=manifest.get("license"),
                status="reviewing",
            )
        else:
            # 已有 app：检查权限（必须是 owner）
            if app.author_id != user_id:
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
            await permission_service.bulk_insert(
                db, app_id=app.id, permissions=permissions
            )

        # 8. 创建 review 记录（pending）
        review = await review_service.create_pending(
            db,
            app_id=app.id,
            version_id=version.id,
            rule_check_result={
                "manifest_valid": True,
                "file_size": upload_result["file_size"],
            },
        )

        return version, review.id


developer_service = DeveloperService()
