from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Tuple


_lock = threading.Lock()
_events: List[Tuple[float, Dict[str, Any]]] = []
_max = 256


def emit(kind: str, payload: Dict[str, Any]) -> None:
    ts = float(time.time())
    rec = {"kind": str(kind), **dict(payload)}
    with _lock:
        _events.append((ts, rec))
        if len(_events) > _max:
            del _events[: len(_events) - _max]


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        return [dict({"ts": t}, **e) for (t, e) in _events[-int(limit) :]]

