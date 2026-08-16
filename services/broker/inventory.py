from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

_DEFAULT_POLICY_PATH = "db/broker_inventory_policy.json"
_lock = Lock()
_cached: dict[str, Any] | None = None


class IntakePaused(Exception):
    """New quotes and bids are refused while inventory failsafe is hot."""

    def __init__(self, utilization: float | None = None) -> None:
        self.utilization = utilization
        detail = "inventory failsafe hot: new intake paused"
        if utilization is not None:
            detail = f"{detail} (utilization={utilization:.4f})"
        super().__init__(detail)


def extract_usage_diem(usage: Any) -> float | None:
    if not isinstance(usage, dict):
        return None
    for key in ("dailyAverageDiem", "daily_average_diem", "avgDailyDiem"):
        if key in usage:
            try:
                return float(usage[key])
            except (TypeError, ValueError):
                continue
    data = usage.get("data")
    if isinstance(data, list) and data:
        totals: list[float] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            for field in (
                "dailyAverageDiem",
                "daily_average_diem",
                "consumptionDaily",
                "consumption",
            ):
                if field in entry:
                    try:
                        totals.append(float(entry[field]))
                    except (TypeError, ValueError):
                        continue
                    break
        if totals:
            return sum(totals) / len(totals)
    aggregate = usage.get("aggregate")
    if isinstance(aggregate, dict):
        for key in ("daily", "diemDaily", "daily_diem"):
            if key in aggregate:
                try:
                    return float(aggregate[key])
                except (TypeError, ValueError):
                    continue
    return None


def extract_limit_total(limits: Any) -> float | None:
    if limits is None:
        return None
    if isinstance(limits, list):
        entries = limits
    elif isinstance(limits, dict):
        entries = []
        for key in ("data", "items", "keys"):
            value = limits.get(key)
            if isinstance(value, list):
                entries = value
                break
        if not entries:
            entries = [value for value in limits.values() if isinstance(value, dict)]
    else:
        return None
    total = 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        limit = entry.get("consumptionLimit") or entry.get("consumption_limit")
        amount = None
        if isinstance(limit, dict):
            amount = limit.get("diem") or limit.get("daily")
        elif isinstance(limit, (int, float)):
            amount = limit
        if amount is None:
            continue
        try:
            total += float(amount)
        except (TypeError, ValueError):
            continue
    return total if total > 0 else None


def broker_inventory_utilization(usage: Any, limits: Any) -> float | None:
    usage_total = extract_usage_diem(usage)
    limit_total = extract_limit_total(limits)
    if limit_total is None or limit_total <= 0 or usage_total is None:
        return None
    return max(0.0, min(1.0, float(usage_total) / float(limit_total)))


def failsafe_status(utilization: float) -> str:
    surge_threshold = float(os.getenv("BROKER_UTIL_SURGE_THRESHOLD", "0.85"))
    relax_threshold = float(os.getenv("BROKER_UTIL_RELAX_THRESHOLD", "0.40"))
    if utilization >= surge_threshold:
        return "hot"
    if utilization <= relax_threshold:
        return "calm"
    return "normal"


def failsafe_actions(status: str) -> list[str]:
    if status == "hot":
        return ["pause_low_tier", "raise_price"]
    if status == "calm":
        return ["open_intake"]
    return []


def _policy_path() -> Path:
    return Path(os.getenv("BROKER_INVENTORY_POLICY_PATH") or _DEFAULT_POLICY_PATH)


def save_inventory_policy(
    *,
    utilization: float,
    status: str,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = {
        "utilization": float(utilization),
        "failsafe_status": str(status),
        "updated_at": time.time(),
    }
    if pricing is not None:
        snapshot["pricing_mode"] = pricing.get("mode")
    with _lock:
        global _cached
        _cached = snapshot
        path = _policy_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot), encoding="utf-8")
        except OSError:
            pass
    return snapshot


def load_inventory_policy() -> dict[str, Any] | None:
    global _cached
    with _lock:
        if _cached is not None:
            return dict(_cached)
        path = _policy_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        _cached = data
        return dict(data)


def clear_inventory_policy_cache() -> None:
    with _lock:
        global _cached
        _cached = None


def inventory_utilization_ratio() -> float:
    policy = load_inventory_policy()
    if not policy:
        return 0.0
    try:
        return max(0.0, min(1.0, float(policy.get("utilization") or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def assert_intake_open() -> None:
    policy = load_inventory_policy()
    if not policy:
        return
    if str(policy.get("failsafe_status") or "") == "hot":
        util = policy.get("utilization")
        try:
            util_f = float(util) if util is not None else None
        except (TypeError, ValueError):
            util_f = None
        raise IntakePaused(utilization=util_f)
