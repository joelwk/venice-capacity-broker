from __future__ import annotations

import threading
from typing import Dict, Tuple


_lock = threading.Lock()
_counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], int] = {}


def inc(name: str, value: int = 1, labels: Dict[str, str] | None = None) -> None:
    key = (str(name), tuple(sorted((labels or {}).items())))
    with _lock:
        _counters[key] = _counters.get(key, 0) + int(value)


def render_prom(prefix: str = "vvv") -> str:
    lines: list[str] = []
    with _lock:
        for (name, label_tuples), v in sorted(_counters.items()):
            label_str = ""
            if label_tuples:
                label_str = "{" + ",".join([f"{k}=\"{val}\"" for k, val in label_tuples]) + "}"
            lines.append(f"{prefix}_{name}{label_str} {int(v)}")
    return "\n".join(lines) + ("\n" if lines else "")

