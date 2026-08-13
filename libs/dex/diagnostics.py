from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_LOG_PATH = Path(os.getenv("DEX_DIAGNOSTICS_LOG") or "logs/dex_diagnostics.jsonl")
_last_provider_config: str | None = None
_last_provider_config_lock = threading.Lock()

# In-process counters for bridge-leg failures: (provider, reason, direction) -> count
_bridge_leg_failure_counts: dict[tuple[str, str, str], int] = defaultdict(int)
_FailureCountsLock = threading.Lock()


def _json_default(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_default(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_default(v) for v in value]
    try:
        return str(value)
    except Exception:
        return repr(value)


def log_event(event: dict[str, Any]) -> None:
    global _last_provider_config
    record = dict(event or {})

    # Deduplicate dex_provider_configuration events
    if record.get("event") == "dex_provider_configuration":
        # Build a stable key from config (exclude timestamp)
        config_key = json.dumps(
            {k: v for k, v in sorted(record.items()) if k not in ("ts",)},
            separators=(",", ":"),
            sort_keys=True,
        )
        with _last_provider_config_lock:
            if config_key == _last_provider_config:
                return  # Skip duplicate
            _last_provider_config = config_key

    record.setdefault("ts", time.time())

    # Track bridge-leg failures in counters
    if record.get("event") == "dex_bridge_leg_failure":
        provider = record.get("provider", "unknown")
        reason = record.get("reason", "unknown")
        mode = record.get("mode", "unknown")
        with _FailureCountsLock:
            _bridge_leg_failure_counts[(provider, reason, mode)] += 1

        # Periodically emit aggregated counts (every 10th failure per key)
        count = _bridge_leg_failure_counts[(provider, reason, mode)]
        if count % 10 == 0:
            try:
                snapshot = {
                    "event": "dex_bridge_leg_failure_counts",
                    "ts": time.time(),
                    "counts": {
                        f"{p}_{r}_{d}": c
                        for (p, r, d), c in _bridge_leg_failure_counts.items()
                    },
                }
                snapshot_payload = json.dumps(
                    snapshot, separators=(",", ":"), default=_json_default
                )
                with _LOCK:
                    with _LOG_PATH.open("a", encoding="utf-8") as handle:
                        handle.write(snapshot_payload)
                        handle.write("\n")
            except Exception:
                pass

    try:
        payload = json.dumps(record, separators=(",", ":"), default=_json_default)
    except Exception:
        # Best-effort fallback if serialization fails
        record = {"ts": time.time(), "event": "serialization_error"}
        payload = json.dumps(record, separators=(",", ":"))

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    except Exception:
        # Diagnostics should never raise back to callers
        return


def get_bridge_leg_failure_counts() -> dict[str, int]:
    """Get current bridge-leg failure counts as a flat dict."""
    with _FailureCountsLock:
        return {
            f"{p}_{r}_{d}": c for (p, r, d), c in _bridge_leg_failure_counts.items()
        }


def log_diem_buy_strategy(
    strategy: str,
    route_tokens: list[str],
    provider_selected: str | None = None,
    skip_reason: str | None = None,
    is_exact_out: bool = False,
    adjust_step: int | None = None,
) -> None:
    """Log DIEM buy strategy selection with structured fields."""
    log_event(
        {
            "event": "diem_buy_strategy",
            "strategy": strategy,
            "route_tokens": route_tokens,
            "provider_selected": provider_selected,
            "skip_reason": skip_reason,
            "is_exact_out": is_exact_out,
            "adjust_step": adjust_step,
        }
    )
