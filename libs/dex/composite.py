"""Composite route quoter for multi-venue routes.

This module provides helpers to quote and execute trades across multiple DEX venues
when a route spans different providers (e.g., V2 pair -> V3 pool).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from libs.dex.providers import DexAggregator, DexProvider, Quote
else:  # pragma: no cover - avoid circular import at runtime
    DexAggregator = Any  # type: ignore
    DexProvider = Any  # type: ignore
    Quote = Any  # type: ignore
from libs.dex.diem_fallbacks import (
    diem_vvv_quote_exact_in_from_reserves,
    diem_vvv_quote_from_reserves,
)
from libs.dex.routes import RoutePlan, _normalize_address, make_route

try:
    from libs.telemetry.logger import get_logger

    _logger = get_logger("dex.composite")
except Exception:
    import logging

    _logger = logging.getLogger("dex.composite")


def _debug_routes_enabled() -> bool:
    """Check if DIEM_DEBUG_ROUTES is enabled."""
    flag = os.getenv("DIEM_DEBUG_ROUTES")
    if flag is None:
        return False
    return str(flag).strip().lower() in {"1", "true", "yes", "on"}


def _is_diem_vvv_leg(leg: CompositeLeg) -> bool:
    """Check if a leg is the DIEM/VVV bridge leg."""
    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
    vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
    if not diem_addr or not vvv_addr:
        return False
    leg_in = leg.token_in.lower()
    leg_out = leg.token_out.lower()
    return (
        leg.provider in {"uniswap_v2", "aerodrome"}
        and diem_addr in {leg_in, leg_out}
        and vvv_addr in {leg_in, leg_out}
    )


try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:

    def _metrics_inc(
        name: str, value: int = 1, labels: dict[str, str] | None = None
    ) -> None:  # type: ignore
        return


@dataclass
class CompositeLeg:
    """Metadata for a single leg in a composite route."""

    token_in: str
    token_out: str
    provider: str  # e.g., "uniswap_v2", "uniswap_v3"
    pool_address: str | None = None
    fee: int | None = None  # For V3 pools


@dataclass
class CompositeQuote:
    """Quote result for a composite route."""

    amount_in: int
    amount_out: int
    legs: list[Quote]
    total_slippage_bps: float = 0.0
    provider: str = "composite"


# Minimum probe size for 18-decimal tokens (0.001 tokens = 10**15 base units)
# This is above the dust threshold for DIEM/USDC quotes (~4.58e9 base units)
_REFERENCE_PROBE_UNITS_18_DEC = 10**15  # 0.001 tokens for 18-decimal tokens

# Minimum probe size for 6-decimal tokens (1 token = 10**6 base units)
_REFERENCE_PROBE_UNITS_6_DEC = 10**6  # 1 token for 6-decimal tokens

# Legacy probe units for backward compatibility when decimals unknown
_REFERENCE_PROBE_UNITS = _REFERENCE_PROBE_UNITS_6_DEC


def _get_probe_units_for_decimals(decimals: int) -> int:
    """Get minimum probe units based on token decimals."""
    if decimals >= 18:
        return _REFERENCE_PROBE_UNITS_18_DEC
    if decimals >= 8:
        return 10 ** max(5, decimals - 3)  # 0.001 tokens
    return _REFERENCE_PROBE_UNITS_6_DEC


def _probe_amount(target: int, *, decimals: int | None = None) -> int:
    """Return a small probe amount to approximate spot price without price impact.

    Args:
        target: Desired trade amount in base units
        decimals: Decimals of the token. If None, uses legacy behavior (caps at 1M).

    Returns:
        A probe amount that is above dust thresholds but small enough for spot price estimation
    """
    if target <= 0:
        return 0

    # When decimals are provided, use decimal-aware minimum probe size
    if decimals is not None:
        min_probe = _get_probe_units_for_decimals(decimals)
        # Return the minimum probe size, but don't exceed target
        return min(max(min_probe, 1), int(target)) if target >= min_probe else min_probe

    # Legacy behavior: cap at reference units for backward compatibility
    return max(1, min(int(target), _REFERENCE_PROBE_UNITS))


def _slippage_bps(
    expected: float, actual: float, *, cost_basis: bool = False
) -> float | None:
    """
    Compute slippage in basis points given expected vs actual price/amount.

    Positive when execution is worse than expected. Returns None when not computable.
    cost_basis=True treats larger actual values as worse (e.g., more input required).
    """
    if expected <= 0 or actual <= 0:
        return None
    if cost_basis:
        if actual <= expected:
            return 0.0
        return float((actual - expected) / expected * 10_000.0)
    if actual >= expected:
        return 0.0
    return float((expected - actual) / expected * 10_000.0)


def _find_provider(aggregator: DexAggregator, provider_name: str) -> DexProvider | None:
    """Find a provider by name in the aggregator."""
    provider_name_lower = provider_name.lower()
    for provider in aggregator.providers:
        if provider.name.lower() == provider_name_lower:
            return provider
    return None


_STF_RE = re.compile(
    r"(?:\bstf\b|transfer_from_failed|safe\s*transfer\s*from\s*failed)",
    re.IGNORECASE,
)


def _is_stf_style_revert(exc: Exception) -> bool:
    """Return True when an exception looks like a Uniswap TransferHelper STF revert."""
    try:
        msg = str(exc or "")
    except Exception:
        return False
    if not msg:
        return False
    return bool(_STF_RE.search(msg))


def _preflight_allowance_low(
    provider: Any,
    route: RoutePlan,
    *,
    required: int,
) -> dict[str, Any]:
    """
    Best-effort allowance preflight for STF recovery.

    Returns a dict with:
    - status: "ok" | "low" | "unavailable"
    - token_in, owner, spender, allowance, required
    """
    token_in = ""
    owner = ""
    spender = ""
    allowance: int | None = None

    try:
        if route and getattr(route, "tokens", None):
            token_in = _normalize_address(str(route.tokens[0]))
    except Exception:
        token_in = ""

    try:
        spender = str(getattr(provider, "router_addr", "") or "").strip()
    except Exception:
        spender = ""

    try:
        from libs.agentkit_ext.agentkit_wallet import get_address

        owner = str(getattr(provider, "recipient", "") or get_address() or "").strip()
    except Exception:
        owner = str(getattr(provider, "recipient", "") or "").strip()

    if required <= 0 or not token_in or not owner or not spender:
        return {
            "status": "unavailable",
            "token_in": token_in,
            "owner": owner,
            "spender": spender,
            "allowance": allowance,
            "required": int(required),
        }

    try:
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        erc20 = get_contract(w3, token_in, "erc20.json")
        allowance = int(erc20.functions.allowance(owner, spender).call())
    except Exception as exc:
        return {
            "status": "unavailable",
            "token_in": token_in,
            "owner": owner,
            "spender": spender,
            "allowance": allowance,
            "required": int(required),
            "error": str(exc),
        }

    status = "ok" if allowance >= int(required) else "low"
    return {
        "status": status,
        "token_in": token_in,
        "owner": owner,
        "spender": spender,
        "allowance": int(allowance),
        "required": int(required),
    }


def execute_with_uniswap_v3_stf_retry(
    *,
    provider: Any,
    route: RoutePlan,
    required_allowance: int,
    attempt: Callable[[], Any],
    correlation_id: str | None = None,
    retry_state: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """
    Execute a composite leg with a single STF recovery retry for UniswapV3.

    If the first attempt fails with an STF-style revert and allowance preflight
    shows allowance < required_allowance, inject an approval and retry once.
    """
    info: dict[str, Any] = {
        "correlation_id": correlation_id,
        "attempts": 0,
        "retried": False,
        "retry_reason": None,
        "approval_tx": None,
        "preflight": None,
    }

    provider_name = ""
    try:
        provider_name = str(getattr(provider, "name", "") or "").strip().lower()
    except Exception:
        provider_name = ""

    def _mark_retry_used() -> None:
        if retry_state is not None:
            retry_state["stf_retry_used"] = True

    def _retry_already_used() -> bool:
        if retry_state is None:
            return False
        return bool(retry_state.get("stf_retry_used"))

    try:
        info["attempts"] = 1
        return attempt(), info
    except Exception as exc:
        if provider_name != "uniswap_v3" or not _is_stf_style_revert(exc):
            raise

        # Classify early for logs/diagnostics.
        try:
            _logger.warning(
                "Composite leg UniswapV3 STF-style revert (likely allowance/transfer)",
                extra={
                    "provider": provider_name,
                    "route": list(getattr(route, "tokens", []) or []),
                    "required_allowance": int(required_allowance),
                    "correlation_id": correlation_id,
                    "error": str(exc),
                },
            )
        except Exception:
            pass

        if _retry_already_used():
            raise

        preflight = _preflight_allowance_low(
            provider, route, required=int(required_allowance)
        )
        info["preflight"] = preflight
        if preflight.get("status") != "low":
            raise

        # Inject approval using the provider's allowance helper when available.
        approval_tx: str | None = None
        try:
            ensure_allowance = getattr(provider, "_ensure_allowance", None)
            if callable(ensure_allowance):
                approval_tx = ensure_allowance(
                    preflight.get("token_in"),
                    preflight.get("owner"),
                    preflight.get("spender"),
                    int(preflight.get("required") or required_allowance or 0),
                )
        except Exception as approval_exc:
            try:
                _logger.warning(
                    "Composite STF retry approval injection failed",
                    extra={
                        "provider": provider_name,
                        "route": list(getattr(route, "tokens", []) or []),
                        "correlation_id": correlation_id,
                        "error": str(approval_exc),
                    },
                )
            except Exception:
                pass
            raise

        info["approval_tx"] = approval_tx
        info["retried"] = True
        info["retry_reason"] = "stf_allowance_low"
        _mark_retry_used()
        try:
            _metrics_inc(
                "dex_composite_stf_retry_total",
                labels={"provider": provider_name, "stage": "retry"},
            )
        except Exception:
            pass
        try:
            _logger.info(
                "Composite STF retry: approval injected, retrying UniswapV3 leg once",
                extra={
                    "provider": provider_name,
                    "route": list(getattr(route, "tokens", []) or []),
                    "approval_tx": approval_tx,
                    "correlation_id": correlation_id,
                },
            )
        except Exception:
            pass

        try:
            info["attempts"] = 2
            res = attempt()
            try:
                _logger.info(
                    "Composite STF retry succeeded",
                    extra={
                        "provider": provider_name,
                        "route": list(getattr(route, "tokens", []) or []),
                        "approval_tx": approval_tx,
                        "correlation_id": correlation_id,
                    },
                )
            except Exception:
                pass
            return res, info
        except Exception as exc2:
            try:
                _logger.warning(
                    "Composite STF retry failed",
                    extra={
                        "provider": provider_name,
                        "route": list(getattr(route, "tokens", []) or []),
                        "approval_tx": approval_tx,
                        "correlation_id": correlation_id,
                        "error": str(exc2),
                    },
                )
            except Exception:
                pass
            raise exc2 from exc


def attach_composite_metadata(
    route: RoutePlan,
    *,
    bridge_legs: list[dict[str, Any]] | None = None,
    is_composite: bool | None = None,
) -> RoutePlan:
    """
    Safely attach composite metadata to a RoutePlan.

    RoutePlan is frozen, so we use object.__setattr__ to store optional metadata.
    """
    if bridge_legs is not None:
        object.__setattr__(route, "_bridge_legs", bridge_legs)
    if is_composite is not None:
        object.__setattr__(route, "_is_composite", bool(is_composite))
    return route


def get_composite_bridge_legs(route: RoutePlan) -> list[dict[str, Any]] | None:
    """Return bridge leg metadata if present."""
    return getattr(route, "_bridge_legs", None)


def _normalize_token(token: Any) -> str:
    """
    Normalize a token identifier so address-style and symbol-style inputs align.

    RoutePlan tokens are already normalized with a 0x prefix and lowercase.
    Bridge metadata may omit the prefix or casing, so we mirror the RoutePlan normalization
    while tolerating non-address symbol strings used in tests.
    """
    token_str = str(token or "").strip()
    if not token_str:
        return ""
    try:
        return _normalize_address(token_str)
    except Exception:
        # Best-effort fallback: lowercase and ensure 0x prefix
        token_str = token_str.lower()
        return token_str if token_str.startswith("0x") else f"0x{token_str}"


def _align_legs_to_route(
    route: RoutePlan,
    bridge_legs: list[dict[str, Any]],
) -> list[CompositeLeg] | None:
    """
    Align bridge leg metadata to route segments by matching token pairs.

    This ensures that when routes are reversed (e.g., for buy-side quotes),
    the correct pool/provider is matched to each route segment regardless
    of the original metadata direction.

    Args:
        route: Route plan with token sequence
        bridge_legs: Bridge leg metadata (may be in forward direction)

    Returns:
        List of CompositeLeg objects aligned to route segments, or None if alignment fails
    """
    tokens = route.tokens
    if len(tokens) < 2:
        return None

    aligned: list[CompositeLeg] = []
    # Map available legs by token pair for lookup (both directions)
    available_legs: dict[tuple[str, str], dict[str, Any]] = {}
    for leg_data in bridge_legs:
        t_in = _normalize_token(leg_data.get("token_in", ""))
        t_out = _normalize_token(leg_data.get("token_out", ""))
        if not t_in or not t_out:
            continue
        # Store both directions so we can match regardless of route direction
        available_legs[(t_in, t_out)] = leg_data
        available_legs[(t_out, t_in)] = leg_data

    # Match route segments to available legs
    for i in range(len(tokens) - 1):
        t_curr = _normalize_token(tokens[i])
        t_next = _normalize_token(tokens[i + 1])

        match = available_legs.get((t_curr, t_next))
        if not match:
            _logger.debug(
                f"Could not align leg {i}: no metadata for {t_curr} -> {t_next}"
            )
            return None

        # Create leg with correct direction matching the route
        aligned.append(
            CompositeLeg(
                token_in=tokens[i],  # Use route's actual token addresses
                token_out=tokens[i + 1],
                provider=str(match.get("provider", "uniswap_v2")),
                pool_address=match.get("pool_address"),
                fee=match.get("fee"),
            )
        )

    return aligned


def _quote_leg(
    provider: DexProvider,
    leg: CompositeLeg,
    amount_in: int,
    *,
    mode: str = "exact_in",
    aggregator: DexAggregator | None = None,
) -> Quote | None:
    """Quote a single leg of a composite route."""
    original_stable: bool | None = None
    try:
        # Allow per-leg stable override for Aerodrome legs.
        if provider.name.lower() == "aerodrome":
            leg_stable = None
            try:
                leg_stable = leg.stable
            except Exception:
                pass
            if leg_stable is None and isinstance(leg, dict):
                try:
                    leg_stable = leg.get("stable")
                except Exception:
                    leg_stable = None
            if leg_stable is not None:
                try:
                    original_stable = bool(provider.stable)
                except Exception:
                    original_stable = None
                try:
                    provider.stable = bool(leg_stable)  # type: ignore[attr-defined]
                except Exception:
                    pass
        # Respect V3 fee tiers when provided or fall back to provider defaults.
        fees = None
        if leg.fee is not None:
            fees = [leg.fee]
        elif hasattr(provider, "default_fee"):
            try:
                default_fee = provider.default_fee
                if default_fee not in (None, ""):
                    fees = [int(default_fee)]
                else:
                    env_default = os.getenv("UNISWAP_V3_DEFAULT_FEE")
                    if env_default:
                        fees = [int(env_default)]
            except Exception:
                fees = None
        else:
            env_default = os.getenv("UNISWAP_V3_DEFAULT_FEE")
            if env_default:
                try:
                    fees = [int(env_default)]
                except Exception:
                    fees = None

        if fees:
            route = make_route([leg.token_in, leg.token_out], fees)
        else:
            route = make_route([leg.token_in, leg.token_out])

        if mode == "exact_out":
            if hasattr(provider, "quote_exact_out"):
                quote = provider.quote_exact_out(amount_in, route)

                # If quote failed and this is DIEM/VVV leg, try analytic preview fallback.
                if quote is None and _is_diem_vvv_leg(leg):
                    analytic_enabled = os.getenv(
                        "DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE", ""
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if analytic_enabled:
                        analytic_quote = _try_analytic_preview_diem_vvv(
                            leg, amount_in, aggregator
                        )
                        if analytic_quote:
                            _logger.debug(
                                f"Using analytic preview for DIEM/VVV leg (exact_out): "
                                f"{leg.token_in} -> {leg.token_out}"
                            )
                            return analytic_quote
                    # Fall back to pair‑math exact‑out using DIEM/VVV reserves when enabled.
                    try:
                        pair_quote = diem_vvv_quote_from_reserves(
                            amount_in, leg.token_in, leg.token_out
                        )
                    except Exception:
                        pair_quote = None
                    if pair_quote is not None:
                        try:
                            _metrics_inc(
                                "dex_composite_fallback_used_total",
                                labels={"leg": "diem_vvv", "mode": "exact_out"},
                            )
                        except Exception:
                            pass
                        return pair_quote

                # Generic analytic preview for non‑DIEM legs when router exact‑out is unavailable.
                # This uses an exact‑in quote to approximate the required input for the desired output.
                if (
                    quote is None
                    and aggregator is not None
                    and not _is_diem_vvv_leg(leg)
                ):
                    analytic_enabled = os.getenv(
                        "DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE", ""
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if analytic_enabled:
                        try:
                            from libs.dex.providers import Quote as QuoteType

                            # Use a fixed probe size to estimate price (exact‑in).
                            probe_in = 10**18
                            base_quote = aggregator.best_quote(probe_in, route)
                            if base_quote and base_quote.amount_out > 0:
                                # Sanity check: require meaningful output ratio to prevent overflow.
                                # If amount_out is too small relative to amount_in, skip this fallback.
                                min_output_ratio = 1e-12  # Minimum output/input ratio
                                output_ratio = float(base_quote.amount_out) / max(
                                    1.0, float(base_quote.amount_in)
                                )
                                if output_ratio < min_output_ratio:
                                    _logger.debug(
                                        f"Analytic fallback skipped: output ratio too small ({output_ratio:.2e})"
                                    )
                                else:
                                    # amount_in_needed ≈ amount_out_desired * (probe_in / base_out)
                                    # Use float to avoid integer overflow, then clamp to reasonable bounds.
                                    approx_in_float = (
                                        float(amount_in)
                                        * float(base_quote.amount_in)
                                        / max(1.0, float(base_quote.amount_out))
                                        * 1.02
                                    )
                                    # Cap at 2^128 to prevent overflow (uint256 max is 2^256-1)
                                    max_sane_amount = 2**128
                                    if approx_in_float > max_sane_amount:
                                        _logger.debug(
                                            f"Analytic fallback skipped: approx_in overflow ({approx_in_float:.2e})"
                                        )
                                    else:
                                        approx_in = int(approx_in_float)
                                        if approx_in > 0:
                                            return QuoteType(
                                                provider="composite_analytic",
                                                amount_in=approx_in,
                                                amount_out=amount_in,
                                                route=route,
                                            )
                        except Exception:
                            # Analytic preview is best‑effort; fall through to None.
                            pass

                return quote
            return None
        # Exact-in mode
        quote = provider.quote(amount_in, route)

        # Fallback for DIEM/VVV leg in exact-in mode if router fails
        if quote is None and _is_diem_vvv_leg(leg):
            try:
                fallback_quote = diem_vvv_quote_exact_in_from_reserves(
                    amount_in, leg.token_in, leg.token_out
                )
                if fallback_quote:
                    _logger.debug(
                        f"Using pair math fallback for DIEM/VVV leg (exact_in): "
                        f"{leg.token_in} -> {leg.token_out}"
                    )
                    try:
                        _metrics_inc(
                            "dex_composite_fallback_used_total",
                            labels={"leg": "diem_vvv", "mode": "exact_in"},
                        )
                    except Exception:
                        pass
                    return fallback_quote
            except Exception as exc:
                _logger.debug(f"DIEM exact-in fallback failed: {exc}")

        return quote
    except Exception as exc:
        _logger.debug(f"Leg quote failed: {exc}")
        return None
    finally:
        # Restore original stable setting for Aerodrome provider
        if original_stable is not None and provider.name.lower() == "aerodrome":
            try:
                provider.stable = original_stable  # type: ignore[attr-defined]
            except Exception:
                pass


def _estimate_reference_out_exact_in(
    aggregator: DexAggregator | None,
    legs: list[CompositeLeg],
    total_in: int,
) -> float | None:
    """
    Estimate expected output per unit (spot) for a composite exact-in route.

    Uses a small probe size on each leg to approximate spot price and scales
    linearly to the full input. Returns the expected out amount for the full input.
    """
    if aggregator is None or not legs or total_in <= 0:
        return None

    multiplier = 1.0
    probe = _probe_amount(total_in)

    current_amount = probe
    for leg in legs:
        provider = _find_provider(aggregator, leg.provider)
        if provider is None or current_amount <= 0:
            return None
        ref_quote = _quote_leg(
            provider, leg, current_amount, mode="exact_in", aggregator=aggregator
        )
        if ref_quote is None or ref_quote.amount_out <= 0 or ref_quote.amount_in <= 0:
            return None
        ratio = ref_quote.amount_out / float(ref_quote.amount_in)
        multiplier *= ratio
        current_amount = ref_quote.amount_out

    return float(total_in) * multiplier


def _estimate_reference_in_exact_out(
    aggregator: DexAggregator | None,
    reversed_legs: list[CompositeLeg],
    amount_out: int,
) -> float | None:
    """
    Estimate expected input per unit output (spot) for composite exact-out routes.

    Quotes a small exact-out amount on each reversed leg to approximate cost per unit.
    Returns expected input required for 1 unit out.
    """
    if aggregator is None or not reversed_legs or amount_out <= 0:
        return None

    probe = _probe_amount(amount_out)
    if probe <= 0:
        return None

    cost_multiplier = 1.0
    current_amount_out = probe

    for leg in reversed_legs:
        provider = _find_provider(aggregator, leg.provider)
        if provider is None or current_amount_out <= 0:
            return None
        ref_quote = _quote_leg(
            provider,
            leg,
            current_amount_out,
            mode="exact_out",
            aggregator=aggregator,
        )
        if ref_quote is None or ref_quote.amount_out <= 0 or ref_quote.amount_in <= 0:
            return None

        ratio = ref_quote.amount_in / float(ref_quote.amount_out)
        cost_multiplier *= ratio
        current_amount_out = ref_quote.amount_in

    return float(amount_out) * cost_multiplier


def _try_analytic_preview_diem_vvv(
    leg: CompositeLeg,
    amount_out: int,
    aggregator: DexAggregator | None,
) -> Quote | None:
    """
    Try to compute a conservative analytic preview for DIEM/VVV leg using reserves.

    This is preview-only and should never be used for actual trade execution.
    """
    if amount_out <= 0:
        return None

    try:
        from services.marketdata import etherscan_verify as es

        pair_addr = leg.pool_address
        if not pair_addr:
            return None

        # Get reserves
        discovery = es.verify_trade_path([leg.token_in, leg.token_out])
        hops = discovery.get("hops") or []
        if not hops:
            return None

        uni = (hops[0] or {}).get("uniswap_v2") or {}
        reserves = uni.get("reserves")
        if not isinstance(reserves, (tuple, list)) or len(reserves) < 2:
            return None

        reserve_in = int(reserves[0])
        reserve_out = int(reserves[1])

        # Determine which reserve corresponds to token_in vs token_out
        # For exact-out: we want amount_out of token_out, need amount_in of token_in
        # Standard Uniswap V2 formula: amount_in = (reserve_in * amount_out) / (reserve_out - amount_out)
        # Add 0.3% fee: amount_in = (reserve_in * amount_out * 1000) / ((reserve_out - amount_out) * 997)
        if reserve_out <= amount_out:
            return None  # Insufficient liquidity

        # Conservative calculation with fee
        numerator = reserve_in * amount_out * 1000
        denominator = (reserve_out - amount_out) * 997
        if denominator <= 0:
            return None

        amount_in_approx = numerator // denominator
        if amount_in_approx <= 0:
            return None

        # Create a synthetic quote (preview only)
        # Import Quote type at runtime to avoid circular imports
        from libs.dex.providers import Quote as QuoteType

        leg_route = make_route([leg.token_in, leg.token_out])
        return QuoteType(
            provider="composite_analytic",
            amount_in=amount_in_approx,
            amount_out=amount_out,
            route=leg_route,
        )
    except Exception:
        return None


def quote_composite_exact_in(
    aggregator: DexAggregator,
    route: RoutePlan,
    amount_in: int,
    *,
    bridge_legs: list[dict[str, Any]] | None = None,
) -> CompositeQuote | None:
    """
    Quote a composite route using exact-in mode.

    Args:
        aggregator: DEX aggregator with configured providers
        route: Route plan spanning multiple tokens
        amount_in: Input amount (base units)
        bridge_legs: Optional leg metadata from bridge path

    Returns:
        CompositeQuote with per-leg quotes, or None if any leg fails
    """
    if not bridge_legs:
        # Try to extract from route metadata
        bridge_legs = get_composite_bridge_legs(route)

    if not bridge_legs:
        return None

    tokens = route.tokens
    if len(tokens) < 2:
        return None

    # Align bridge legs to route segments (handles direction mismatches)
    legs = _align_legs_to_route(route, bridge_legs)
    if not legs:
        _logger.warning(
            f"Failed to align bridge legs to route: {len(bridge_legs)} legs for {len(tokens) - 1} hops"
        )
        return None

    if len(legs) != len(tokens) - 1:
        _logger.warning(
            f"Leg count mismatch after alignment: {len(legs)} legs for {len(tokens) - 1} hops"
        )
        return None

    leg_quotes: list[Quote] = []
    current_amount = amount_in

    for idx, leg in enumerate(legs):
        provider = _find_provider(aggregator, leg.provider)
        if not provider:
            _logger.warning(f"Provider {leg.provider} not found for leg {idx}")
            try:
                _metrics_inc(
                    "dex_composite_leg_failed_total",
                    labels={"reason": "provider_not_found", "leg_index": str(idx)},
                )
            except Exception:
                pass
            return None

        quote = _quote_leg(
            provider, leg, current_amount, mode="exact_in", aggregator=aggregator
        )
        if not quote:
            error_msg = f"Leg {idx} quote failed: {leg.token_in} -> {leg.token_out} via {leg.provider}"
            # Downgrade to DEBUG for known low-liquidity scenarios (VVV/USDC V3 pool)
            vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
            usdc_addr = (
                (
                    os.getenv("USDC_TOKEN_ADDRESS")
                    or "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
                )
                .strip()
                .lower()
            )
            is_vvv_usdc_leg = (
                (
                    (
                        leg.token_in.lower() == vvv_addr
                        and leg.token_out.lower() == usdc_addr
                    )
                    or (
                        leg.token_in.lower() == usdc_addr
                        and leg.token_out.lower() == vvv_addr
                    )
                )
                if vvv_addr
                else False
            )
            if is_vvv_usdc_leg and leg.provider.lower() == "uniswap_v3":
                _logger.debug("%s (expected: VVV/USDC V3 low liquidity)", error_msg)
            else:
                _logger.warning(error_msg)

            # Enhanced diagnostics for DIEM routes
            try:
                from libs.dex.diagnostics import log_event as _dex_diag_log_event

                diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                is_diem_leg = (
                    leg.token_in.lower() == diem_addr
                    or leg.token_out.lower() == diem_addr
                    or leg.token_in.lower() == vvv_addr
                    or leg.token_out.lower() == vvv_addr
                )

                if is_diem_leg:
                    _dex_diag_log_event(
                        {
                            "event": "dex_composite_leg_failed",
                            "leg_index": idx,
                            "token_in": leg.token_in,
                            "token_out": leg.token_out,
                            "provider": leg.provider,
                            "pool_address": leg.pool_address,
                            "amount": current_amount,
                            "mode": "exact_in",
                            "diem_leg": True,
                        }
                    )
            except Exception:
                pass

            try:
                _metrics_inc(
                    "dex_composite_leg_failed_total",
                    labels={
                        "reason": "quote_failed",
                        "leg_index": str(idx),
                        "provider": leg.provider,
                    },
                )
            except Exception:
                pass
            return None

        leg_quotes.append(quote)
        current_amount = quote.amount_out
        try:
            _metrics_inc(
                "dex_composite_leg_quoted_total",
                labels={"provider": leg.provider, "leg_index": str(idx)},
            )
        except Exception:
            pass

    if not leg_quotes:
        return None

    total_in = leg_quotes[0].amount_in
    total_out = leg_quotes[-1].amount_out

    # Calculate aggregate slippage using spot reference derived from small-probe leg quotes
    total_slippage_bps = 0.0
    try:
        expected_out = _estimate_reference_out_exact_in(
            aggregator, legs, total_in=total_in
        )
        slip_val = (
            _slippage_bps(expected_out, float(total_out)) if expected_out else 0.0
        )
        if slip_val is not None:
            total_slippage_bps = float(slip_val)
    except Exception:
        total_slippage_bps = 0.0

    try:
        _metrics_inc(
            "dex_composite_quote_success_total",
            labels={"mode": "exact_in", "leg_count": str(len(leg_quotes))},
        )
    except Exception:
        pass

    return CompositeQuote(
        amount_in=total_in,
        amount_out=total_out,
        legs=leg_quotes,
        total_slippage_bps=total_slippage_bps,
        provider="composite",
    )


def quote_composite_exact_out(
    aggregator: DexAggregator,
    route: RoutePlan,
    amount_out: int,
    *,
    bridge_legs: list[dict[str, Any]] | None = None,
) -> CompositeQuote | None:
    """
    Quote a composite route using exact-out mode (reverse direction).

    Args:
        aggregator: DEX aggregator with configured providers
        route: Route plan spanning multiple tokens
        amount_out: Desired output amount (base units)
        bridge_legs: Optional leg metadata from bridge path

    Returns:
        CompositeQuote with per-leg quotes, or None if any leg fails
    """
    if not bridge_legs:
        # Try to extract from route metadata
        bridge_legs = get_composite_bridge_legs(route)

    if not bridge_legs:
        return None

    tokens = route.tokens
    if len(tokens) < 2:
        return None

    # Align bridge legs to route segments (handles direction mismatches)
    legs = _align_legs_to_route(route, bridge_legs)
    if not legs:
        _logger.warning(
            f"Failed to align bridge legs to route: {len(bridge_legs)} legs for {len(tokens) - 1} hops"
        )
        return None

    if len(legs) != len(tokens) - 1:
        _logger.warning(
            f"Leg count mismatch after alignment: {len(legs)} legs for {len(tokens) - 1} hops"
        )
        return None

    # Work backwards from the desired output while keeping the original swap direction.
    # We only reverse the leg order; we do NOT swap token_in/token_out, otherwise the
    # amount_out passed to quote_exact_out would no longer match the route's output token
    # and Uniswap V3 exact-out calls revert (observed as repeated leg-1 failures for
    # VVV->USDC).
    reversed_legs: list[CompositeLeg] = list(reversed(legs))
    leg_quotes: list[Quote] = []
    current_amount = amount_out

    for idx, leg in enumerate(reversed_legs):
        provider = _find_provider(aggregator, leg.provider)
        if not provider:
            _logger.warning(f"Provider {leg.provider} not found for leg {idx}")
            try:
                _metrics_inc(
                    "dex_composite_leg_failed_total",
                    labels={
                        "reason": "provider_not_found",
                        "leg_index": str(idx),
                        "mode": "exact_out",
                    },
                )
            except Exception:
                pass
            return None

        quote = _quote_leg(
            provider, leg, current_amount, mode="exact_out", aggregator=aggregator
        )
        if not quote:
            # Log correct direction: token_in -> token_out (what we're actually quoting)
            pool_info = f", pool={leg.pool_address}" if leg.pool_address else ""
            error_msg = (
                f"Leg {idx} exact-out quote failed: {leg.token_in} -> {leg.token_out} "
                f"(provider={leg.provider}, mode=exact_out{pool_info})"
            )
            # Downgrade to DEBUG for known low-liquidity scenarios (VVV/USDC V3 pool)
            vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
            usdc_addr = (
                (
                    os.getenv("USDC_TOKEN_ADDRESS")
                    or "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
                )
                .strip()
                .lower()
            )
            is_vvv_usdc_leg = (
                (
                    (
                        leg.token_in.lower() == vvv_addr
                        and leg.token_out.lower() == usdc_addr
                    )
                    or (
                        leg.token_in.lower() == usdc_addr
                        and leg.token_out.lower() == vvv_addr
                    )
                )
                if vvv_addr
                else False
            )
            if is_vvv_usdc_leg and leg.provider.lower() == "uniswap_v3":
                _logger.debug("%s (expected: VVV/USDC V3 low liquidity)", error_msg)
            else:
                _logger.warning(error_msg)

            # Enrich metrics with provider and token info (within cardinality limits)
            metric_labels = {
                "reason": "quote_failed",
                "leg_index": str(idx),
                "mode": "exact_out",
                "provider": leg.provider,
            }
            # Only include token addresses if they're short (avoid high cardinality)
            token_in_short = (
                leg.token_in[-8:] if len(leg.token_in) > 8 else leg.token_in
            )
            token_out_short = (
                leg.token_out[-8:] if len(leg.token_out) > 8 else leg.token_out
            )
            metric_labels["token_in"] = token_in_short
            metric_labels["token_out"] = token_out_short

            try:
                _metrics_inc(
                    "dex_composite_leg_failed_total",
                    labels=metric_labels,
                )
            except Exception:
                pass

            # Debug logging when DIEM_DEBUG_ROUTES is enabled
            if _debug_routes_enabled():
                _logger.info(
                    f"Composite exact-out leg failure details: leg={idx}, "
                    f"amount_out={current_amount}, route_tokens={list(route.tokens)}, "
                    f"leg_tokens=[{leg.token_in}, {leg.token_out}]"
                )

                # Try to inspect the leg route if aggregator supports it
                try:
                    if hasattr(aggregator, "_inspect_provider_route"):
                        leg_route = make_route([leg.token_in, leg.token_out])
                        inspection = aggregator._inspect_provider_route(
                            provider, leg_route, current_amount, mode="exact_out"
                        )
                        if inspection:
                            _logger.info(
                                f"Leg {idx} route inspection: {inspection}",
                                extra={"dex_composite_leg_inspect": inspection},
                            )
                except Exception:
                    pass  # Inspection is best-effort

            # Enhanced diagnostics for DIEM routes
            try:
                from libs.dex.diagnostics import log_event as _dex_diag_log_event

                diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                is_diem_leg = (
                    leg.token_in.lower() == diem_addr
                    or leg.token_out.lower() == diem_addr
                    or leg.token_in.lower() == vvv_addr
                    or leg.token_out.lower() == vvv_addr
                )

                if is_diem_leg:
                    _dex_diag_log_event(
                        {
                            "event": "dex_composite_leg_failed",
                            "leg_index": idx,
                            "token_in": leg.token_in,
                            "token_out": leg.token_out,
                            "provider": leg.provider,
                            "pool_address": leg.pool_address,
                            "amount_out": current_amount,
                            "mode": "exact_out",
                            "diem_leg": True,
                            "partial_quotes": len(leg_quotes),
                            "total_legs": len(reversed_legs),
                        }
                    )
            except Exception:
                pass

            # Special handling for DIEM/VVV bridge leg failures
            if _is_diem_vvv_leg(leg):
                # Determine failure reason based on inspection if available
                failure_reason = "router_error"
                try:
                    if hasattr(aggregator, "_inspect_provider_route"):
                        leg_route = make_route([leg.token_in, leg.token_out])
                        inspection = aggregator._inspect_provider_route(
                            provider, leg_route, current_amount, mode="exact_out"
                        )
                        if inspection:
                            status = inspection.get("status", "")
                            if status == "ok":
                                result = inspection.get("result", [])
                                if (
                                    isinstance(result, (list, tuple))
                                    and len(result) > 0
                                ):
                                    if int(result[0] or 0) <= 0:
                                        failure_reason = "zero_amount"
                            elif status == "error":
                                error_msg = inspection.get("error", "")
                                if "normalize" in str(error_msg).lower():
                                    failure_reason = "normalization_error"
                except Exception:
                    pass

                # Log DIEM-specific warning with reserves info if available
                reserves_info = ""
                try:
                    from services.marketdata import etherscan_verify as es

                    pair_addr = leg.pool_address
                    if pair_addr:
                        discovery = es.verify_trade_path([leg.token_in, leg.token_out])
                        hops = discovery.get("hops") or []
                        if hops:
                            uni = (hops[0] or {}).get("uniswap_v2") or {}
                            reserves = uni.get("reserves")
                            if (
                                isinstance(reserves, (tuple, list))
                                and len(reserves) >= 2
                            ):
                                reserves_info = (
                                    f", reserves=[{reserves[0]}, {reserves[1]}]"
                                )
                except Exception:
                    pass  # Reserves check is optional

                _logger.warning(
                    f"DIEM/VVV bridge leg exact-out quote failed: {leg.token_in} -> "
                    f"{leg.token_out}, amount_out={current_amount}{reserves_info}, "
                    f"reason={failure_reason}"
                )

                # Emit dedicated metric for DIEM bridge failures
                try:
                    _metrics_inc(
                        "dex_composite_diem_bridge_fail_total",
                        labels={
                            "reason": failure_reason,
                            "leg_index": str(idx),
                            "mode": "exact_out",
                        },
                    )
                except Exception:
                    pass

            return None

        leg_quotes.insert(0, quote)  # Insert at beginning to maintain order
        current_amount = quote.amount_in
        try:
            _metrics_inc(
                "dex_composite_leg_quoted_total",
                labels={
                    "provider": leg.provider,
                    "leg_index": str(idx),
                    "mode": "exact_out",
                },
            )
        except Exception:
            pass

    if not leg_quotes:
        return None

    total_in = leg_quotes[0].amount_in
    total_out = leg_quotes[-1].amount_out

    total_slippage_bps = 0.0
    try:
        expected_in = _estimate_reference_in_exact_out(
            aggregator, reversed_legs, amount_out=total_out
        )
        slip_val = (
            _slippage_bps(expected_in, float(total_in), cost_basis=True)
            if expected_in
            else 0.0
        )
        if slip_val is not None:
            total_slippage_bps = float(slip_val)
    except Exception:
        total_slippage_bps = 0.0

    try:
        _metrics_inc(
            "dex_composite_quote_success_total",
            labels={"mode": "exact_out", "leg_count": str(len(leg_quotes))},
        )
    except Exception:
        pass

    return CompositeQuote(
        amount_in=total_in,
        amount_out=total_out,
        legs=leg_quotes,
        total_slippage_bps=total_slippage_bps,
        provider="composite",
    )


def is_composite_route(route: RoutePlan) -> bool:
    """Check if a route is a composite multi-venue route."""
    # Explicit flag wins to support partial bridge metadata used in tests/simulations.
    if getattr(route, "_is_composite", False):
        return True
    bridge_legs = get_composite_bridge_legs(route)
    if bridge_legs:
        # Only consider composite if route token count matches leg count
        # A route with N tokens has N-1 hops, which should match len(bridge_legs)
        expected_hops = len(bridge_legs)
        actual_hops = len(route.tokens) - 1
        if actual_hops != expected_hops:
            # Route structure doesn't match bridge legs - not a valid composite route
            return False
        return True
    return False


__all__ = [
    "CompositeLeg",
    "CompositeQuote",
    "attach_composite_metadata",
    "execute_with_uniswap_v3_stf_retry",
    "get_composite_bridge_legs",
    "is_composite_route",
    "quote_composite_exact_in",
    "quote_composite_exact_out",
]
