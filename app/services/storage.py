"""Blob storage behind a tiny interface so S3/GCS can replace the local disk
without touching the pipeline. Keys are content-addressed: <owner>/<sha[:2]>/<sha>.<ext>."""

import os
import shutil
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import settings


class Storage(ABC):
    @abstractmethod
    def temp_path(self) -> Path: ...

    @abstractmethod
    def commit(self, temp: Path, key: str) -> None: ...

    @abstractmethod
    def local_path(self, key: str) -> Path:
        """Path a worker can read. For S3 this would download to a temp file."""

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalStorage(Storage):
    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / ".tmp").mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("storage key escapes storage root")
        return path

    def temp_path(self) -> Path:
        return self.root / ".tmp" / uuid.uuid4().hex

    def commit(self, temp: Path, key: str) -> None:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp, dest)  # atomic on the same filesystem

    def local_path(self, key: str) -> Path:
        return self._resolve(key)

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def wipe(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def storage_key(owner_id: str, sha256: str, file_type: str) -> str:
    safe_owner = "".join(c for c in owner_id if c.isalnum() or c in "-_") or "default"
    return f"{safe_owner}/{sha256[:2]}/{sha256}.{file_type}"


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(settings.storage_dir)
    return _storage
