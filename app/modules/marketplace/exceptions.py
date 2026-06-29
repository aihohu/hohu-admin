"""应用市场异常错误码常量（spec 19.2）

所有错误码统一 APP_* 前缀。继承 hohu-admin BusinessException 体系。
"""

from app.core.exceptions import (
    BusinessException,
    DuplicateException,
    NotFoundException,
)


class AppErrorCode:
    """应用市场错误码常量（spec 19.2）"""

    NOT_FOUND = "APP_NOT_FOUND"
    VERSION_NOT_FOUND = "APP_VERSION_NOT_FOUND"
    DUPLICATE_SLUG = "APP_DUPLICATE_SLUG"
    INVALID_MANIFEST = "APP_INVALID_MANIFEST"
    REVIEW_REJECTED = "APP_REVIEW_REJECTED"
    DEPENDENCY_MISSING = "APP_DEPENDENCY_MISSING"
    INSTALL_LOCKED = "APP_INSTALL_LOCKED"
    SCHEMA_BREAKING_CHANGE = "APP_SCHEMA_BREAKING_CHANGE"
    PERMISSION_DENIED = "APP_PERMISSION_DENIED"
    INVALID_PATH_VAR = "APP_PATH_VAR_INVALID"
    UNSUPPORTED_FILE_TYPE = "APP_UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "APP_FILE_TOO_LARGE"
    RATING_DUPLICATE = "APP_RATING_DUPLICATE"
    RATING_NOT_FOUND = "APP_RATING_NOT_FOUND"
    # Filter API（spec 6.2 / 决策 #75 #76）
    FILTER_SYSTEM_FIELD_FORBIDDEN = "APP_FILTER_SYSTEM_FIELD_FORBIDDEN"
    FILTER_UNKNOWN_FIELD = "APP_FILTER_UNKNOWN_FIELD"
    FILTER_INVALID_OPERATOR = "APP_FILTER_INVALID_OPERATOR"
    FILTER_OP_TYPE_MISMATCH = "APP_FILTER_OP_TYPE_MISMATCH"


class AppNotFoundException(NotFoundException):
    """应用不存在"""

    def __init__(self, slug: str | None = None, app_id: int | None = None):
        resource = f"应用 slug={slug}" if slug else f"应用 id={app_id}"
        super().__init__(resource_type=resource, error_code=AppErrorCode.NOT_FOUND)


class AppDuplicateSlugException(DuplicateException):
    """应用 slug 重复"""

    def __init__(self, slug: str):
        super().__init__(
            field="slug", value=slug, error_code=AppErrorCode.DUPLICATE_SLUG
        )


class AppInstallLockedException(BusinessException):
    """应用正在被其他进程安装/卸载"""

    def __init__(self, app_id: int):
        super().__init__(
            code=409,
            message=f"应用 {app_id} 正在被其他进程安装/卸载，请稍后",
            error_code=AppErrorCode.INSTALL_LOCKED,
        )


class AppInvalidManifestException(BusinessException):
    """manifest 校验失败"""

    def __init__(self, reason: str):
        super().__init__(
            code=400,
            message=f"manifest 校验失败：{reason}",
            error_code=AppErrorCode.INVALID_MANIFEST,
        )
