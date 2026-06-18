"""应用版本 service（spec 7.1 + 6.3 + 13.2）。

职责：
1. validate_manifest：纯函数校验 manifest（slug 正则、type/category 白名单、semver、
   required 字段必须有字面常量 default —— 防止 PG ADD COLUMN NOT NULL 触发全表重写）。
2. CRUD on AppVersion（create / get_by_version / get_latest_approved）。

AppVersion 表通过 app_id FK 隐式继承 tenant（无独立 tenant_id 列），
因此不继承 MarketplaceBaseService（其 scoped() 假设 model 有 tenant_id）。
版本查询通过 app_id 关联，自动跟随所属 App 的 tenant。
"""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.marketplace.exceptions import (
    AppInvalidManifestException,
    AppNotFoundException,
)
from app.modules.marketplace.models import AppVersion

# spec 7.1：slug 必须匹配（首字符字母，末字符非连字符）
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,148}[a-z0-9]$")

# 禁止的 dynamic default 模式（spec 决策 #69）：
# - 圆括号表达式：NOW() / RANDOM() / uuid_generate_v4()
# - Jinja 模板：{{ ... }}
# - SQL 标识符：CURRENT_*
# 大小写不敏感
DYNAMIC_DEFAULT_PATTERNS = re.compile(
    r"(\(|\)|\{\{|\}\}|NOW\(|UUID|CURRENT_|RANDOM\(|RAND\()",
    re.IGNORECASE,
)

VALID_TYPES = {"lowcode", "frontend", "backend", "fullstack", "theme", "bundle"}
VALID_CATEGORIES = {
    "business",
    "tool",
    "analytics",
    "ai-agent",
    "ai-skill",
    "mcp-adapter",
    "integration",
    "theme",
}

# semver 简单校验：x.y.z 起步（允许预发布后缀，如 1.0.0-rc.1）
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+")


class VersionService:
    """应用版本 service（spec 7.1 + 6.3 + 13.2）

    AppVersion 无 tenant_id 字段（通过 app_id FK 隐式继承 tenant），
    所以查询不走 self.scoped()，直接走 app_id 过滤。
    """

    def validate_manifest(self, manifest: dict) -> None:
        """校验 manifest 格式（spec 13.2 第 1 层规则检查的核心部分）。

        Args:
            manifest: 应用清单字典（含 name/slug/version/type/category 等）

        Raises:
            AppInvalidManifestException: 任一规则不满足
        """
        # 必填字段
        required_fields = ["name", "slug", "version", "type", "category"]
        for f in required_fields:
            if f not in manifest:
                raise AppInvalidManifestException(f"缺少必填字段：{f}")

        # slug 正则（spec 7.1）
        slug = manifest["slug"]
        if not SLUG_PATTERN.match(slug):
            raise AppInvalidManifestException(
                f"slug '{slug}' 不匹配 {SLUG_PATTERN.pattern}"
                "（首字符必须字母，末字符不能是连字符）"
            )

        # type / category 合法值
        if manifest["type"] not in VALID_TYPES:
            raise AppInvalidManifestException(
                f"type 必须是 {sorted(VALID_TYPES)}，当前：{manifest['type']}"
            )
        if manifest["category"] not in VALID_CATEGORIES:
            raise AppInvalidManifestException(
                f"category 必须是 {sorted(VALID_CATEGORIES)}，"
                f"当前：{manifest['category']}"
            )

        # version semver 简单校验
        if not SEMVER_PATTERN.match(manifest["version"]):
            raise AppInvalidManifestException(
                f"version '{manifest['version']}' 不是合法 semver"
            )

        # data_schema 校验：required 字段必须有字面常量 default（spec 6.3 + 决策 #69）
        data_schema = manifest.get("data_schema") or {}
        if isinstance(data_schema, dict):
            self._validate_data_schema_defaults(data_schema)

    def _validate_data_schema_defaults(self, data_schema: dict) -> None:
        """spec 6.3：新增 required 字段必须有字面常量 default（防 PG 全表重写）。

        Args:
            data_schema: manifest.data_schema，期望符合 JSON Schema 结构
                {properties: {...}, required: [...]}

        Raises:
            AppInvalidManifestException: required 字段缺 default，或 default 是动态表达式
        """
        required = set(data_schema.get("required", []))
        properties = data_schema.get("properties", {})
        for field_name in required:
            field_def = properties.get(field_name, {})
            if "default" not in field_def:
                raise AppInvalidManifestException(
                    f"required 字段 '{field_name}' 必须声明 default 值（spec 6.3）"
                )
            default_val = field_def["default"]
            # 字面常量校验：必须是 string/number/boolean
            # （不允许 null，PG ADD COLUMN NOT NULL 无意义）
            if not isinstance(default_val, (str, int, float, bool)):
                raise AppInvalidManifestException(
                    f"字段 '{field_name}' 的 default 必须是字面常量"
                    "（string/number/boolean），"
                    f"当前类型：{type(default_val).__name__}"
                )
            # 字符串 default 不能含动态表达式
            if isinstance(default_val, str) and DYNAMIC_DEFAULT_PATTERNS.search(
                default_val
            ):
                raise AppInvalidManifestException(
                    f"字段 '{field_name}' 的 default 不能含动态表达式"
                    "（NOW()/uuid() 等），"
                    f"当前：'{default_val}'"
                )

    async def create(
        self,
        db: AsyncSession,
        *,
        app_id: int,
        version: str,
        manifest: dict,
        file_url: str,
        file_hash: str,
        file_size: int | None = None,
        changelog: str | None = None,
    ) -> AppVersion:
        """创建版本记录。

        前置条件：调用方应已通过 validate_manifest（本 service 不自动校验，
        避免同一 manifest 被多次校验浪费 CPU）。

        Args:
            db: 数据库会话（调用方负责 commit）
            app_id: 所属应用 ID
            version: semver 版本号
            manifest: 已校验的 manifest
            file_url: 制品包 URL（来自 UploadService）
            file_hash: 制品包 SHA-256（来自 UploadService）
            file_size: 制品包字节数（来自 UploadService）
            changelog: 变更说明（可选）

        Returns:
            已 flush 拿到 id 的 AppVersion 实例
        """
        version_record = AppVersion(
            app_id=app_id,
            version=version,
            manifest=manifest,
            file_url=file_url,
            file_hash=file_hash,
            file_size=file_size,
            changelog=changelog,
            review_status="pending",
        )
        db.add(version_record)
        await db.flush()
        return version_record

    async def get_by_version(
        self, db: AsyncSession, *, app_id: int, version: str
    ) -> AppVersion:
        """按 (app_id, version) 唯一查询版本记录。

        Raises:
            AppNotFoundException: 该版本不存在
        """
        stmt = (
            select(AppVersion)
            .where(AppVersion.app_id == app_id)
            .where(AppVersion.version == version)
        )
        result = await db.execute(stmt)
        record = result.scalar_one_or_none()
        if record is None:
            # 简化处理：版本不存在时报"应用不存在"。
            # Phase 1 接口未细分版本不存在错误，沿用通用 NOT_FOUND。
            raise AppNotFoundException(app_id=app_id)
        return record

    async def get_latest_approved(
        self, db: AsyncSession, *, app_id: int
    ) -> AppVersion | None:
        """查最新 approved 版本（用于"安装最新版"接口）。

        Returns:
            最新 approved 的 AppVersion，或 None（无 approved 版本）
        """
        stmt = (
            select(AppVersion)
            .where(AppVersion.app_id == app_id)
            .where(AppVersion.review_status == "approved")
            .order_by(AppVersion.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()


version_service = VersionService()
