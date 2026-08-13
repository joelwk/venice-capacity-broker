from __future__ import annotations

import time
from typing import Any


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _mint_cost_floor_usd(
    *,
    fair_value_components: dict[str, Any] | None,
    vvv_price_usd: float | None,
    mint_rate: float | None,
) -> float | None:
    """
    Prefer the fair-value model's cost floor when available.

    Falls back to vvv_price_usd * mint_rate when components are absent.
    """
    if isinstance(fair_value_components, dict):
        base_cost = _safe_float(fair_value_components.get("base_cost"))
        if base_cost is not None and base_cost > 0:
            return base_cost
        mint_cost = _safe_float(fair_value_components.get("mint_cost"))
        if mint_cost is not None and mint_cost > 0:
            return mint_cost
    px = _safe_float(vvv_price_usd)
    rate = _safe_float(mint_rate)
    if px is None or rate is None:
        return None
    derived = px * rate
    return derived if derived > 0 else None


def build_diem_premium_snapshot(
    *,
    price_usd: float | None,
    vvv_price_usd: float | None,
    mint_rate: float | None,
    fair_value_usd: float | None,
    fair_value_components: dict[str, Any] | None,
    price_health: dict[str, Any] | None,
    computed_at_ts: float | None = None,
) -> dict[str, Any]:
    """
    Build a canonical DIEM premium snapshot.

    - premium_fair = price / fair_value
    - premium_mint = price / mint_cost_floor

    Includes price source/trust metadata so operator surfaces can distinguish
    real market moves from pricing fallbacks.
    """
    ts = float(computed_at_ts) if computed_at_ts is not None else time.time()
    px = _safe_float(price_usd)
    vvv_px = _safe_float(vvv_price_usd)
    rate = _safe_float(mint_rate)
    fair = _safe_float(fair_value_usd)

    source = None
    fallback_reason = None
    clamped = None
    valid = None
    trusted_external = None
    if isinstance(price_health, dict):
        source = price_health.get("source")
        fallback_reason = price_health.get("fallback_reason")
        clamped = _as_bool(price_health.get("clamped"))
        valid = price_health.get("valid")
        trusted_external = _as_bool(price_health.get("trusted_external"))

    trusted_price = False
    if _as_bool(valid) and not _as_bool(clamped):
        if str(source or "") == "external_reference":
            trusted_price = bool(trusted_external)
        else:
            trusted_price = True

    mint_floor = _mint_cost_floor_usd(
        fair_value_components=fair_value_components,
        vvv_price_usd=vvv_px,
        mint_rate=rate,
    )

    premium_fair = None
    if px is not None and fair is not None and fair > 0:
        premium_fair = px / fair

    premium_mint = None
    if px is not None and mint_floor is not None and mint_floor > 0:
        premium_mint = px / mint_floor

    return {
        "computedAtTs": ts,
        "priceUsd": px,
        "priceSource": source,
        "fallbackReason": fallback_reason,
        "trustedPrice": trusted_price,
        "vvvPriceUsd": vvv_px,
        "mintRate": rate,
        "fairValueUsd": fair,
        "fairValueComponents": fair_value_components or {},
        "mintCostFloorUsd": mint_floor,
        "premiumFair": premium_fair,
        "premiumMint": premium_mint,
    }


def _fv_driver_subset(components: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(components, dict):
        return {}
    keys = (
        "adoption",
        "horizon_days",
        "illiquidity_discount_applied",
        "scarcity_multiplier",
        "sentiment_adjustment",
    )
    subset: dict[str, Any] = {}
    for key in keys:
        if key in components:
            subset[key] = components.get(key)
    return subset


def compute_diem_premium_attribution(
    *,
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Attribute DIEM premium changes to price moves, fair value moves, or source/trust changes.

    Returns a compact JSON-safe dict suitable for persisting into agent memory.
    """
    if not isinstance(current, dict):
        return {"status": "error", "reason": "invalid_current"}
    if not isinstance(previous, dict):
        return {"status": "no_history"}

    cur_px = _safe_float(current.get("priceUsd"))
    prev_px = _safe_float(previous.get("priceUsd"))
    cur_fair = _safe_float(current.get("fairValueUsd"))
    prev_fair = _safe_float(previous.get("fairValueUsd"))
    cur_pf = _safe_float(current.get("premiumFair"))
    prev_pf = _safe_float(previous.get("premiumFair"))
    cur_pm = _safe_float(current.get("premiumMint"))
    prev_pm = _safe_float(previous.get("premiumMint"))

    cur_source = current.get("priceSource")
    prev_source = previous.get("priceSource")
    cur_trusted = _as_bool(current.get("trustedPrice"))
    prev_trusted = _as_bool(previous.get("trustedPrice"))

    driver_cur = _fv_driver_subset(current.get("fairValueComponents"))
    driver_prev = _fv_driver_subset(previous.get("fairValueComponents"))

    driver_changes: dict[str, Any] = {}
    for key in set(driver_cur.keys()) | set(driver_prev.keys()):
        if driver_cur.get(key) != driver_prev.get(key):
            driver_changes[key] = {
                "from": driver_prev.get(key),
                "to": driver_cur.get(key),
            }

    def _delta(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return a - b

    return {
        "status": "ok",
        "delta": {
            "priceUsd": _delta(cur_px, prev_px),
            "fairValueUsd": _delta(cur_fair, prev_fair),
            "premiumFair": _delta(cur_pf, prev_pf),
            "premiumMint": _delta(cur_pm, prev_pm),
        },
        "priceSourceChanged": cur_source != prev_source,
        "priceSource": {"from": prev_source, "to": cur_source},
        "trustedPriceChanged": cur_trusted != prev_trusted,
        "trustedPrice": {"from": prev_trusted, "to": cur_trusted},
        "fairValueDriverChanges": driver_changes,
    }
