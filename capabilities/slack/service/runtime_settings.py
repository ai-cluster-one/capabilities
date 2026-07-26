"""Persistent per-conversation worker settings for the Slack service."""

import json
import os
import threading
from pathlib import Path


class RuntimeSettings:
    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = {}
        if self._path.is_file():
            try:
                loaded = json.loads(self._path.read_text())
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"cannot load runtime settings {self._path}: {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"runtime settings {self._path} must contain a JSON object"
                )
            self._data = loaded

    def get(self, conversation):
        with self._lock:
            row = self._data.get(str(conversation), {})
            return json.loads(json.dumps(row)) if isinstance(row, dict) else {}

    def replace(self, conversation, row):
        if not isinstance(row, dict):
            raise TypeError("runtime settings row must be an object")
        with self._lock:
            self._data[str(conversation)] = json.loads(json.dumps(row))
            self._flush()

    def _flush(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n")
        tmp.chmod(0o600)
        os.replace(tmp, self._path)
