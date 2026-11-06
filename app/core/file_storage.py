"""文件存储抽象。

业务层只依赖 FileStorage Protocol，部署时切换实现：
- LocalFileStorage：本地文件系统，适用于单机或 Docker volume
- 其他后端可实现同一接口接入 S3、MinIO 或 GridFS

业务层禁止 import 具体类（pyproject.toml ruff.banned-imports 限制），
只通过 get_file_storage() 注入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.authorization_file import (
    local_publication,
    read_local,
    require_unbudgeted_legacy_write,
)
from app.core.config import settings
from app.core.exceptions import BusinessRuleException

_OWNED_PREFIX = "_owned/v1/"


def _artifact_error() -> BusinessRuleException:
    return BusinessRuleException(
        "文件归属或完整性待核对，不能重写或删除",
        error_code="AI_ARTIFACT_UNVERIFIED",
    )


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Server-only ownership; never accepted from a file or tool request."""

    tenant_id: int
    operation_id: str
    execution_owner: str
    role: str
    resource_id: str

    def __post_init__(self) -> None:
        if type(self.tenant_id) is not int or self.tenant_id < 0:
            raise _artifact_error()
        if self.role not in {"preview_source", "import_errors", "export"}:
            raise _artifact_error()
        for value in (self.operation_id, self.execution_owner, self.resource_id):
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_:-]{1,160}", value
            ):
                raise _artifact_error()

    @property
    def storage_key(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()
        return f"{_OWNED_PREFIX}{self.tenant_id}/{hashlib.sha256(encoded).hexdigest()}/payload"


def is_owned_artifact(storage_key: str | None) -> bool:
    return isinstance(storage_key, str) and storage_key.startswith(_OWNED_PREFIX)


@dataclass(frozen=True, slots=True)
class ArtifactExecution:
    tenant_id: int
    operation_id: str
    execution_owner: str

    def identity(self, role: str, resource_id: str) -> ArtifactIdentity:
        return ArtifactIdentity(
            self.tenant_id, self.operation_id, self.execution_owner, role, resource_id
        )


def _manifest(data: bytes, identity: ArtifactIdentity) -> dict:
    return {
        "version": 1,
        "identity": asdict(identity),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "retired": False,
    }


def _verify_manifest(
    facts: dict, storage_key: str, data: bytes | None
) -> ArtifactIdentity:
    try:
        if set(facts) != {"version", "identity", "size", "sha256", "retired"}:
            raise _artifact_error()
        owner = ArtifactIdentity(**facts["identity"])
        if (
            facts["version"] != 1
            or type(facts["size"]) is not int
            or facts["size"] < 0
            or type(facts["retired"]) is not bool
            or not isinstance(facts["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", facts["sha256"]) is None
            or owner.storage_key != storage_key
        ):
            raise _artifact_error()
        if data is not None and (
            facts["size"] != len(data)
            or facts["sha256"] != hashlib.sha256(data).hexdigest()
        ):
            raise _artifact_error()
        return owner
    except (TypeError, ValueError, KeyError) as exc:
        raise _artifact_error() from exc


@runtime_checkable
class FileStorage(Protocol):
    """文件存储接口。

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

    async def save_artifact(
        self, data: bytes, *, identity: ArtifactIdentity
    ) -> str: ...

    async def read_artifact(
        self, storage_key: str, *, identity: ArtifactIdentity
    ) -> bytes: ...

    async def artifact_identity(self, storage_key: str) -> ArtifactIdentity: ...

    async def delete_artifact(
        self, storage_key: str, *, identity: ArtifactIdentity
    ) -> bool: ...

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None: ...


class LocalFileStorage:
    """默认的本地文件系统实现。

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
        require_unbudgeted_legacy_write()
        ns_dir = self._resolve_path(namespace)
        ns_dir.mkdir(parents=True, exist_ok=True)

        file_id = f"{uuid.uuid4().hex}{suffix}"
        file_path = ns_dir / file_id
        file_path.write_bytes(data)
        return f"{namespace}/{file_id}"

    async def read(self, storage_key: str) -> bytes:
        if is_owned_artifact(storage_key):
            identity = await self.artifact_identity(storage_key)
            return await self.read_artifact(storage_key, identity=identity)
        for file_path in self._candidate_paths(storage_key):
            if file_path.is_file():
                return read_local(file_path)
        raise FileNotFoundError(f"文件不存在: {storage_key}")

    async def delete(self, storage_key: str) -> bool:
        require_unbudgeted_legacy_write()
        if is_owned_artifact(storage_key):
            raise _artifact_error()
        for file_path in self._candidate_paths(storage_key):
            if file_path.is_file():
                file_path.unlink()
                return True
        return False

    async def exists(self, storage_key: str) -> bool:
        return any(path.is_file() for path in self._candidate_paths(storage_key))

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None:
        return None

    @staticmethod
    def _publish(path: Path, data: bytes, *, replace: bool = False) -> None:
        temporary = path.with_name(f".writing-{uuid.uuid4().hex}")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                # A hard link publishes the complete inode without overwriting
                # an existing object; unsupported filesystems fail closed.
                os.link(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _artifact_writer(self, storage_key: str):
        directory = self._resolve_path(storage_key).parent
        directory.mkdir(parents=True, exist_ok=True)
        lock = directory / "writer.lock"
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise _artifact_error() from exc
        try:
            os.close(descriptor)
            yield directory
        finally:
            lock.unlink(missing_ok=True)

    def _inspect_artifact(
        self, storage_key: str, *, locked: bool = False
    ) -> tuple[dict, bytes]:
        path = self._resolve_path(storage_key)
        if not is_owned_artifact(storage_key) or (
            not locked and (path.parent / "writer.lock").exists()
        ):
            raise _artifact_error()
        try:
            facts = json.loads(read_local(path.parent / "manifest.json", manifest=True))
            data = read_local(path)
            _verify_manifest(facts, storage_key, data)
            if facts["retired"]:
                raise _artifact_error()
            return facts, data
        except (OSError, ValueError, TypeError) as exc:
            raise _artifact_error() from exc

    async def save_artifact(self, data: bytes, *, identity: ArtifactIdentity) -> str:
        """Publish synchronously: cancellation cannot leave a detached writer.

        No thread/task is spawned and no await occurs while the writer lock is
        held. Process death leaves a lock requiring operational reconciliation.
        """
        key = identity.storage_key
        with local_publication(data), self._artifact_writer(key) as directory:
            manifest = directory / "manifest.json"
            payload = directory / "payload"
            expected = _manifest(data, identity)
            if manifest.exists() or payload.exists():
                actual, _ = self._inspect_artifact(key, locked=True)
                if actual != expected:
                    raise _artifact_error()
                return key
            self._publish(manifest, json.dumps(expected, sort_keys=True).encode())
            self._publish(payload, data)
        return key

    async def artifact_identity(self, storage_key: str) -> ArtifactIdentity:
        with local_publication():
            facts, _ = self._inspect_artifact(storage_key)
            return ArtifactIdentity(**facts["identity"])

    async def read_artifact(
        self, storage_key: str, *, identity: ArtifactIdentity
    ) -> bytes:
        with local_publication():
            if storage_key != identity.storage_key:
                raise _artifact_error()
            facts, data = self._inspect_artifact(storage_key)
            if facts["identity"] != asdict(identity):
                raise _artifact_error()
            return data

    async def delete_artifact(
        self, storage_key: str, *, identity: ArtifactIdentity
    ) -> bool:
        with local_publication():
            if storage_key != identity.storage_key:
                raise _artifact_error()
            return self._delete_artifact(storage_key)

    def _delete_artifact(self, storage_key: str) -> bool:
        with self._artifact_writer(storage_key) as directory:
            manifest = directory / "manifest.json"
            try:
                facts = json.loads(read_local(manifest, manifest=True))
                _verify_manifest(facts, storage_key, None)
            except (OSError, ValueError, TypeError) as exc:
                raise _artifact_error() from exc
            payload = directory / "payload"
            if not facts["retired"]:
                self._inspect_artifact(storage_key, locked=True)
                facts["retired"] = True
                self._publish(
                    manifest, json.dumps(facts, sort_keys=True).encode(), replace=True
                )
            existed = payload.exists()
            payload.unlink(missing_ok=True)
            return existed


class MockFileStorage:
    """In-memory dict 实现（测试用）。

    模拟 LocalFileStorage 的 storage_key 格式，但数据存内存 dict。
    public_url 返回 None（与 LocalFileStorage 一致）。
    """

    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._artifacts: dict[str, dict] = {}

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
        if is_owned_artifact(storage_key):
            owner = await self.artifact_identity(storage_key)
            return await self.read_artifact(storage_key, identity=owner)
        if storage_key not in self._store:
            raise FileNotFoundError(f"文件不存在: {storage_key}")
        return self._store[storage_key]

    async def delete(self, storage_key: str) -> bool:
        if is_owned_artifact(storage_key):
            raise _artifact_error()
        return self._store.pop(storage_key, None) is not None

    async def exists(self, storage_key: str) -> bool:
        return storage_key in self._store

    def public_url(self, storage_key: str, *, expires_in: int = 3600) -> str | None:
        return None

    async def save_artifact(self, data: bytes, *, identity: ArtifactIdentity) -> str:
        key = identity.storage_key
        expected = _manifest(data, identity)
        if key in self._artifacts:
            if self._artifacts[key] != expected:
                raise _artifact_error()
            await self.read_artifact(key, identity=identity)
        else:
            self._store[key] = data
            self._artifacts[key] = expected
        return key

    async def artifact_identity(self, storage_key: str) -> ArtifactIdentity:
        if storage_key not in self._artifacts or storage_key not in self._store:
            raise _artifact_error()
        facts = self._artifacts[storage_key]
        if facts["retired"]:
            raise _artifact_error()
        return _verify_manifest(facts, storage_key, self._store[storage_key])

    async def read_artifact(
        self, storage_key: str, *, identity: ArtifactIdentity
    ) -> bytes:
        if await self.artifact_identity(storage_key) != identity:
            raise _artifact_error()
        return self._store[storage_key]

    async def delete_artifact(
        self, storage_key: str, *, identity: ArtifactIdentity
    ) -> bool:
        if storage_key != identity.storage_key or storage_key not in self._artifacts:
            raise _artifact_error()
        if not self._artifacts[storage_key]["retired"]:
            await self.read_artifact(storage_key, identity=identity)
            self._artifacts[storage_key]["retired"] = True
        return self._store.pop(storage_key, None) is not None


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
    """进程级 DI 工厂，首次调用时按 settings 实例化。

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
