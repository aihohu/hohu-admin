"""JobAiUpdate schema 白名单单测 — spec §11.3

验证 AI 入口更新请求的字段级硬约束。即使调用方传入危险字段，
Pydantic 在反序列化阶段就丢弃。
"""

# ruff: noqa: ARG001

import pytest
from pydantic import ValidationError

from app.modules.job.schemas.job import JobAiUpdate


class TestJobAiUpdateWhitelist:
    """spec §11.3: AI 入口只允许调度配置 + 启停 + 任务参数"""

    def test_clean_cron_update_accepted(self) -> None:
        """正常 cron 表达式更新被接受"""
        data = JobAiUpdate(job_id=1, cron_expression="*/5 * * * *")
        dumped = data.model_dump(exclude_unset=True)
        assert dumped == {"job_id": 1, "cron_expression": "*/5 * * * *"}

    def test_status_update_accepted(self) -> None:
        """启停（spec enabled）被接受"""
        data = JobAiUpdate(job_id=1, status="2")  # 停用
        assert data.status == "2"

    def test_name_update_accepted(self) -> None:
        """任务名（spec name）被接受"""
        data = JobAiUpdate(job_id=1, job_name="新任务名")
        assert data.job_name == "新任务名"

    def test_job_args_accepted(self) -> None:
        """任务参数（spec params）被接受"""
        data = JobAiUpdate(job_id=1, job_args='{"key":"value"}')
        assert data.job_args == '{"key":"value"}'

    def test_trigger_type_accepted(self) -> None:
        data = JobAiUpdate(job_id=1, trigger_type="interval")
        assert data.trigger_type == "interval"


class TestJobAiUpdateForbiddenFieldsDropped:
    """spec §11.3 禁止字段：调用方传过来时 Pydantic 自动丢弃（不报错）"""

    def test_job_key_dropped(self) -> None:
        """job_key 改了等同于改执行哪个 Python 代码（等同改 code），禁止"""
        data = JobAiUpdate.model_validate(
            {"job_id": 1, "job_key": "evil.task", "cron_expression": "* * * * *"}
        )
        # job_key 字段不在 schema 里，被丢弃
        assert not hasattr(data, "job_key")
        assert data.cron_expression == "* * * * *"

    def test_run_on_enable_dropped(self) -> None:
        """run_on_enable（手动触发等价物）禁止"""
        data = JobAiUpdate.model_validate(
            {"job_id": 1, "run_on_enable": True, "status": "1"}
        )
        assert not hasattr(data, "run_on_enable")
        assert data.status == "1"

    def test_timeout_seconds_dropped(self) -> None:
        """运维参数（timeout）禁止 AI 改"""
        data = JobAiUpdate.model_validate(
            {"job_id": 1, "timeout_seconds": 1, "cron_expression": "*/5 * * * *"}
        )
        assert not hasattr(data, "timeout_seconds")

    def test_max_retries_dropped(self) -> None:
        """运维参数（重试次数）禁止 AI 改"""
        data = JobAiUpdate.model_validate(
            {"job_id": 1, "max_retries": 99, "cron_expression": "*/5 * * * *"}
        )
        assert not hasattr(data, "max_retries")

    def test_concurrent_dropped(self) -> None:
        """运维参数（并发策略）禁止 AI 改"""
        data = JobAiUpdate.model_validate(
            {"job_id": 1, "concurrent": "1", "cron_expression": "*/5 * * * *"}
        )
        assert not hasattr(data, "concurrent")

    def test_unknown_field_dropped(self) -> None:
        """任意未知字段（包括未来 spec 加的 code/module_path/func_name）被丢弃"""
        data = JobAiUpdate.model_validate(
            {
                "job_id": 1,
                "code": "import os; os.system('rm -rf /')",
                "module_path": "evil.module",
                "func_name": "pwn",
                "cron_expression": "*/5 * * * *",
            }
        )
        dumped = data.model_dump(exclude_unset=True)
        assert "code" not in dumped
        assert "module_path" not in dumped
        assert "func_name" not in dumped
        assert dumped["cron_expression"] == "*/5 * * * *"

    def test_camel_case_alias_also_drops(self) -> None:
        """前端 / LLM 用 camelCase 别名传也走相同白名单"""
        data = JobAiUpdate.model_validate(
            {"jobId": 1, "jobKey": "evil", "cronExpression": "*/5 * * * *"}
        )
        assert not hasattr(data, "job_key")
        assert data.job_id == 1
        assert data.cron_expression == "*/5 * * * *"


class TestJobAiUpdateValidation:
    """状态字段 validator"""

    def test_invalid_status_rejected(self) -> None:
        """status 必须是 '1' / '2'，其他拒"""
        with pytest.raises(ValidationError):
            JobAiUpdate(job_id=1, status="3")

    def test_job_id_required(self) -> None:
        with pytest.raises(ValidationError):
            JobAiUpdate(cron_expression="* * * * *")

    def test_empty_payload_allowed(self) -> None:
        """空 payload（只含 job_id）合法 — 用于 enable/disable 等纯状态变更"""
        data = JobAiUpdate(job_id=1)
        assert data.job_id == 1
        assert data.cron_expression is None


class TestFullWhitelistCoverage:
    """spec §11.3 表格逐项验证：所有允许字段在 schema 里，所有禁止字段不在"""

    # spec §11.3 允许字段
    ALLOWED_FIELDS = {
        "job_name",  # spec name
        "cron_expression",
        "status",  # spec enabled
        "job_args",  # spec params
    }
    # spec §11.3 禁止字段 + 我额外加的运维字段
    FORBIDDEN_FIELDS = {
        "job_key",  # 改代码标识
        "run_on_enable",  # 手动触发等价物（spec run_now）
        "code",
        "module_path",
        "func_name",
        "timeout_seconds",
        "max_retries",
        "concurrent",
    }

    def test_all_allowed_fields_in_schema(self) -> None:
        fields = set(JobAiUpdate.model_fields.keys())
        for allowed in self.ALLOWED_FIELDS:
            assert allowed in fields, f"允许字段 {allowed} 不在 JobAiUpdate schema"

    def test_all_forbidden_fields_not_in_schema(self) -> None:
        fields = set(JobAiUpdate.model_fields.keys())
        for forbidden in self.FORBIDDEN_FIELDS:
            assert forbidden not in fields, (
                f"禁止字段 {forbidden} 不应在 JobAiUpdate schema"
            )
