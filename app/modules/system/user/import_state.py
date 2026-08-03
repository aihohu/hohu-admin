"""ImportState 状态机 + CAS helper + 清理 cron（v2.2 P0/P1）。

Task 0a 占位：业务在 Task 0b / 22 落地，含：
- ImportBatchStatus(Enum)：CREATED / PREVIEW_DONE / RUNNING / SUCCESS / PARTIAL_SUCCESS
  / FAILED / EXPIRED / CANCELLED（v2.2 P1-2 加 PREVIEW_DONE）
- EmployeeNoSyncMode(Enum)：CREATE_ONLY / UPDATE_PROFILE / FULL_SYNC，#2.24
- ExportTaskStatus(Enum)：CREATED / RUNNING / SUCCESS / FAILED / EXPIRED，#2.31
- LEGAL_TRANSITIONS 映射（含 CREATED → PREVIEW_DONE / FAILED 分支）
- _transition_batch_status(db, batch_id, from_status, to_status) CAS helper
  v2.2 P0 #2.27：UPDATE ... WHERE status=from_status，0 行匹配视为并发冲突
- cleanup_expired_batches cron（每日 02:00，90 天）
- cleanup_expired_previews cron（每小时，PREVIEW_DONE 超 10min → EXPIRED）
- cleanup_expired_export_tasks cron（每日 02:30，30 天）
"""
