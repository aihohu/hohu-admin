"""ImportService 主流程（v2.2 P0/P1）。

Task 0a 占位：业务在 Task 8/9/10 落地，含：
- parse(db, file_bytes, mime_type) — Excel/CSV 解析入口（委派给 import_parser）
- dry_run(db, records, current_user, file_bytes, filename) — 三端共用预检
  v2.2 P0 #2.19：业务数据 INSERT sys_user_import_batch + Redis cache only
  v2.2 P1 #3.2：records 截断 + 写 *_records_file
- execute(db, records, *, preview_token, current_user, on_conflict, sync_mode, reason)
  v2.2 P0 #2.27：CAS WHERE status='CREATED' 幂等保护
  chunk 100 + savepoint + IntegrityError 区分
  v2.2 P1：写 batch_log + 响应精简 failed_rows

子模块对外不导出，只通过 user_service facade 暴露（#2.2 反例 3）。
"""
