"""RoleOut 序列化测试。

回归：旧实现 extract_dept_ids 用 object.__setattr__(data, "__dict__", ...)
直接覆盖 ORM 实例的 __dict__ 来注入 dept_ids。虽然 spread 保留了
_sa_instance_state，但脆弱。重构后必须保证：
- 从 Role ORM 实例序列化得到正确的 dept_ids
- 序列化后的 RoleOut 不影响原 ORM 对象（不修改其状态）
- BigInteger ID 序列化为字符串
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.constants import DATA_SCOPE_CUSTOM, STATUS_ENABLED
from app.main import app
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleOut, RoleQuery


def _make_role_with_depts(*, role_id: int, dept_ids: list[int]) -> Role:
    role = Role(
        role_id=role_id,
        role_name=f"R-{role_id}",
        role_code=f"R_TEST_SCHEMA_{role_id}",
        role_desc=None,
        data_scope=DATA_SCOPE_CUSTOM,
        status=STATUS_ENABLED,
        create_time=datetime(2026, 1, 1, 12, 0, 0),
    )
    role.depts = [
        Dept(dept_id=did, dept_name=f"D-{did}", ancestors="0", order_num=0, status="1")
        for did in dept_ids
    ]
    return role


class TestRoleOutSerialization:
    def test_extracts_dept_ids_from_orm_relationship(self):
        """从 Role ORM 序列化时，dept_ids 应从 depts 关系提取并序列化为字符串列表。"""
        role = _make_role_with_depts(role_id=1001, dept_ids=[10, 20, 30])
        out = RoleOut.model_validate(role)
        # JSON dump 后是字符串列表（@field_serializer 在 dump 时生效）
        dumped = out.model_dump(mode="json")
        assert dumped["dept_ids"] == ["10", "20", "30"]

    def test_role_id_serialized_as_string(self):
        """Snowflake ID 在 JSON 输出时必须为字符串，避免 JS BigInt 精度丢失。"""
        role = _make_role_with_depts(role_id=123456789012345, dept_ids=[])
        out = RoleOut.model_validate(role)
        dumped = out.model_dump(mode="json")
        assert dumped["role_id"] == "123456789012345"
        assert isinstance(dumped["role_id"], str)

    def test_does_not_mutate_original_orm_instance(self):
        """序列化不应往 ORM 实例的 __dict__ 注入 dept_ids 字段。"""
        role = _make_role_with_depts(role_id=1002, dept_ids=[40])
        # 序列化前 __dict__ 不含 dept_ids
        assert "dept_ids" not in role.__dict__
        RoleOut.model_validate(role)
        # 序列化后 ORM 实例 __dict__ 仍不应含 dept_ids（避免污染 ORM 状态）
        assert "dept_ids" not in role.__dict__, (
            "RoleOut 序列化不应改 ORM 实例的 __dict__"
        )

    def test_empty_depts_produces_empty_list(self):
        """Role 没有任何 depts 关联时，dept_ids 应为 []。"""
        role = _make_role_with_depts(role_id=1003, dept_ids=[])
        out = RoleOut.model_validate(role)
        assert out.model_dump(mode="json")["dept_ids"] == []


class TestRoleQueryDataScope:
    def test_accepts_camel_case_data_scope_filter(self):
        query = RoleQuery.model_validate({"dataScope": DATA_SCOPE_CUSTOM})

        assert query.data_scope == DATA_SCOPE_CUSTOM

    def test_rejects_unknown_data_scope_filter(self):
        with pytest.raises(ValidationError, match="数据权限范围必须是 1~5"):
            RoleQuery(data_scope="9")

    def test_openapi_exposes_camel_case_data_scope_query_parameter(self):
        operation = app.openapi()["paths"]["/system/role/list"]["get"]
        parameter_names = {parameter["name"] for parameter in operation["parameters"]}

        assert "dataScope" in parameter_names
        assert "data_scope" not in parameter_names
