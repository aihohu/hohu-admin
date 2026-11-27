"""Explicit dormant builtins; ordinary function registration grants no capability."""

from app.modules.job.task_contracts import (
    ClosedTaskArgs,
    TaskContractRegistry,
    TaskSpec,
)
from app.modules.job.task_registry import TaskScope
from app.tasks.log_tasks import test_task
from app.tasks.user_cleanup_tasks import (
    clean_expired_export_tasks,
    clean_expired_import_batches,
    clean_expired_import_previews,
)


def builtin_task_contracts():
    return TaskContractRegistry(
        (
            TaskSpec(
                "test_task",
                "page.system.job.tasks.testTask",
                TaskScope.TENANT,
                ClosedTaskArgs,
                test_task,
                "tenant.no-data/v1",
            ),
            TaskSpec(
                "clean_expired_import_batches",
                "page.system.job.tasks.retireImports",
                TaskScope.PLATFORM,
                ClosedTaskArgs,
                clean_expired_import_batches,
                "platform.transfer-retention/v1",
            ),
            TaskSpec(
                "clean_expired_import_previews",
                "page.system.job.tasks.retirePreviews",
                TaskScope.PLATFORM,
                ClosedTaskArgs,
                clean_expired_import_previews,
                "platform.transfer-retention/v1",
            ),
            TaskSpec(
                "clean_expired_export_tasks",
                "page.system.job.tasks.retireExports",
                TaskScope.PLATFORM,
                ClosedTaskArgs,
                clean_expired_export_tasks,
                "platform.transfer-retention/v1",
            ),
        )
    )
