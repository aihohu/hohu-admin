"""ExportService 导出（v2.2 P0/P1）。

Task 0a 占位：业务在 Task 11/13 落地，含：
- export(db, filter, current_user, reason) — 同步导出（< 5000 行）+ 强制建 ExportTask
  v2.2 P1-5 #2.31：filter_snapshot 冻结当时的 accessible_dept_ids（防异步导出越权）
  v2.2 P1-3 #2.30：reason 必填（审计可追溯）
  30 天 TTL（cron 在 import_state.cleanup_expired_export_tasks 清理）
- get_export_task(db, export_id, operator_id) — 任务详情反查
- list_export_tasks(db, query, current_user) — 任务列表分页

异步导出（> USER_EXPORT_ASYNC_THRESHOLD）推迟到 Phase 3。
"""
