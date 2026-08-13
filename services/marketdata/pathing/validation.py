"""Validation helpers for marketdata pathing."""

from __future__ import annotations

from libs.dex.routes import RoutePlan


def validate_diem_route_price(route: RoutePlan, price: float) -> tuple[bool, str]:
    """Validate a DIEM route quote against bridge reference pricing.

    Returns a tuple of (is_valid, reason).
    """

    try:
        from services.marketdata.pathing.env import load_env_config
        from services.marketdata.pathing.fallbacks import bridge_vvv_price
    except Exception:
        # If imports fail (e.g., during module import cycle), treat as unknown
        return True, "validation_unavailable"

    config = load_env_config()
    diem_addr = (config.diem_token or "").lower()
    if not diem_addr:
        return True, "missing_diem_token"

    tokens = [t.lower() for t in getattr(route, "tokens", [])]
    if diem_addr not in tokens:
        return True, "not_diem_route"

    bridge_price = bridge_vvv_price(config)
    if not bridge_price:
        return True, "bridge_price_unavailable"

    try:
        bridge = float(bridge_price)
    except Exception:
        return True, "bridge_price_invalid"

    if bridge <= 0:
        return True, "bridge_price_nonpositive"

    try:
        quoted_price = float(price)
    except Exception:
        return False, "quoted_price_invalid"

    if quoted_price <= 0:
        return False, "quoted_price_nonpositive"

    drift = abs(quoted_price - bridge) / bridge

    # Maximum allowable drift depends on token characteristics; default to 30%.
    max_drift = 0.30
    try:
        from services.marketdata.token_classifier import classify_token

        characteristics = classify_token(config.diem_token, bridge, None)
        max_drift = characteristics.max_drift_from_bridge
    except Exception:
        max_drift = 0.30

    if drift > max_drift:
        return False, f"drift_{drift * 100:.1f}pct_exceeds_{max_drift * 100:.0f}pct"

    return True, "valid"
