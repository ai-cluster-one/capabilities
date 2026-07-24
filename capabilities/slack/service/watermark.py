"""Per-channel last-seen ts watermark for the slack service daemon.

Slack ts values are decimal-string timestamps; compare as float. The store is a
flat {channel: last_ts} JSON map, written atomically (temp file + os.replace)."""

import json
import os
import threading
from pathlib import Path


def ts_le(a, b) -> bool:
    """True if timestamp a <= b (numeric compare, string fallback)."""
    try:
        return float(a) <= float(b)
    except (TypeError, ValueError):
        return str(a) <= str(b)


class Watermark:
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

    def get(self, channel: str):
        return self._data.get(channel)

    def keys(self):
        return list(self._data.keys())

    def advance(self, channel: str, ts: str) -> bool:
        with self._lock:
            cur = self._data.get(channel)
            if cur is not None and ts_le(ts, cur):
                return False
            self._data[channel] = str(ts)
            self._flush()
            return True

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, self._path)
