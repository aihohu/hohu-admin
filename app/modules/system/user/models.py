"""用户导入导出 ORM（v2.2 P0/P1）。

Task 0a 占位：ORM 在 Task 2 落地，含：
- UserImportBatch（sys_user_import_batch，PostgreSQL ENUM status + CHECK，#2.26）
- UserImportBatchLog（sys_user_import_batch_log，FK CASCADE，#2.28）
- UserExportTask（sys_user_export_task，v2.2 P1-5 #2.31）
- sys_user.employee_no 字段（UNIQUE 索引）

User ORM 本身保留在 app.modules.system.models.user（不在本子包）。
"""
