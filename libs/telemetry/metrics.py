from __future__ import annotations

import threading

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}


def inc(name: str, value: int = 1, labels: dict[str, str] | None = None) -> None:
    key = (str(name), tuple(sorted((labels or {}).items())))
    with _lock:
        _counters[key] = _counters.get(key, 0) + int(value)


def set_gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    key = (str(name), tuple(sorted((labels or {}).items())))
    try:
        v = float(value)
    except Exception:
        return
    with _lock:
        _gauges[key] = v


def render_prom(prefix: str = "vvv") -> str:
    lines: list[str] = []
    with _lock:
        for (name, label_tuples), v in sorted(_counters.items()):
            label_str = ""
            if label_tuples:
                label_str = (
                    "{" + ",".join([f'{k}="{val}"' for k, val in label_tuples]) + "}"
                )
            lines.append(f"{prefix}_{name}{label_str} {int(v)}")
        for (name, label_tuples), v in sorted(_gauges.items()):
            label_str = ""
            if label_tuples:
                label_str = (
                    "{" + ",".join([f'{k}="{val}"' for k, val in label_tuples]) + "}"
                )
            lines.append(f"{prefix}_{name}{label_str} {v}")
    return "\n".join(lines) + ("\n" if lines else "")
