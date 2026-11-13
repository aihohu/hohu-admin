"""Unknown historical uploads never acquire invented delivery provenance."""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from app.modules.system.models.file import File
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)


def test_file_provenance_has_an_explicit_unknown_default():
    columns = File.__table__.c
    assert {"delivery_class", "content_sha256", "source_manifest"} <= set(
        columns.keys()
    )
    assert not columns.delivery_class.nullable
    assert columns.content_sha256.nullable and columns.source_manifest.nullable


async def test_existing_file_insert_keeps_unknown_provenance(import_sessions):
    _, plain, _ = import_sessions
    async with plain() as db:
        await db.execute(
            insert(File).values(
                tenant_id=0,
                owner_user_id=101,
                file_id=801,
                original_name="historical.png",
                file_name="801",
                file_path="unknown/801.png",
                file_url="/uploads/unknown/801.png",
                file_size=4,
                file_ext=".png",
            )
        )
        row = await db.scalar(select(File).where(File.file_id == 801))
        assert row.delivery_class == "legacy-unknown"
        assert row.content_sha256 is None and row.source_manifest is None
        await db.rollback()


@pytest.mark.parametrize(
    "values",
    [
        {"delivery_class": "public-image"},
        {"delivery_class": "unknown-kind"},
        {"delivery_class": "legacy-unknown", "content_sha256": "a" * 64},
        {
            "delivery_class": "owned-private",
            "content_sha256": "INVALID",
            "source_manifest": {},
        },
        {
            "delivery_class": "owned-private",
            "content_sha256": "a" * 64,
            "source_manifest": [],
        },
    ],
)
async def test_database_rejects_partial_or_unknown_delivery_proof(
    import_sessions, values
):
    _, plain, _ = import_sessions
    async with plain() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                insert(File).values(
                    tenant_id=0,
                    owner_user_id=101,
                    file_id=802,
                    original_name="file.txt",
                    file_name="802",
                    file_path="private/file.txt",
                    file_url="",
                    file_size=4,
                    file_ext=".txt",
                    **values,
                )
            )
        await db.rollback()
