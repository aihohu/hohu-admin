"""Local FILE limits count physical payload reads, including identity checks."""

import pytest

from app.core.authorization_file import FileBudget
from app.core.exceptions import BusinessException
from app.core.file_storage import ArtifactIdentity, LocalFileStorage


@pytest.mark.parametrize("method", ["artifact_identity", "read_artifact"])
async def test_file_budget_rejects_payload_before_unbounded_read(
    tmp_path, monkeypatch, method
):
    storage = LocalFileStorage(tmp_path)
    identity = ArtifactIdentity(0, "op", "owner", "export", "export-1")
    key = await storage.save_artifact(b"12345", identity=identity)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Path.read_bytes bypassed bounded IO")

    monkeypatch.setattr(type(tmp_path), "read_bytes", forbidden)
    with (
        pytest.raises(BusinessException) as error,
        FileBudget(max_payload_bytes=4, max_total_bytes=8, max_reads=2).activate(),
    ):
        if method == "artifact_identity":
            await storage.artifact_identity(key)
        else:
            await storage.read_artifact(key, identity=identity)
    assert (
        getattr(error.value, "error_code", None) == "AUTHORIZATION_FILE_LIMIT_EXCEEDED"
    )


async def test_identity_and_read_each_consume_payload_budget(tmp_path):
    storage = LocalFileStorage(tmp_path)
    identity = ArtifactIdentity(0, "op", "owner", "export", "export-1")
    key = await storage.save_artifact(b"1234", identity=identity)
    budget = FileBudget(max_payload_bytes=4, max_total_bytes=8, max_reads=2)
    with pytest.raises(BusinessException) as error, budget.activate():
        assert await storage.artifact_identity(key) == identity
        assert await storage.read_artifact(key, identity=identity) == b"1234"
        await storage.artifact_identity(key)
    assert (
        getattr(error.value, "error_code", None) == "AUTHORIZATION_FILE_LIMIT_EXCEEDED"
    )
    assert budget.payload_bytes == 8 and budget.reads == 2


async def test_file_budget_checks_after_synchronous_publication(tmp_path):
    storage = LocalFileStorage(tmp_path)
    identity = ArtifactIdentity(0, "op", "owner", "export", "export-1")
    ticks = iter((0, 0, 0, 2, 2, 2, 2))
    budget = FileBudget(deadline=1, clock=lambda: next(ticks, 2))
    with pytest.raises(BusinessException) as error, budget.activate():
        await storage.save_artifact(b"safe", identity=identity)
    assert (
        getattr(error.value, "error_code", None) == "AUTHORIZATION_TRANSACTION_TIMEOUT"
    )
    assert not list(tmp_path.rglob("writer.lock"))


async def test_file_budget_nested_reset_does_not_allow_more_io():
    budget = FileBudget()
    with budget.activate(), pytest.raises(BusinessException) as error:
        with FileBudget().activate():
            pass
    assert getattr(error.value, "error_code", None) == "FIELD_POLICY_LOCK_ORDER"


async def test_caught_overflow_cannot_trigger_negative_unbounded_read(tmp_path):
    path = tmp_path / "payload"
    path.write_bytes(b"too large")
    budget = FileBudget(max_payload_bytes=1, max_total_bytes=1)
    with pytest.raises(BusinessException) as poisoned, budget.activate():
        with pytest.raises(BusinessException):
            budget.read(path)
        with pytest.raises(BusinessException) as second:
            budget.read(path)
        assert getattr(second.value, "error_code", None) == "FIELD_POLICY_LOCK_ORDER"
        assert budget.reads == 1
    assert getattr(poisoned.value, "error_code", None) == "FIELD_POLICY_LOCK_ORDER"


async def test_caught_publication_error_invalidates_budget(tmp_path, monkeypatch):
    storage = LocalFileStorage(tmp_path)
    identity = ArtifactIdentity(0, "op", "owner", "export", "export-1")

    def fail(*_args, **_kwargs):
        raise OSError("disk failed")

    monkeypatch.setattr(storage, "_publish", fail)
    with pytest.raises(BusinessException), FileBudget().activate():
        with pytest.raises(OSError):
            await storage.save_artifact(b"data", identity=identity)


@pytest.mark.parametrize("failure", ["manifest", "identity"])
async def test_caught_artifact_validation_error_invalidates_budget(tmp_path, failure):
    storage = LocalFileStorage(tmp_path)
    identity = ArtifactIdentity(0, "op", "owner", "export", "export-1")
    key = await storage.save_artifact(b"data", identity=identity)
    if failure == "manifest":
        (tmp_path / key).parent.joinpath("manifest.json").write_text("invalid")
    else:
        identity = ArtifactIdentity(0, "other", "owner", "export", "export-1")
    with pytest.raises(BusinessException), FileBudget().activate():
        with pytest.raises(BusinessException):
            await storage.read_artifact(key, identity=identity)


@pytest.mark.parametrize("method", ["save", "delete"])
async def test_budget_disallows_unowned_legacy_writes(tmp_path, method):
    storage = LocalFileStorage(tmp_path)
    key = await storage.save(b"original", mime_type="text/plain", namespace="legacy")
    with pytest.raises(BusinessException), FileBudget().activate():
        if method == "save":
            await storage.save(b"new", mime_type="text/plain", namespace="legacy")
        else:
            await storage.delete(key)
    assert await storage.read(key) == b"original"
