"""Opaque transfer identifiers must never weaken Snowflake identity checks."""

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
    RecordContext,
    compile_rules,
    project,
)


def opaque_catalog(kind="opaque_id"):
    read = Access("read", "management.read")
    return Catalog(
        (
            ObjectSpec(
                "sample.task",
                "tenant",
                "task.owner/v1",
                (FieldSpec("task_id", kind, (read,), structural=True),),
            ),
        ),
        (
            ActionSpec(
                "task.read",
                ("sample.task",),
                (FieldUse(FieldRef("sample.task", "task_id"), read),),
                ("system:task:list",),
            ),
        ),
    )


@pytest.mark.parametrize(
    "value",
    [
        "6b9b5688-ec01-42fd-bd77-148ca89f8503",
        "batch-legacy-1",
        "a" * 64,
        "_" + "a" * 31,
        "-" + "a" * 31,
        "_",
        "-" * 64,
    ],
)
def test_opaque_structural_identity_is_a_bounded_literal_string(value):
    catalog = opaque_catalog()
    record = RecordContext(
        0, "sample.task", value, frozenset(), frozenset({"system:task:list"})
    )
    assert (
        project(
            catalog,
            compile_rules(catalog, ()),
            record,
            "task.read",
            Access("read", "management.read"),
            {"task_id": value},
        )["task_id"]
        == value
    )


@pytest.mark.parametrize(
    "kind,value",
    [
        ("opaque_id", "../secret"),
        ("opaque_id", "a" * 65),
        ("opaque_id", 101),
        ("opaque_id", ""),
        ("opaque_id", "a/b"),
        ("opaque_id", "a\\b"),
        ("opaque_id", "."),
        ("opaque_id", "a\n"),
        ("opaque_id", "非ascii"),
        ("opaque_id", "_" * 65),
        ("opaque_id", True),
        ("id", "batch-legacy-1"),
        ("id", "_1"),
        ("id", "-1"),
    ],
)
def test_opaque_ids_do_not_accept_paths_numbers_or_change_snowflake(kind, value):
    catalog = opaque_catalog(kind)
    record = RecordContext(
        0, "sample.task", "query", frozenset(), frozenset({"system:task:list"})
    )
    with pytest.raises(BusinessException):
        project(
            catalog,
            compile_rules(catalog, ()),
            record,
            "task.read",
            Access("read", "management.read"),
            {"task_id": value},
        )
