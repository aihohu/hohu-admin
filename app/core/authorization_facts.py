"""Fixed factual mutation surface. Identity and lineage bindings are immutable."""

from types import MappingProxyType

from app.core.authorization_plan import ResourceKind as K

FACT_COLUMNS = MappingProxyType(
    {
        K.JOB: frozenset({"cron_expression", "update_by"}),
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
