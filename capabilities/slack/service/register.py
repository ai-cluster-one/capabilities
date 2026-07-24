"""Persistent job register — dedup guard for the slack service daemon.

A message key (``f"{channel}:{ts}"``) is reserved before any work, so a
reconnect or re-delivery cannot process the same message twice. Writes are
atomic (temp file + os.replace)."""

import json
import os
import threading
from pathlib import Path

REGISTER_MAX_KEYS = 5000


class Register:
    def __init__(self, path):
        self._path = Path(path)
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()
        if self._path.is_file():
            try:
                loaded = json.loads(self._path.read_text())
                if isinstance(loaded, dict):
                    self._data = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, ValueError):
                self._data = {}
        # Reset-and-recover: a key still "reserved" means the daemon died mid-job.
        # Drop it so startup catch-up can re-reserve and re-process it exactly once.
        stale = [k for k, v in self._data.items() if v == "reserved"]
        if stale:
            for k in stale:
                del self._data[k]
            self._flush()

    def reserve(self, key: str) -> bool:
        """Reserve a key. True if newly reserved, False if already seen."""
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = "reserved"
            self._flush()
            return True

    def mark_done(self, key: str) -> None:
        with self._lock:
            self._data[key] = "done"
            self._flush()

    def mark_error(self, key: str) -> None:
        with self._lock:
            self._data[key] = "error"
            self._flush()

    def is_done(self, key: str) -> bool:
        return self._data.get(key) == "done"

    def _prune(self) -> None:
        excess = len(self._data) - REGISTER_MAX_KEYS
        if excess <= 0:
            return
        for key in list(self._data.keys()):   # insertion order = oldest first
            if excess <= 0:
                break
            if self._data[key] in ("done", "error"):
                del self._data[key]
                excess -= 1

    def _flush(self) -> None:
        self._prune()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, self._path)
