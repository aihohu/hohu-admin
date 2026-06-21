"""slug → 物理表名 转换工具单测

避免类似 `app_data_zhangsan-crm` 这种把连字符当作操作符导致 PostgreSQL 语法错误。
"""

from app.modules.marketplace.lowcode.type_mapping import (
    make_table_name,
    slug_to_table_prefix,
)


class TestSlugToTableName:
    def test_hyphen_replaced(self):
        assert slug_to_table_prefix("zhangsan-crm") == "zhangsan_crm"

    def test_dot_replaced(self):
        assert slug_to_table_prefix("my.app") == "my_app"

    def test_no_special_chars_unchanged(self):
        assert slug_to_table_prefix("hohu_crm") == "hohu_crm"

    def test_make_table_name_single(self):
        assert make_table_name("zhangsan-crm") == "app_data_zhangsan_crm"

    def test_make_table_name_with_model(self):
        assert (
            make_table_name("zhangsan-crm", "customer")
            == "app_data_zhangsan_crm_customer"
        )

    def test_make_table_name_no_special(self):
        assert make_table_name("hohu_crm") == "app_data_hohu_crm"
