"""Private publication is immutable, complete and bound to a server identity."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.core import file_storage
from app.core.exceptions import BusinessRuleException


def identity(**changes):
    values = {
        "tenant_id": 7,
        "operation_id": "action-42",
        "execution_owner": "v0162-file:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "role": "export",
        "resource_id": "1234",
    }
    values.update(changes)
    return file_storage.ArtifactIdentity(**values)


async def test_owned_publication_reuses_exact_content_without_overwrite(tmp_path):
    storage = file_storage.LocalFileStorage(tmp_path)
    owner = identity()
    key = await storage.save_artifact(b"complete", identity=owner)
    assert await storage.save_artifact(b"complete", identity=owner) == key
    assert await storage.read_artifact(key, identity=owner) == b"complete"
    with pytest.raises(BusinessRuleException):
        await storage.save_artifact(b"different", identity=owner)
    assert await storage.read_artifact(key, identity=owner) == b"complete"


@pytest.mark.parametrize(
    "fault", ["partial_payload", "bad_manifest", "missing_manifest"]
)
async def test_damaged_publication_is_not_repaired_or_returned(tmp_path, fault):
    storage = file_storage.LocalFileStorage(tmp_path)
    owner = identity()
    key = await storage.save_artifact(b"complete", identity=owner)
    payload = tmp_path / key
    manifest = payload.parent / "manifest.json"
    if fault == "partial_payload":
        payload.write_bytes(b"comp")
    elif fault == "bad_manifest":
        facts = json.loads(manifest.read_text())
        facts["identity"]["tenant_id"] = 9
        manifest.write_text(json.dumps(facts))
    else:
        manifest.unlink()
    with pytest.raises(BusinessRuleException):
        await storage.read_artifact(key, identity=owner)
    with pytest.raises(BusinessRuleException):
        await storage.save_artifact(b"complete", identity=owner)


async def test_no_cross_tenant_read_or_unrestricted_delete(tmp_path):
    storage = file_storage.LocalFileStorage(tmp_path)
    key = await storage.save_artifact(b"private", identity=identity())
    with pytest.raises(BusinessRuleException):
        await storage.read_artifact(key, identity=identity(tenant_id=8))
    with pytest.raises(BusinessRuleException):
        await storage.delete(key)
    assert await storage.read_artifact(key, identity=identity()) == b"private"


async def test_retirement_rejects_late_publication_of_same_identity(tmp_path):
    storage = file_storage.LocalFileStorage(tmp_path)
    key = await storage.save_artifact(b"private", identity=identity())
    assert await storage.delete_artifact(key, identity=identity()) is True
    with pytest.raises(BusinessRuleException):
        await storage.save_artifact(b"private", identity=identity())
    assert not (tmp_path / key).exists()


async def test_active_or_crashed_writer_blocks_read_and_retirement(tmp_path):
    storage = file_storage.LocalFileStorage(tmp_path)
    key = await storage.save_artifact(b"private", identity=identity())
    (tmp_path / key).parent.joinpath("writer.lock").write_text("unresolved writer")
    with pytest.raises(BusinessRuleException):
        await storage.delete_artifact(key, identity=identity())
    assert (tmp_path / key).read_bytes() == b"private"


async def test_failed_publish_leaves_diagnostic_not_a_readable_artifact(
    tmp_path, monkeypatch
):
    storage = file_storage.LocalFileStorage(tmp_path)
    original_link = file_storage.os.link

    def fail_payload(source, target):
        if str(target).endswith("payload"):
            raise OSError("Injected before payload publication")
        return original_link(source, target)

    monkeypatch.setattr(file_storage.os, "link", fail_payload)
    with pytest.raises(OSError, match="Injected"):
        await storage.save_artifact(b"private", identity=identity())
    assert list(tmp_path.rglob("manifest.json"))
    assert not list(tmp_path.rglob("payload"))
    assert not list(tmp_path.rglob("writer.lock"))


async def test_competing_writer_and_cleanup_cannot_cross_publication(
    tmp_path, monkeypatch
):
    storage = file_storage.LocalFileStorage(tmp_path)
    owner = identity()
    writing = Event()
    release = Event()
    original_publish = storage._publish

    def pause_payload(path, data, **kwargs):
        if path.name == "payload":
            writing.set()
            assert release.wait(timeout=5)
        return original_publish(path, data, **kwargs)

    monkeypatch.setattr(storage, "_publish", pause_payload)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(
            lambda: asyncio.run(storage.save_artifact(b"complete", identity=owner))
        )
        try:
            assert await asyncio.to_thread(writing.wait, 5)
            competitor = file_storage.LocalFileStorage(tmp_path)
            for operation in (
                competitor.read_artifact(owner.storage_key, identity=owner),
                competitor.delete_artifact(owner.storage_key, identity=owner),
                competitor.save_artifact(b"late", identity=owner),
            ):
                with pytest.raises(BusinessRuleException):
                    await operation
            assert not writer.done()
        finally:
            release.set()
        assert await asyncio.wrap_future(writer) == owner.storage_key
    assert await storage.read_artifact(owner.storage_key, identity=owner) == b"complete"
