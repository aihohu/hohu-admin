"""Built-in setting definitions. Labels belong to client language catalogs."""

from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    kind: str = "text"
    default: object = ""
    public: bool = False
    minimum: int | None = None
    maximum: int | None = None
    options: tuple[str, ...] = ()


# Single source for the selectable upload extension universe; clients render
# field options from metadata instead of keeping a parallel hardcoded list.
# Active content (html/svg/js) and executables stay excluded on purpose.
UPLOAD_EXTENSION_UNIVERSE = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".md",
    ".xls",
    ".xlsx",
    ".txt",
    ".csv",
    ".json",
    ".xml",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".mp3",
    ".wav",
    ".mp4",
    ".mov",
)


SETTINGS = {
    definition.key: definition
    for definition in (
        Setting("ai:enabled_tools", "ai", default="[]"),
        Setting("site_name", "brand", default="后台管理系统", public=True),
        Setting("site_logo", "brand", public=True),
        Setting("site_description", "brand", default="通用后台管理系统", public=True),
        Setting("site_icp", "brand", public=True),
        Setting("site_copyright", "brand", default="Copyright © 2026", public=True),
        Setting("user_agreement", "agreements", public=True),
        Setting("privacy_policy", "agreements", public=True),
        Setting("default_avatar", "account"),
        Setting("register_enabled", "account", "boolean", "true"),
        Setting("user_require_primary_dept", "account", "boolean", "false"),
    )
}
SETTINGS.update(
    {
        s.key: s
        for s in (
            Setting("auth:default_password", "account", "secret"),
            Setting(
                "default_locale",
                "locale",
                "select",
                settings.DEFAULT_LOCALE,
                True,
                options=("zh-CN", "en-US"),
            ),
            Setting(
                "upload:max_bytes",
                "files",
                "integer",
                10 * 1024 * 1024,
                minimum=1024,
                maximum=settings.UPLOAD_HARD_MAX_BYTES,
            ),
            Setting(
                "upload:image_max_bytes",
                "files",
                "integer",
                10 * 1024 * 1024,
                minimum=1024,
                maximum=settings.UPLOAD_HARD_MAX_BYTES,
            ),
            Setting(
                "upload:import_max_bytes",
                "files",
                "integer",
                10 * 1024 * 1024,
                minimum=1024,
                maximum=10 * 1024 * 1024,
            ),
            Setting(
                "upload:allowed_extensions",
                "files",
                "extensions",
                ",".join(UPLOAD_EXTENSION_UNIVERSE),
                options=UPLOAD_EXTENSION_UNIVERSE,
            ),
            Setting(
                "security:login_per_minute",
                "security",
                "integer",
                5,
                minimum=1,
                maximum=100,
            ),
            Setting(
                "security:register_per_minute",
                "security",
                "integer",
                3,
                minimum=1,
                maximum=30,
            ),
            Setting(
                "security:api_per_minute",
                "security",
                "integer",
                100,
                minimum=10,
                maximum=10000,
            ),
        )
    }
)
BUILTIN_KEYS = frozenset(SETTINGS)
SETTING_GROUPS = ("brand", "account", "locale", "files", "agreements", "security")
ALLOWED_UPLOAD_EXTENSIONS = frozenset(UPLOAD_EXTENSION_UNIVERSE)


def is_builtin_key(key: str) -> bool:
    # AI policy keys are controlled by their domain, never arbitrary parameters.
    return key in BUILTIN_KEYS or key.startswith(("ai:", "security:", "upload:"))
