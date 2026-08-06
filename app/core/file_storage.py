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
    - 单机部署：默认 "uploads/file_storage"（相对工作目录）
    - Docker：volume mount 到容器内
    - K8s：必须用 PVC 或换 S3FileStorage（多副本本地不共享）
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, *parts: str) -> Path:
        """拼接路径并校验仍在 root 内（防穿越）。"""
        target = (self.root.joinpath(*parts)).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"非法 storage_key（路径穿越）: {'/'.join(parts)}")
        return target

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
        file_path = self._resolve_path(storage_key)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {storage_key}")
        return file_path.read_bytes()

    async def delete(self, storage_key: str) -> bool:
        file_path = self._resolve_path(storage_key)
        if not file_path.exists():
            return False
        file_path.unlink()
        return True

    async def exists(self, storage_key: str) -> bool:
        return self._resolve_path(storage_key).exists()

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


def get_file_storage() -> FileStorage:
    """DI 工厂（spec §3.9）。进程级单例，首次调用时按 settings 实例化。

    业务层注入方式：
        fs = get_file_storage()
        ImportService(fs)
    """
    global _file_storage
    if _file_storage is None:
        if settings.FILE_STORAGE_BACKEND == "local":
            _file_storage = LocalFileStorage(settings.LOCAL_FILE_STORAGE_ROOT)
        else:
            raise ValueError(
                f"未实现的 FILE_STORAGE_BACKEND: {settings.FILE_STORAGE_BACKEND}"
            )
    return _file_storage


def reset_file_storage_for_test(storage: FileStorage | None = None) -> None:
    """测试专用：重置单例（注入 MockFileStorage 或清空）。"""
    global _file_storage
    _file_storage = storage
