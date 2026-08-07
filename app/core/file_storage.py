"""文件存储抽象（spec §3.9 v2.2 P1-4）。

业务层只依赖 FileStorage Protocol，部署时切换实现：
- Phase 1: LocalFileStorage（本地文件系统，单机 / Docker volume）
- Phase 3+: S3FileStorage / MinIOFileStorage / GridFSFileStorage

业务层禁止 import 具体类（pyproject.toml ruff.banned-imports 限制），
只通过 get_file_storage() 注入。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.config import settings


@runtime_checkable
class FileStorage(Protocol):
    """文件存储抽象（spec §3.9）。

    save 返回的 storage_key 是不透明字符串（业务层不解析）：
    - LocalFileStorage: "namespace/uuid.xlsx"（相对 root 的路径）
    - S3FileStorage: 同格式（业务层无感）
    """

    async def save(
        self,
        data: bytes,
        *,
        mime_type: str,
        namespace: str,
        suffix: str = "",
        ttl_seconds: int | None = None,
    ) -> str: ...

    async def read(self, storage_key: str) -> bytes: ...

    async def delete(self, storage_key: str) -> bool: ...

    async def exists(self, storage_key: str) -> bool: ...

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None: ...


class LocalFileStorage:
    """本地文件系统实现（Phase 1 默认，spec §3.9）。

    配置 LOCAL_FILE_STORAGE_ROOT：
    - 单机部署：默认 "private_uploads/file_storage"（相对工作目录，不静态挂载）
    - Docker：volume mount 到容器内
    - K8s：必须用 PVC 或换 S3FileStorage（多副本本地不共享）
    """

    def __init__(
        self,
        root: Path | str,
        *,
        legacy_read_roots: tuple[Path | str, ...] = (),
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Compatibility is read/delete only.  New artifacts are always written
        # below the private primary root.
        self.legacy_read_roots = tuple(
            Path(legacy_root).resolve() for legacy_root in legacy_read_roots
        )

    @staticmethod
    def _resolve_in_root(root: Path, *parts: str) -> Path:
        target = root.joinpath(*parts).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"非法 storage_key（路径穿越）: {'/'.join(parts)}")
        return target

    def _resolve_path(self, *parts: str) -> Path:
        """拼接路径并校验仍在 primary root 内（防穿越）。"""
        return self._resolve_in_root(self.root, *parts)

    def _candidate_paths(self, storage_key: str) -> tuple[Path, ...]:
        return (
            self._resolve_path(storage_key),
            *(
                self._resolve_in_root(legacy_root, storage_key)
                for legacy_root in self.legacy_read_roots
            ),
        )

    async def save(
        self,
        data: bytes,
        *,
        mime_type: str,
        namespace: str,
        suffix: str = "",
        ttl_seconds: int | None = None,
    ) -> str:
        ns_dir = self._resolve_path(namespace)
        ns_dir.mkdir(parents=True, exist_ok=True)

        file_id = f"{uuid.uuid4().hex}{suffix}"
        file_path = ns_dir / file_id
        file_path.write_bytes(data)
        return f"{namespace}/{file_id}"

    async def read(self, storage_key: str) -> bytes:
        for file_path in self._candidate_paths(storage_key):
            if file_path.is_file():
                return file_path.read_bytes()
        raise FileNotFoundError(f"文件不存在: {storage_key}")

    async def delete(self, storage_key: str) -> bool:
        for file_path in self._candidate_paths(storage_key):
            if file_path.is_file():
                file_path.unlink()
                return True
        return False

    async def exists(self, storage_key: str) -> bool:
        return any(path.is_file() for path in self._candidate_paths(storage_key))

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None:
        return None


class MockFileStorage:
    """In-memory dict 实现（测试用）。

    模拟 LocalFileStorage 的 storage_key 格式，但数据存内存 dict。
    public_url 返回 None（与 LocalFileStorage 一致）。
    """

    def __init__(self):
        self._store: dict[str, bytes] = {}

    async def save(
        self,
        data: bytes,
        *,
        mime_type: str,
        namespace: str,
        suffix: str = "",
        ttl_seconds: int | None = None,
    ) -> str:
        key = f"{namespace}/{uuid.uuid4().hex}{suffix}"
        self._store[key] = data
        return key

    async def read(self, storage_key: str) -> bytes:
        if storage_key not in self._store:
            raise FileNotFoundError(f"文件不存在: {storage_key}")
        return self._store[storage_key]

    async def delete(self, storage_key: str) -> bool:
        return self._store.pop(storage_key, None) is not None

    async def exists(self, storage_key: str) -> bool:
        return storage_key in self._store

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None:
        return None


_file_storage: FileStorage | None = None


def validate_private_storage_roots() -> None:
    """Fail fast if a private storage root is exposed by ``/uploads``.

    This is called before the public static mount and again by the lazy storage
    factory.  Re-validating is cheap and keeps direct service/test use safe.
    """
    public_root = Path(settings.UPLOAD_DIR).resolve()
    private_roots: list[tuple[str, Path]] = [
        ("PRIVATE_UPLOAD_DIR", Path(settings.PRIVATE_UPLOAD_DIR).resolve()),
    ]
    if settings.FILE_STORAGE_BACKEND == "local":
        private_roots.append(
            (
                "LOCAL_FILE_STORAGE_ROOT",
                Path(settings.LOCAL_FILE_STORAGE_ROOT).resolve(),
            )
        )

    for setting_name, private_root in private_roots:
        if private_root == public_root or private_root.is_relative_to(public_root):
            raise RuntimeError(
                f"{setting_name} must not be inside the public upload root"
            )


def get_file_storage() -> FileStorage:
    """DI 工厂（spec §3.9）。进程级单例，首次调用时按 settings 实例化。

    业务层注入方式：
        fs = get_file_storage()
        ImportService(fs)
    """
    global _file_storage
    if _file_storage is None:
        if settings.FILE_STORAGE_BACKEND == "local":
            validate_private_storage_roots()
            _file_storage = LocalFileStorage(
                settings.LOCAL_FILE_STORAGE_ROOT,
                legacy_read_roots=(Path(settings.UPLOAD_DIR) / "file_storage",),
            )
        else:
            raise ValueError(
                f"未实现的 FILE_STORAGE_BACKEND: {settings.FILE_STORAGE_BACKEND}"
            )
    return _file_storage


def reset_file_storage_for_test(storage: FileStorage | None = None) -> None:
    """测试专用：重置单例（注入 MockFileStorage 或清空）。"""
    global _file_storage
    _file_storage = storage
