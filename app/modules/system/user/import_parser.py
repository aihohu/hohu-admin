"""ImportParser Excel/CSV 解析 + 字段校验（v2.2）。

Task 0a 占位：业务在 Task 8 落地，含：
- parse_excel(file_bytes) -> list[UserImportRecord]
- parse_csv(file_bytes) -> list[UserImportRecord]
- 字段级格式校验（必填 / 长度 / 邮箱 / 手机号）
- 行数硬上限 USER_IMPORT_MAX_ROWS=2000（#2.10）

模板下载在 Task 14（HTTP /system/user/import/template）。
"""
