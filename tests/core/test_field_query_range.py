"""Time-window filtering has its own permission, independent from date reading."""

from app.core.field_policy import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    Grant,
    ObjectSpec,
    RecordContext,
    compile_rules,
    resolve_fields,
)


def test_range_query_and_read_never_imply_each_other():
    read = Access("read", "management.read")
    interval = Access("range", "management.query", "filter")
    ref = FieldRef("sample.task", "created_at")
    catalog = Catalog(
        (
            ObjectSpec(
                "sample.task",
                "tenant",
                "task.owner/v1",
                (FieldSpec("created_at", "datetime", (read, interval)),),
            ),
        ),
        (
            ActionSpec(
                "task.list",
                ("sample.task",),
                (FieldUse(ref, read), FieldUse(ref, interval)),
                ("system:task:list",),
            ),
        ),
    )
    record = RecordContext(
        0, "sample.task", "query", frozenset({"201"}), frozenset({"system:task:list"})
    )
    for permitted, denied in ((read, interval), (interval, read)):
        rules = compile_rules(catalog, (Grant(0, "201", ref, permitted),))
        assert resolve_fields(catalog, rules, record, "task.list", permitted)[
            "created_at"
        ].allowed
        assert not resolve_fields(catalog, rules, record, "task.list", denied)[
            "created_at"
        ].allowed
