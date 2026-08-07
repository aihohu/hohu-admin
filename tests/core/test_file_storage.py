"""FileStorage Protocol 测试（Task 0d，spec §3.9）。

覆盖 5 个核心场景：
- Protocol runtime check（LocalFileStorage / MockFileStorage 都通过 isinstance）
- 路径穿越防御（namespace / storage_key 含 `../` 拒绝）
- save → read 往返数据一致
- delete 幂等（不存在返回 False）
- exists 行为
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.core.file_storage import (
    FileStorage,
    LocalFileStorage,
    MockFileStorage,
    get_file_storage,
    reset_file_storage_for_test,
    validate_private_storage_roots,
)


class TestProtocolContract:
    def test_docker_image_declares_public_and_private_storage_volumes(self):
        dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(
            encoding="utf-8"
        )

        assert "mkdir -p /app/uploads /app/private_uploads" in dockerfile
        assert 'VOLUME ["/app/uploads", "/app/private_uploads"]' in dockerfile

    def test_local_storage_is_file_storage(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        assert isinstance(fs, FileStorage)

    def test_configured_local_storage_cannot_live_under_public_upload_root(
        self, tmp_path, monkeypatch
    ):
        public_root = tmp_path / "public"
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(
            settings,
            "LOCAL_FILE_STORAGE_ROOT",
            str(public_root / "file_storage"),
        )
        reset_file_storage_for_test()

        try:
            with pytest.raises(RuntimeError, match="public upload root"):
                get_file_storage()
        finally:
            reset_file_storage_for_test()

    @pytest.mark.parametrize(
        ("setting_name", "relative_path"),
        [
            ("PRIVATE_UPLOAD_DIR", "private"),
            ("LOCAL_FILE_STORAGE_ROOT", "file_storage"),
        ],
    )
    def test_every_private_storage_root_must_be_outside_public_upload_root(
        self,
        tmp_path,
        monkeypatch,
        setting_name,
        relative_path,
    ):
        public_root = tmp_path / "public"
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(
            settings,
            setting_name,
            str(public_root / relative_path),
        )

        with pytest.raises(RuntimeError, match=setting_name):
            validate_private_storage_roots()

    def test_mock_storage_is_file_storage(self):
        fs = MockFileStorage()
        assert isinstance(fs, FileStorage)


class TestPathTraversal:
    async def test_namespace_traversal_rejected(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        with pytest.raises(ValueError, match="路径穿越"):
            await fs.save(
                b"x",
                mime_type="text/plain",
                namespace="../../../etc",
                suffix=".txt",
            )

    async def test_storage_key_traversal_rejected_on_read(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        with pytest.raises(ValueError, match="路径穿越"):
            await fs.read("../../../etc/passwd")

    async def test_storage_key_traversal_rejected_on_delete(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        with pytest.raises(ValueError, match="路径穿越"):
            await fs.delete("../outside")

    async def test_storage_key_traversal_rejected_on_exists(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        with pytest.raises(ValueError, match="路径穿越"):
            await fs.exists("../outside")


class TestSaveReadRoundtrip:
    async def test_local_storage_roundtrip(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        payload = b"hello hohu\n" * 100

        key = await fs.save(
            payload,
            mime_type="text/plain",
            namespace="import-preview",
            suffix=".xlsx",
        )
        assert key.startswith("import-preview/")
        assert key.endswith(".xlsx")

        data = await fs.read(key)
        assert data == payload

    async def test_mock_storage_roundtrip(self):
        fs = MockFileStorage()
        payload = b"mock data"

        key = await fs.save(
            payload,
            mime_type="application/octet-stream",
            namespace="import-error",
        )
        assert await fs.read(key) == payload

    async def test_read_missing_raises(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        with pytest.raises(FileNotFoundError):
            await fs.read("import-preview/nonexistent.xlsx")

    async def test_legacy_public_artifact_is_read_only_fallback(self, tmp_path):
        private_root = tmp_path / "private"
        legacy_root = tmp_path / "public" / "file_storage"
        legacy_file = legacy_root / "import-preview" / "legacy.xlsx"
        legacy_file.parent.mkdir(parents=True)
        legacy_file.write_bytes(b"legacy")
        fs = LocalFileStorage(private_root, legacy_read_roots=(legacy_root,))

        assert await fs.read("import-preview/legacy.xlsx") == b"legacy"
        assert await fs.exists("import-preview/legacy.xlsx") is True

        new_key = await fs.save(
            b"new",
            mime_type="application/octet-stream",
            namespace="import-preview",
            suffix=".xlsx",
        )
        assert (private_root / new_key).read_bytes() == b"new"
        assert not (legacy_root / new_key).exists()


class TestDeleteIdempotent:
    async def test_delete_existing_returns_true(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        key = await fs.save(b"x", mime_type="text/plain", namespace="ns")
        assert await fs.delete(key) is True

    async def test_delete_missing_returns_false(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        assert await fs.delete("ns/nonexistent") is False

    async def test_delete_twice_second_returns_false(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        key = await fs.save(b"x", mime_type="text/plain", namespace="ns")
        assert await fs.delete(key) is True
        assert await fs.delete(key) is False


class TestExists:
    async def test_exists_after_save(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        key = await fs.save(b"x", mime_type="text/plain", namespace="ns")
        assert await fs.exists(key) is True

    async def test_not_exists_after_delete(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        key = await fs.save(b"x", mime_type="text/plain", namespace="ns")
        await fs.delete(key)
        assert await fs.exists(key) is False

    async def test_not_exists_never_saved(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        assert await fs.exists("ns/never") is False


class TestPublicUrl:
    def test_local_returns_none(self, tmp_path):
        fs = LocalFileStorage(tmp_path)
        assert fs.public_url("ns/file") is None

    def test_mock_returns_none(self):
        fs = MockFileStorage()
        assert fs.public_url("ns/file") is None
