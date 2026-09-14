"""slug → 物理表名转换与 legacy fail-closed 单测。

避免类似 `app_data_zhangsan-crm` 这种把连字符当作操作符导致 PostgreSQL 语法错误。
"""

import pytest

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.lowcode.type_mapping import (
    make_table_name,
    slug_to_table_prefix,
)


class TestSlugToTableName:
    def test_hyphen_replaced(self):
        assert slug_to_table_prefix("zhangsan-crm") == "zhangsan_crm"

    def test_dot_rejected(self):
        with pytest.raises(AppInvalidManifestException):
            slug_to_table_prefix("my.app")

    def test_underscore_slug_rejected(self):
        with pytest.raises(AppInvalidManifestException):
            slug_to_table_prefix("hohu_crm")

    def test_make_table_name_single(self):
        assert make_table_name("zhangsan-crm") == "app_data_zhangsan_crm"

    def test_make_table_name_with_model(self):
        assert (
            make_table_name("zhangsan-crm", "customer")
            == "app_data_zhangsan_crm_customer"
        )

    def test_make_table_name_legacy_underscore_rejected(self):
        with pytest.raises(AppInvalidManifestException):
            make_table_name("hohu_crm")
