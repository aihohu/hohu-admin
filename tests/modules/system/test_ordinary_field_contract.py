"""Ordinary object adapters must declare the exact executable footprint."""

import pytest
from pydantic import ValidationError

from app.core.field_policy import Access, FieldRef
from app.modules.system.field_policy.ordinary_catalog import ordinary_catalog
from app.modules.system.schemas.data_scope_demo import DataScopeDemoUpdate


def test_catalog_has_ordinary_objects_and_closed_write_footprints():

    catalog = ordinary_catalog()
    assert set(catalog.objects) == {
        "system.user",
        "system.dict_type",
        "system.dict_data",
        "system.data_scope_demo",
    }
    for object_id, anchor in (
        ("system.dict_type", "dict_type_id"),
        ("system.dict_data", "dict_code"),
        ("system.data_scope_demo", "demo_id"),
    ):
        assert catalog.fields[FieldRef(object_id, anchor)].structural
    for field in ("dept_id", "create_by"):
        assert not any(
            a.operation in {"create", "update"}
            for a in catalog.fields[FieldRef("system.data_scope_demo", field)].accesses
        )
    assert (
        Access("sort", "management.query", "sort")
        in catalog.fields[FieldRef("system.dict_data", "dict_sort")].accesses
    )
    assert not any(
        a.operation == "export"
        for f in catalog.objects["system.dict_data"].fields
        for a in f.accesses
    )


@pytest.mark.parametrize("field", ["deptId", "createBy", "tenantId", "demoId"])
def test_demo_rejects_attempted_structural_patch(field):
    with pytest.raises(ValidationError):
        DataScopeDemoUpdate.model_validate({"title": "ok", field: "900"})
