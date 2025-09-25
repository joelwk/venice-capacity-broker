from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Deque, Dict, Iterable, List, Optional


class MemoryStore:
    """Lightweight append-only store for agent cycle records."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        buffer_size: int = 256,
    ) -> None:
        default_path = os.getenv("AGENT_MEMORY_PATH") or "db/agent_memory.jsonl"
        self._path = Path(path or default_path)
        self._lock = Lock()
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=max(1, int(buffer_size)))

    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    def record_cycle(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a cycle record and keep a short in-memory buffer."""

        entry = {
            "ts": record.get("ts"),
            "cycle": self._sanitize(record),
        }
        payload = json.dumps(entry, separators=(",", ":"))

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
            self._buffer.append(entry)
        return entry

    # ------------------------------------------------------------------
    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recent limit entries (oldest -> newest)."""

        if limit <= 0:
            return []
        with self._lock:
            if not self._path.exists():
                return list(self._buffer)[-limit:]
            return self._tail_locked(limit)

    # ------------------------------------------------------------------
    def most_recent(self) -> Optional[Dict[str, Any]]:
        """Return the latest record if available."""

        entries = self.recent(1)
        return entries[0] if entries else None

    # ------------------------------------------------------------------
    def _tail_locked(self, count: int) -> List[Dict[str, Any]]:
        entries: Deque[Dict[str, Any]] = deque(maxlen=count)
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        entries.append(obj)
                except json.JSONDecodeError:
                    continue
        return list(entries)

    # ------------------------------------------------------------------
    def _sanitize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): self._sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize(v) for v in value]
        return str(value)


__all__ = ["MemoryStore"]
