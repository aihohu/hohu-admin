from sqlalchemy import Index, PrimaryKeyConstraint, UniqueConstraint

from app.core.tenant_inventory import (
    HOSTED_CONTAINED_TABLES,
    PLATFORM_GLOBAL_TABLES,
    TENANT_MODEL_INVENTORY,
    TenantNullability,
    load_inventory_table,
)

EXPECTED_TENANT_TABLES = {
    "ai_conversation",
    "ai_message",
    "ai_prepared_action",
    "ai_operation_log",
    "ai_routing_log",
    "ai_routing_feedback",
    "tenant_ai_model_policy",
    "sys_user",
    "sys_role",
    "sys_dept",
    "sys_menu",
    "sys_user_role",
    "sys_user_dept",
    "sys_role_menu",
    "sys_role_dept",
    "role_ai_agent",
    "sys_config",
    "sys_dict_type",
    "sys_dict_data",
    "sys_file",
    "sys_data_scope_demo",
    "sys_user_import_batch",
    "sys_user_import_batch_log",
    "sys_user_export_task",
    "sys_job",
    "sys_job_log",
    "sys_operation_log",
    "sys_login_log",
}


def _tenant_leading_keys(table) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint)):
            keys.add(tuple(constraint.columns.keys()))
    for index in table.indexes:
        assert isinstance(index, Index)
        keys.add(tuple(column.name for column in index.columns))
    return {key for key in keys if key and key[0] == "tenant_id"}


def test_tenant_inventory_is_complete_and_has_no_ambiguous_classification():
    assert set(TENANT_MODEL_INVENTORY) == EXPECTED_TENANT_TABLES
    assert not (set(TENANT_MODEL_INVENTORY) & set(PLATFORM_GLOBAL_TABLES))


def test_tenant_owned_models_have_declared_column_and_tenant_leading_access_path():
    for table_name, resource in TENANT_MODEL_INVENTORY.items():
        table = load_inventory_table(resource)
        assert "tenant_id" in table.c, table_name
        expected_nullable = resource.nullability is TenantNullability.AUDIT_OPTIONAL
        assert table.c.tenant_id.nullable is expected_nullable, table_name
        assert table.c.tenant_id.default is None, table_name
        assert table.c.tenant_id.server_default is None, table_name
        assert _tenant_leading_keys(table), table_name


def test_platform_global_models_do_not_reuse_tenant_zero_as_global_scope():
    for resource in PLATFORM_GLOBAL_TABLES.values():
        table = load_inventory_table(resource)
        if resource.identity_column == "tenant_id":
            assert table.c.tenant_id.primary_key, resource.table_name
        else:
            assert "tenant_id" not in table.c, resource.table_name


def test_platform_identity_and_audit_are_explicit_global_resources():
    assert "sys_platform_principal" in PLATFORM_GLOBAL_TABLES
    assert "sys_platform_audit_log" in PLATFORM_GLOBAL_TABLES


def test_marketplace_tables_are_explicitly_hosted_contained() -> None:
    assert set(HOSTED_CONTAINED_TABLES) == {
        "mk_app",
        "mk_app_permission",
        "mk_app_rating",
        "mk_app_review",
        "mk_app_version",
        "mk_tenant_app",
    }
    assert not (set(HOSTED_CONTAINED_TABLES) & set(TENANT_MODEL_INVENTORY))
    assert not (set(HOSTED_CONTAINED_TABLES) & set(PLATFORM_GLOBAL_TABLES))
    for resource in HOSTED_CONTAINED_TABLES.values():
        table = load_inventory_table(resource)
        if resource.tenant_column is None:
            assert "tenant_id" not in table.c
            assert resource.parent_table == "mk_app"
            assert resource.parent_column == "app_id"
        else:
            assert resource.tenant_column in table.c
        assert resource.allowed_tenant_ids == (0,)
