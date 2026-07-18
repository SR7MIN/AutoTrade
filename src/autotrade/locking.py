from __future__ import annotations

import json
import os
import ctypes
from datetime import datetime, timezone
from pathlib import Path

from .errors import InstanceLockError


def lock_owner_active(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        process_id = int(payload["pid"])
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, process_id
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() == 5  # access denied still means it exists
        os.kill(process_id, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


class SingleInstanceLock:
    """Cross-platform exclusive lock file for the single account writer."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}
        ).encode()
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            try:
                owner = self.path.read_text(encoding="utf-8")
            except OSError:
                owner = "unreadable"
            raise InstanceLockError(
                f"Another writer owns {self.path}: {owner}. Remove only after verifying it stopped."
            ) from exc
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        self._owned = True

    def release(self) -> None:
        if self._owned:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._owned = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
