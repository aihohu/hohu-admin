"""用户导入导出 Pydantic schemas（v2.2 P0/P1）。

Task 0a 占位：schemas 在 Task 1 落地，含：
- UserImportRecord（含 dept_input / role_input / employee_no）
- ImportDryRunResult（含 *_truncated + *_records_file，v2.2 P1 #3.2）
- ImportResult（含 failed_rows_preview / failed_rows_file / idempotent_replay，v2.2 P1 #3.3）
- FailedRow
- UserImportBatch（v2.2 P1-2：原 ImportPreviewSession 已合并）
- UserExportTask（v2.2 P1-5 #2.31）
"""
