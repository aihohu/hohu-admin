"""Explicit task metadata is a schema contract, never a claim or a user grant."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import BusinessException
from app.modules.job.builtin_task_specs import builtin_task_contracts
from app.modules.job.task_contracts import (
    ClosedTaskArgs,
    PreparedTaskArgs,
    TaskContractRegistry,
    TaskSpec,
    prepare_task_args,
)
from app.modules.job.task_registry import TaskScope, get_task_function


class NumberArgs(ClosedTaskArgs):
    count: int = 1
    label: str = "label"


async def number_task(_args=None):
    """This function's docstring must not become a public task label."""
    return _args


def spec(**changes):
    return TaskSpec(
        **(
            {
                "key": "test_number",
                "label_key": "page.system.job.testNumber",
                "scope": TaskScope.TENANT,
                "args_model": NumberArgs,
                "function": number_task,
                "service_protocol": "tenant.no-data/v1",
            }
            | changes
        )
    )


def test_registry_is_explicit_immutable_and_not_the_old_function_registry():
    registry = TaskContractRegistry((spec(),))
    assert (
        registry.resolve("test_number", scope=TaskScope.TENANT).function is number_task
    )
    for key, scope in (
        ("unknown", TaskScope.TENANT),
        ("test_number", TaskScope.PLATFORM),
    ):
        with pytest.raises(BusinessException):
            registry.resolve(key, scope=scope)
    with pytest.raises(BusinessException):
        TaskContractRegistry((spec(), spec()))
    with pytest.raises(BusinessException):
        TaskContractRegistry(tuple(spec(key=f"t{i}") for i in range(257)))
    with pytest.raises(FrozenInstanceError):
        registry.specs = ()
    with pytest.raises(FrozenInstanceError):
        spec().max_retries = 100


@pytest.mark.parametrize(
    "changes",
    [
        {"args_model": BaseModel},
        {"args_model": None},
        {"function": lambda _args: None},
        {"service_protocol": "inherit-creator-roles"},
        {"scope": "tenant"},
        {"max_retries": 4},
        {"timeout_seconds": None},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
    ],
)
def test_missing_schema_service_protocol_or_unbounded_execution_never_qualifies(
    changes,
):
    with pytest.raises(BusinessException):
        spec(**changes)


def test_open_nested_or_subject_bearing_schemas_are_not_task_parameters():
    class Open(ClosedTaskArgs):
        model_config = ConfigDict(extra="allow")

    class DictArgs(ClosedTaskArgs):
        anything: dict

    class Subject(ClosedTaskArgs):
        actor_user_id: int

    class AliasSubject(ClosedTaskArgs):
        model_config = ConfigDict(alias_generator=lambda _name: "tenantId")
        normal: str

    class Child(BaseModel):
        anything: str

    class Nested(ClosedTaskArgs):
        child: Child

    for model in (Open, DictArgs, Subject, AliasSubject, Nested):
        with pytest.raises(BusinessException):
            spec(args_model=model)


def test_call_shape_is_checked_before_registration():
    async def keyword_only(*, args):
        return args

    async def too_many(args, other):
        return args, other

    async def no_args():
        return None

    for function in (keyword_only, too_many, no_args):
        with pytest.raises(BusinessException):
            spec(function=function)


async def test_validated_arguments_are_detached_strict_and_positional():
    task = spec()
    source = {"count": 2}
    prepared = prepare_task_args(task, source)
    source["count"] = 3
    args = task.call_arguments(prepared)
    assert await task.function(*args) == {"count": 2, "label": "label"}
    args[0]["count"] = 4
    assert task.call_arguments(prepared)[0]["count"] == 2
    with pytest.raises(BusinessException):
        spec(key="other").call_arguments(prepared)
    assert "label" not in repr(prepared)
    for raw in (
        '{"count":"2"}',
        '{"count":true}',
        '{"count":2,"unexpected":"sentinel"}',
    ):
        with pytest.raises(BusinessException) as error:
            prepare_task_args(task, raw)
        assert "sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"count":1,"count":2}',
        '{"count":NaN}',
        "[]",
        "null",
        "{broken",
        '{"label":"' + "汉" * 2731 + '"}',
        '{"x":' * 9 + "1" + "}" * 9,
    ],
)
def test_bad_json_and_payload_budgets_fail_before_execution(raw):
    with pytest.raises(BusinessException):
        prepare_task_args(spec(), raw)


def test_default_expansion_is_also_bounded():
    class LargeDefaults(ClosedTaskArgs):
        padding: str = "x" * 8192

    with pytest.raises(BusinessException):
        prepare_task_args(spec(args_model=LargeDefaults), None)


def test_schema_drift_after_registration_is_rejected():
    class Local(ClosedTaskArgs):
        value: int = 1

    task = spec(args_model=Local)
    Local.model_config["extra"] = "allow"
    with pytest.raises(BusinessException):
        prepare_task_args(task, {})


async def test_builtin_specs_keep_empty_task_and_platform_protocols_separate():
    registry = builtin_task_contracts()
    task = registry.resolve("test_task", scope=TaskScope.TENANT)
    assert task.function is get_task_function("test_task")
    for raw in (None, "", "{}", {}):
        prepared = prepare_task_args(task, raw)
        # Argument adaptation, not a production execution or authorization proof.
        assert task.call_arguments(prepared) == ({},)
        await task.function(*task.call_arguments(prepared))
    for raw in (
        '{"args":{}}',
        '{"tenantId":"0"}',
        '{"roleIds":["1"]}',
        '{"purpose":"export"}',
    ):
        with pytest.raises(BusinessException):
            prepare_task_args(task, raw)
    for key in (
        "clean_expired_import_batches",
        "clean_expired_import_previews",
        "clean_expired_export_tasks",
    ):
        with pytest.raises(BusinessException):
            registry.resolve(key, scope=TaskScope.TENANT)
        platform = registry.resolve(key, scope=TaskScope.PLATFORM)
        assert platform.service_protocol == "platform.transfer-retention/v1"
        assert platform.function is get_task_function(key)
    public = registry.tenant_descriptions()
    assert [r["key"] for r in public] == ["test_task"]
    assert public[0]["labelKey"] == task.label_key
    assert "docstring" not in str(public)


def test_no_task_contract_creates_background_work():
    # Merely constructing schemas and snapshots cannot dispatch an async task.
    task = spec()
    prepared = prepare_task_args(task, None)
    assert not asyncio.iscoroutine(prepared)
    assert not hasattr(task, "execute") and not hasattr(prepared, "tenant")


def test_public_schema_keeps_property_names_but_removes_metadata():
    class Fields(ClosedTaskArgs):
        title: str = Field(default="private default", description="private description")
        description: str = "private other default"

    task = spec(args_model=Fields)
    schema = task.public_schema()
    assert set(schema["properties"]) == {"title", "description"}
    assert schema["properties"]["title"] == {"type": "string"}
    assert "private" not in str(schema)
    assert (
        task.call_arguments(prepare_task_args(task, {}))[0]["title"]
        == "private default"
    )


def test_nested_models_cannot_hide_subject_names_behind_aliases():
    class Child(ClosedTaskArgs):
        tenant_id: int = Field(alias="selection")

    class Parent(ClosedTaskArgs):
        child: Child

    with pytest.raises(BusinessException):
        spec(args_model=Parent)


def test_nested_models_also_require_the_closed_contract():
    class Child(BaseModel):
        model_config = ConfigDict(extra="forbid")
        selection: int

    class Parent(ClosedTaskArgs):
        child: Child

    with pytest.raises(BusinessException):
        spec(args_model=Parent)


def test_forged_prepared_bytes_are_revalidated_before_call_arguments():
    task = spec()
    for payload in (b'{"count":"1"}', b'{"tenantId":"0"}', b"\xff", b" " * 8193):
        with pytest.raises(BusinessException):
            task.call_arguments(PreparedTaskArgs(task, payload))
