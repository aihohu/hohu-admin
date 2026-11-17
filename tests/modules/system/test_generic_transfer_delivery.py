"""Real generic manifests must govern every user transfer purpose."""

from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import event, insert, select

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.core.file_storage import LocalFileStorage
from app.db.authorization_transfer_owner import TransferOwner
from app.db.authorization_transfer_read_owner import TransferReadOwner
from app.db.base import user_depts
from app.modules.system.models.user_transfer import UserExportTask
from app.modules.system.schemas.user_transfer import UserExportTaskQuery
from app.modules.system.service import user_export_service as exports
from app.modules.system.service import user_transfer_read_service as service
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_transfer_fields import user_transfer_fields
from tests.modules.system import test_relation_field_writes as helper
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def test_generic_transfer_context_and_export_use_current_manifest(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    await activate_relations((factory, plain), monkeypatch)
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            context = await user_transfer_fields.context(
                db, tx, tenant=tenant, version=1
            )
            assert context.grants
            stamp = await user_transfer_fields.stamp(db, tx, tenant_id=0)
            assert stamp["policy_kind"] == "generic"
            assert stamp["active_digest"]
        await db.rollback()
    result = await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
        tenant=tenant, actor_user_version=1, args={"reason": "generic transfer"}
    )
    book = load_workbook(BytesIO(result.data), read_only=True)
    assert "账号" in tuple(next(book.active.values))
    book.close()


async def test_generic_import_does_not_inherit_manual_create(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    # Removing every import create variant must not be replaced by the manual
    # create grant that remains for the same original user field.
    original = helper.rules_document

    def without_import_nickname(catalog, grants, **kwargs):
        return original(
            catalog,
            tuple(
                g
                for g in grants
                if not (
                    g.ref.field_id == "nickname"
                    and g.access.operation == "create"
                    and g.access.purpose == "import"
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(helper, "rules_document", without_import_nickname)
    await activate_relations((factory, plain), monkeypatch)
    with pytest.raises(BusinessException) as caught:
        await TransferOwner(factory, LocalFileStorage(tmp_path)).preview(
            tenant=tenant,
            actor_user_version=1,
            file_bytes=b"user_name,dept_input,nickname\nnewperson,Team,New Person\n",
            filename="input.csv",
            reason="generic import",
        )
    assert caught.value.error_code == "FIELD_WRITE_DENIED"
    assert not list(tmp_path.rglob("payload"))


async def test_generic_export_records_value_free_relation_provenance(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    await activate_relations((factory, plain), monkeypatch)
    owner = TransferOwner(factory, LocalFileStorage(tmp_path))
    result = await owner.export(
        tenant=tenant, actor_user_version=1, args={"reason": "provenance"}
    )
    async with plain() as db:
        task = await db.scalar(
            select(UserExportTask).where(UserExportTask.export_id == result.resource_id)
        )
        document = task.field_lineage["export"]
        assert document["schema"] == "user-transfer-fields/2"
        assert document["related_sources"]
        assert "R_SUPER" not in str(document["related_sources"])
    content, _ = await owner.download(
        tenant=tenant,
        actor_user_version=1,
        kind="export",
        identifier=result.resource_id,
        administrative=True,
    )
    assert content == result.data


@pytest.mark.parametrize(
    "source,field,header",
    [
        ("system.dept", "dept_name", "部门"),
        ("system.role", "role_code", "角色编码"),
        ("system.role", "status", "角色编码"),
        ("system.dept", "parent_id", "部门"),
    ],
)
async def test_export_relation_columns_need_the_real_source_fields(
    import_sessions, monkeypatch, tmp_path, source, field, header
):
    factory, plain, tenant = import_sessions
    async with plain() as db:
        await db.execute(
            insert(user_depts).values(
                tenant_id=0, user_id=101, dept_id=301, is_primary="1"
            )
        )
        await db.commit()
    await activate_relations(
        (factory, plain), monkeypatch, missing=((source, field, "read"),)
    )
    result = await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
        tenant=tenant, actor_user_version=1, args={"reason": "source protection"}
    )
    book = load_workbook(BytesIO(result.data), read_only=True)
    assert header not in tuple(next(book.active.values))
    book.close()


async def test_template_dictionary_cannot_restore_hidden_dept_and_role_names(
    import_sessions, monkeypatch
):
    factory, plain, tenant = import_sessions
    await activate_relations(
        (factory, plain),
        monkeypatch,
        missing=(
            ("system.dept", "dept_name", "read"),
            ("system.role", "role_code", "read"),
        ),
    )
    payload = await TransferReadOwner(factory).execute(
        tenant=tenant, actor_user_version=1, operation="template"
    )
    book = load_workbook(BytesIO(payload), read_only=True)
    dept_values = tuple(book["部门字典"].values)
    role_values = tuple(book["角色字典"].values)
    assert not any(value == "Team" for row in dept_values for value in row)
    assert not any(
        value in {"R_SUPER", "R_USER"} for row in role_values for value in row
    )
    book.close()


async def test_template_encoding_starts_after_every_database_transaction_ends(
    import_sessions, monkeypatch
):
    factory, plain, tenant = import_sessions
    active, calls = set(), []
    engine = plain.kw["bind"].sync_engine
    original = service._build_template

    def begin(connection):
        active.add(connection)

    def finish(connection):
        active.discard(connection)

    def encode(*args):
        assert not active
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(service, "_build_template", encode)
    for name, fn in (("begin", begin), ("commit", finish), ("rollback", finish)):
        event.listen(engine, name, fn)
    try:
        payload = await TransferReadOwner(factory).execute(
            tenant=tenant, actor_user_version=1, operation="template"
        )
        assert payload.startswith(b"PK") and calls == [True]
    finally:
        for name, fn in (("begin", begin), ("commit", finish), ("rollback", finish)):
            event.remove(engine, name, fn)


async def test_generic_export_history_is_sparse_without_internal_metadata(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    await activate_relations(
        (factory, plain),
        monkeypatch,
        missing=(
            ("transfer.export", "status", "read"),
            ("transfer.export", "row_count", "read"),
        ),
    )
    await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
        tenant=tenant, actor_user_version=1, args={"reason": "private reason"}
    )
    page = await TransferReadOwner(factory).execute(
        tenant=tenant,
        actor_user_version=1,
        operation="exports",
        query=UserExportTaskQuery(),
    )
    assert "fieldPolicy" in page
    row = page["records"][0]
    assert "exportId" in row
    assert (
        not {"status", "rowCount", "filterSnapshot", "reason", "operatorId"}
        & row.keys()
    )


async def test_generic_history_query_is_proven_before_status_filter(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    await activate_relations(
        (factory, plain), monkeypatch, missing=(("transfer.export", "status", "eq"),)
    )
    await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
        tenant=tenant, actor_user_version=1, args={"reason": "query proof"}
    )
    with pytest.raises(BusinessException) as caught:
        await TransferReadOwner(factory).execute(
            tenant=tenant,
            actor_user_version=1,
            operation="exports",
            query=UserExportTaskQuery(status="FAILED"),
        )
    assert caught.value.code == 403


async def test_import_relation_matching_is_not_granted_by_source_read(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    await activate_relations(
        (factory, plain), monkeypatch, missing=(("system.dept", "dept_name", "exact"),)
    )
    with pytest.raises(BusinessException) as caught:
        await TransferOwner(factory, LocalFileStorage(tmp_path)).preview(
            tenant=tenant,
            actor_user_version=1,
            file_bytes=b"user_name,dept_input\nnewperson,Team\n",
            filename="input.csv",
            reason="match proof",
        )
    assert caught.value.code == 403


async def test_export_never_hydrates_target_users_as_orm_entities(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    await activate_relations((factory, plain), monkeypatch)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("generic export hydrated complete target User entities")

    monkeypatch.setattr(exports, "_query_users_with_data_scope", forbidden)
    result = await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
        tenant=tenant, actor_user_version=1, args={"reason": "scalar projection"}
    )
    assert result.data.startswith(b"PK")


async def test_export_join_use_does_not_require_unrelated_filter_use(
    import_sessions, monkeypatch, tmp_path
):
    factory, plain, tenant = import_sessions
    original = helper.rules_document

    def join_only(catalog, grants, **kwargs):
        return original(
            catalog,
            tuple(
                g
                for g in grants
                if not (
                    g.ref.object_id == "system.user"
                    and g.ref.field_id == "departments"
                    and g.access.operation == "eq"
                    and g.access.use == "filter"
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(helper, "rules_document", join_only)
    await activate_relations((factory, plain), monkeypatch)
    result = await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
        tenant=tenant,
        actor_user_version=1,
        args={"reason": "join use", "dept_id": "301"},
    )
    assert result.data.startswith(b"PK")
