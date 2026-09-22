"""JobCreate 调度配置校验下沉 schema：422 + 字段级 loc（前端内联依赖）。"""

import pytest
from pydantic import ValidationError

from app.modules.job.schemas.job import JobCreate


def _payload(**overrides):
    base = {"jobName": "t", "jobKey": "test_task", "cronExpression": None}
    base.update(overrides)
    return base


def test_cron_mode_requires_expression_field_error():
    with pytest.raises(ValidationError) as exc_info:
        JobCreate.model_validate(_payload())

    errors = exc_info.value.errors()
    assert errors, "expected a validation error"
    locs = [e["loc"] for e in errors]
    assert ("cronExpression",) in locs


def test_cron_mode_rejects_omitted_expression():
    with pytest.raises(ValidationError) as exc_info:
        JobCreate.model_validate({"jobName": "t", "jobKey": "test_task"})
    assert ("cron_expression",) in [e["loc"] for e in exc_info.value.errors()]


def test_interval_mode_allows_omitted_cron():
    job = JobCreate.model_validate(
        {
            "jobName": "t",
            "jobKey": "test_task",
            "triggerType": "interval",
            "intervalValue": 5,
            "intervalUnit": "minutes",
        }
    )
    assert job.cron_expression is None


def test_cron_mode_rejects_wrong_field_count():
    with pytest.raises(ValidationError) as exc_info:
        JobCreate.model_validate(_payload(cronExpression="* * *"))

    assert ("cronExpression",) in [e["loc"] for e in exc_info.value.errors()]


def test_cron_mode_rejects_unparsable_expression():
    with pytest.raises(ValidationError) as exc_info:
        JobCreate.model_validate(_payload(cronExpression="99 * * * *"))

    assert ("cronExpression",) in [e["loc"] for e in exc_info.value.errors()]


@pytest.mark.parametrize("expr", ["* * * * *", "0 0 * * 1", "0 * * * * *"])
def test_valid_cron_expressions_pass(expr):
    job = JobCreate.model_validate(_payload(cronExpression=expr))
    assert job.cron_expression == expr


def test_interval_mode_requires_value_and_unit():
    with pytest.raises(ValidationError) as exc_info:
        JobCreate.model_validate(_payload(triggerType="interval"))

    # default-value validation reports snake_case field names (regular
    # validation reports camelCase aliases); the frontend parser normalizes both.
    locs = [e["loc"] for e in exc_info.value.errors()]
    assert ("interval_value",) in locs
    assert ("interval_unit",) in locs


def test_interval_mode_rejects_unknown_unit():
    with pytest.raises(ValidationError) as exc_info:
        JobCreate.model_validate(
            _payload(triggerType="interval", intervalValue=5, intervalUnit="weeks")
        )

    # explicitly provided fields report camelCase aliases
    assert ("intervalUnit",) in [e["loc"] for e in exc_info.value.errors()]


def test_valid_interval_passes():
    job = JobCreate.model_validate(
        _payload(triggerType="interval", intervalValue=5, intervalUnit="minutes")
    )
    assert job.interval_unit == "minutes"
