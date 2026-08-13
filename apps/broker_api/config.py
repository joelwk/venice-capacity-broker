"""
Configuration and utility helpers for the Venice Broker API.

Provides environment variable defaults, expiry computation, and field extraction utilities.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from typing import Any

# --- Environment defaults ---
DEFAULT_QUOTA = int((os.getenv("BROKER_DEFAULT_QUOTA") or "1000").strip() or 1000)
DEFAULT_EXPIRY_DAYS = int(
    (os.getenv("BROKER_DEFAULT_EXPIRY_DAYS") or "30").strip() or 30
)


def _default_expiry_days() -> int:
    raw = os.getenv("BROKER_DEFAULT_EXPIRY_DAYS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_EXPIRY_DAYS
    try:
        return int(str(raw).strip())
    except Exception:
        return DEFAULT_EXPIRY_DAYS


def compute_expires_at(now_s: float | None = None) -> str | None:
    """Compute expiry timestamp based on DEFAULT_EXPIRY_DAYS.

    Returns ISO8601 Zulu string like '2025-11-28T00:00:00Z' or None when disabled.
    """
    expiry_days = _default_expiry_days()
    if expiry_days <= 0:
        return None
    t = int((now_s or time.time()) + expiry_days * 24 * 3600)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def extract_field(payload: Any, candidates: Iterable[str]) -> str:
    """Find the first non-empty string (or numeric) value for any candidate key.

    Recursively searches through nested dicts, lists, tuples, and sets.

    Args:
        payload: Data structure to search (dict, list, etc.)
        candidates: Iterable of key names to search for

    Returns:
        First non-empty string value found, or empty string if none found

    Example:
        >>> extract_field({"data": {"apiKey": "abc123"}}, ["apiKey", "key"])
        "abc123"
    """
    try:
        keys_iter = [k for k in candidates if k]
    except Exception:
        keys_iter = list(candidates)

    seen: set[int] = set()
    stack: list[Any] = [payload]

    while stack:
        current = stack.pop()

        # Track visited objects to avoid cycles
        if isinstance(current, (dict, list, tuple, set)):
            ident = id(current)
            if ident in seen:
                continue
            seen.add(ident)

        if isinstance(current, dict):
            # Check for candidate keys in current dict
            for key in keys_iter:
                if key not in current:
                    continue
                value = current[key]
                if isinstance(value, str):
                    text = value.strip()
                    if text:
                        return text
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    return str(value)

            # Add nested structures to stack
            for value in current.values():
                if isinstance(value, (dict, list, tuple, set)):
                    stack.append(value)

        elif isinstance(current, (list, tuple, set)):
            # Add items to stack for further searching
            for item in current:
                if isinstance(item, (dict, list, tuple, set)):
                    stack.append(item)

    return ""


__all__ = [
    "DEFAULT_EXPIRY_DAYS",
    "DEFAULT_QUOTA",
    "compute_expires_at",
    "extract_field",
]
