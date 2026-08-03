"""用户模块常量（v2.2 P0/P1）。

Task 0a 占位：常量在 Task 0c 落地，含：
- USER_IMPORT_MAX_ROWS = 2000 (#2.10)
- USER_EXPORT_ASYNC_THRESHOLD = 5000 (#2.10)
- MAX_PREVIEW_RECORDS = 2000 (#3.2)
- OVERWRITE_NEVER / OVERWRITE_ALLOWED (#3.x)
- EXPORT_ALLOWED_FIELDS

故意不进 settings，避免部署方误改导致安全边界被绕过。
"""
