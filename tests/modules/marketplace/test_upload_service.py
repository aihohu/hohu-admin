import hashlib
import io
import zipfile
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.marketplace.exceptions import AppErrorCode, AppInvalidManifestException
from app.modules.marketplace.service.upload_service import upload_service
from app.utils.storage import read_file


def _make_zip(payload: str = "app") -> bytes:
    """生成最小合法 zip 内容（含一个 entry）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app.json", payload)
    return buf.getvalue()


class TestUploadService:
    async def test_upload_returns_url_and_hash(self, tmp_path, monkeypatch):
        # 重定向 UPLOAD_DIR 到 tmp_path
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        content = _make_zip('{"name":"x"}')
        file_obj = io.BytesIO(content)

        result = await upload_service.save(
            file_obj=file_obj,
            filename="test-app-1.0.0.zip",
            slug="zhangsan-test-app",
            version="1.0.0",
        )

        assert result["file_hash"]
        assert len(result["file_hash"]) == 64
        assert "zhangsan-test-app" in result["file_url"]
        assert "1.0.0" in result["file_url"]
        assert result["file_size"] == len(content)

    async def test_upload_validates_zip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        # 不是 zip（缺少 PK 魔数）
        file_obj = io.BytesIO(b"not a zip")
        with pytest.raises(AppInvalidManifestException):
            await upload_service.save(
                file_obj=file_obj,
                filename="bad.zip",
                slug="bad-app",
                version="1.0.0",
            )

    async def test_hash_matches_sha256_of_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        content = _make_zip("hashable")
        expected_hash = hashlib.sha256(content).hexdigest()

        result = await upload_service.save(
            file_obj=io.BytesIO(content),
            filename="x.zip",
            slug="x-app",
            version="1.0.0",
        )
        assert result["file_hash"] == expected_hash

    async def test_actual_zip_file_accepted(self, tmp_path, monkeypatch):
        """真实 zip 文件（zipfile 模块生成）应该通过校验"""
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("app.json", '{"name":"X"}')
        buf.seek(0)

        result = await upload_service.save(
            file_obj=buf,
            filename="real.zip",
            slug="real-app",
            version="1.0.0",
        )
        assert result["file_size"] > 0
        assert result["file_hash"]

    async def test_upload_writes_file_to_disk(self, tmp_path, monkeypatch):
        """文件应真实落到 UPLOAD_DIR"""
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("hello.txt", "hi")
        buf.seek(0)

        result = await upload_service.save(
            file_obj=buf,
            filename="disk.zip",
            slug="disk-app",
            version="2.0.0",
        )

        written_path = tmp_path / "marketplace" / "disk-app" / "2.0.0" / "disk.zip"
        assert written_path.exists()
        assert result["file_url"].endswith("disk.zip")

    async def test_empty_archive_rejected(self, tmp_path, monkeypatch):
        """空归档（仅 PK\\x05\\x06 结束标记）虽然是合法 zip 但 namelist 为空，应拒绝"""
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        # PK\\x05\\x06 + 8 字节零（最小空归档 22 字节）
        empty_archive = b"PK\x05\x06" + b"\x00" * 18
        with pytest.raises(AppInvalidManifestException):
            await upload_service.save(
                file_obj=io.BytesIO(empty_archive),
                filename="empty.zip",
                slug="empty-app",
                version="1.0.0",
            )

    async def test_read_file_round_trip(self, tmp_path, monkeypatch):
        """上传后通过 read_file 读回，内容应一致"""
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        content = _make_zip('{"name":"round-trip"}')
        result = await upload_service.save(
            file_obj=io.BytesIO(content),
            filename="rt.zip",
            slug="rt-app",
            version="1.0.0",
        )

        # file_url 形如 /uploads/marketplace/rt-app/1.0.0/rt.zip
        relative_path = result["file_url"].removeprefix("/uploads/")
        read_back = await read_file(relative_path)
        assert read_back == content

    async def test_large_file_rejected(self, tmp_path, monkeypatch):
        """超过 MAX_PACKAGE_SIZE 的文件应被拒绝（DoS 防护）"""
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

        # mock 一个略大于 200MB 的内容（避免实际分配 200MB 内存）
        oversized_content = _make_zip("x")  # 合法 zip，绕过格式校验进入大小校验
        with patch(
            "app.modules.marketplace.service.upload_service.MAX_PACKAGE_SIZE",
            len(oversized_content) - 1,
        ):
            with pytest.raises(BusinessException) as exc_info:
                await upload_service.save(
                    file_obj=io.BytesIO(oversized_content),
                    filename="large.zip",
                    slug="large-app",
                    version="1.0.0",
                )

        assert exc_info.value.code == 413
        assert exc_info.value.error_code == AppErrorCode.FILE_TOO_LARGE
