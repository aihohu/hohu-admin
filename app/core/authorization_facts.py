"""Fixed factual mutation surface. Identity and lineage bindings are immutable."""

from types import MappingProxyType

from app.core.authorization_plan import ResourceKind as K

FACT_COLUMNS = MappingProxyType(
    {
        K.DICT_TYPE: frozenset({"dict_name", "dict_type", "status", "remark"}),
        K.DICT_DATA: frozenset(
            {
                "dict_sort",
                "dict_label",
                "dict_value",
                "dict_type",
                "css_class",
                "list_class",
                "is_default",
                "status",
            }
        ),
        K.DATA_SCOPE_DEMO: frozenset({"title", "content", "status"}),
        K.JOB: frozenset(
            {
                "job_name",
                "cron_expression",
                "trigger_type",
                "interval_value",
                "interval_unit",
                "job_args",
                "status",
                "concurrent",
                "timeout_seconds",
                "max_retries",
                "run_on_enable",
                "remark",
                "update_by",
            }
        ),
        K.JOB_LOG: frozenset(
            {"status", "error_msg", "end_time", "duration", "attempt_count"}
        ),
        K.CONVERSATION: frozenset(
            {
                "title",
                "status",
                "deleted_at",
                "agent_code",
                "model_name",
                "system_prompt",
                "trace_id",
            }
        ),
        K.SOURCE_MESSAGE: frozenset({"is_active", "routing_feedback"}),
        K.ASSISTANT_MESSAGE: frozenset(
            {
                "routing_feedback",
                "content",
                "parts",
                "tool_calls",
                "tokens_input",
                "tokens_output",
                "is_active",
                "tool_codes",
                "subject_refs",
                "subject_refs_hash",
                "data_scope_hash",
                "resolver_version",
                "projection_dependency_message_ids",
            }
        ),
        K.PREPARED_ACTION: frozenset(
            {
                "status",
                "row_version",
                "execution_owner",
                "execution_lease_expires_at",
                "approved_by",
                "approved_at",
                "finished_at",
                "duration_ms",
                "error_code",
                "result_data",
                "result_ui",
                "subject_refs",
                "subject_refs_hash",
                "tool_codes",
                "resolver_version",
                "data_scope_hash",
            }
        ),
        K.OPERATION_LOG: frozenset(
            {
                "status",
                "started_at",
                "finished_at",
                "duration_ms",
                "error_code",
                "result_summary",
                "target_summary",
                "approved_by",
                "hitl_wait_ms",
            }
        ),
        K.IMPORT_BATCH: frozenset(
            {
                "field_lineage",
                "status",
                "started_at",
                "finished_at",
                "success_count",
                "skipped_count",
                "overwritten_count",
                "failed_count",
                "failed_rows_file",
                "file_storage_key",
                "summary_new",
                "summary_exists",
                "summary_conflict",
                "summary_out_of_scope",
            }
        ),
        K.EXPORT_TASK: frozenset(
            {
                "status",
                "file_storage_key",
                "file_size_bytes",
                "row_count",
                "error_code",
                "error_message",
                "started_at",
                "finished_at",
                "duration_ms",
            }
        ),
    }
)

CONTEXT_CHILDREN = frozenset(
    {K.SOURCE_MESSAGE, K.ASSISTANT_MESSAGE, K.PREPARED_ACTION, K.OPERATION_LOG}
)
FILE_COMPLETIONS = frozenset(
    {
        K.ASSISTANT_MESSAGE,
        K.PREPARED_ACTION,
        K.OPERATION_LOG,
        K.IMPORT_BATCH,
        K.EXPORT_TASK,
    }
)
