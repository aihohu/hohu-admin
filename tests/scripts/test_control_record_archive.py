"""Maintenance archival preserves originals and refuses a conflicting history."""

from pathlib import Path

import pytest

from app.core.exceptions import BusinessException
from tools.ops.control_record_archive import archive_control_records


def test_partial_consume_can_restore_exact_originals_with_fence_first(
    tmp_path, monkeypatch
):
    originals = {
        "stop.json": b'{"fenced":true}',
        "policy-prepare.json": b'{"state":"qualified-stopped"}',
        "install.json": b'{"schema":2}',
    }
    for name, value in originals.items():
        (tmp_path / name).write_bytes(value)
    archive = archive_control_records(tmp_path)
    original_unlink = Path.unlink

    def fail_install(path, *args, **kwargs):
        if path == tmp_path / "install.json":
            raise PermissionError("exclusive test injection")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_install)
    with pytest.raises(PermissionError):
        archive.consume()
    assert not (tmp_path / "policy-prepare.json").exists()
    assert (tmp_path / "stop.json").exists()
    archive.restore()
    assert {name: (tmp_path / name).read_bytes() for name in originals} == originals
    assert archive_control_records(tmp_path).records == archive.records


def test_conflicting_archive_cannot_consume_current_installation(tmp_path):
    (tmp_path / "stop.json").write_bytes(b"{}")
    (tmp_path / "install.json").write_bytes(b'{"schema":2}')
    archive = archive_control_records(tmp_path)
    next(
        saved for source, saved, _ in archive.records if source.name == "install.json"
    ).write_bytes(b'{"wrong":true}')
    with pytest.raises(BusinessException):
        archive_control_records(tmp_path)
    assert (tmp_path / "stop.json").read_bytes() == b"{}"
    assert (tmp_path / "install.json").read_bytes() == b'{"schema":2}'
