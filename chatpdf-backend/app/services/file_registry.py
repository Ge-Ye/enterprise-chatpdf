"""Concurrency-safe access to the files.json upload registry.

All read-modify-write operations take an in-process lock plus an OS-level
advisory lock, then replace files.json atomically. This keeps concurrent
uploads/deletes from losing records or leaving a partially written JSON file,
including when the backend runs with more than one process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

META_FILE = settings.upload_path / "files.json"
LOCK_FILE = settings.upload_path / "files.json.lock"

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.02

_THREAD_LOCK = threading.RLock()
_DELETING_LOCK = threading.Lock()
_DELETING_MAX_ENTRIES = 1024
_DELETING: "OrderedDict[str, None]" = OrderedDict()
_TOMBSTONE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


class FileRegistryError(RuntimeError):
    """The registry cannot be read or written safely."""


class _InterProcessLock:
    """Advisory byte-range lock on a dedicated lock file."""

    def __init__(self, path: Path, timeout: float = _LOCK_TIMEOUT_SECONDS):
        self._path = path
        self._timeout = timeout
        self._handle = None

    def __enter__(self) -> "_InterProcessLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a+b")
        try:
            # Windows can lock a byte range beyond EOF, so the lock file can stay
            # empty; writing a seed byte here would race with another process.
            self._handle.seek(0)
            deadline = time.monotonic() + self._timeout
            while True:
                try:
                    self._acquire()
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise FileRegistryError(
                            f"获取文件注册表锁超时: {self._path}"
                        )
                    time.sleep(_LOCK_RETRY_SECONDS)
        except Exception:
            self._handle.close()
            self._handle = None
            raise

    def _acquire(self) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._release()
        except OSError:
            logger.warning("释放文件注册表锁失败: %s", self._path, exc_info=True)
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


def _read_locked() -> List[Any]:
    if not META_FILE.exists():
        return []
    try:
        with META_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        logger.error("files.json 内容损坏: %s", exc)
        raise FileRegistryError("files.json 内容损坏，拒绝覆盖") from exc
    except OSError as exc:
        logger.error("读取 files.json 失败: %s", exc)
        raise FileRegistryError("无法读取 files.json") from exc
    if not isinstance(data, list):
        raise FileRegistryError("files.json 顶层不是数组")
    return data


def _write_locked(records: List[Any]) -> None:
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = META_FILE.with_name(f"{META_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, META_FILE)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def list_records() -> List[dict]:
    """Return a shallow copy of all record objects."""
    with _THREAD_LOCK, _InterProcessLock(LOCK_FILE):
        raw = _read_locked()
    return [dict(item) for item in raw if isinstance(item, dict)]


def get_record(file_id: str) -> Optional[dict]:
    with _THREAD_LOCK, _InterProcessLock(LOCK_FILE):
        raw = _read_locked()
    for item in raw:
        if isinstance(item, dict) and item.get("id") == file_id:
            return dict(item)
    return None


def append_record(record: dict) -> None:
    if not isinstance(record, dict) or not record.get("id"):
        raise ValueError("record 必须包含 id")
    with _THREAD_LOCK, _InterProcessLock(LOCK_FILE):
        raw = _read_locked()
        if any(isinstance(item, dict) and item.get("id") == record["id"] for item in raw):
            raise FileRegistryError(f"记录已存在: {record['id']}")
        raw.append(dict(record))
        _write_locked(raw)


def remove_record(file_id: str) -> Optional[dict]:
    """Remove every record with this id; return the first removed record."""
    with _THREAD_LOCK, _InterProcessLock(LOCK_FILE):
        raw = _read_locked()
        remaining: List[Any] = []
        removed: Optional[dict] = None
        for item in raw:
            if isinstance(item, dict) and item.get("id") == file_id:
                if removed is None:
                    removed = dict(item)
                continue
            remaining.append(item)
        if removed is None:
            return None
        _write_locked(remaining)
        return removed


def _tombstone_path(file_id: str) -> Optional[Path]:
    """Return a safe marker path under <upload>/.deleted, or None for odd ids."""
    if not isinstance(file_id, str) or not _TOMBSTONE_NAME.fullmatch(file_id):
        return None
    return settings.upload_path / ".deleted" / f"{file_id}.deleted"


def mark_deleting(file_id: str) -> None:
    """Remember a deleted id so a still-running indexing task cannot resurrect it.

    The marker is also persisted on disk, so a delete handled by one worker is
    visible to an indexing task running in another worker.
    """
    with _DELETING_LOCK:
        _DELETING[file_id] = None
        _DELETING.move_to_end(file_id)
        while len(_DELETING) > _DELETING_MAX_ENTRIES:
            _DELETING.popitem(last=False)

    marker = _tombstone_path(file_id)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        logger.warning("无法写入删除标记: %s", marker, exc_info=True)


def is_deleting(file_id: str) -> bool:
    with _DELETING_LOCK:
        if file_id in _DELETING:
            return True
    marker = _tombstone_path(file_id)
    return bool(marker and marker.exists())
