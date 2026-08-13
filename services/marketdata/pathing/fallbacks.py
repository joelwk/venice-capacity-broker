from __future__ import annotations

import os
import time
from collections.abc import Callable
from threading import Lock
from typing import Any

import requests

from libs.dex.diem_fallbacks import slot0_cache_fetch

from .env import EnvConfig
from .models import GuardrailContext, PolicyContext, QuoteMode, QuoteResult

# Import logger
try:
    from libs.telemetry.logger import get_logger

    _logger = get_logger("marketdata.fallbacks")
except Exception:
    import logging

    _logger = logging.getLogger("marketdata.fallbacks")

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:

    def _metrics_inc(
        name: str, value: int = 1, labels: dict[str, str] | None = None
    ) -> None:  # type: ignore
        return


def _valid_price(value: float | None) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v > 0 and v < 1e6


def _normalize(addr: str | None) -> str:
    if not addr:
        return ""
    value = addr.strip()
    if not value:
        return ""
    if value.startswith("0x"):
        return value.lower()
    return "0x" + value.lower()


def _erc20_decimals_from_env(addr: str) -> int:
    norm = _normalize(addr)
    if not norm:
        return 18
    if norm == _normalize(os.getenv("QUOTE_TOKEN_ADDRESS")):
        return int((os.getenv("QUOTE_TOKEN_DECIMALS") or "6").strip() or 6)
    if norm == _normalize(os.getenv("DIEM_TOKEN_ADDRESS")):
        return int((os.getenv("DIEM_DECIMALS") or "18").strip() or 18)
    if norm == _normalize(os.getenv("VVV_TOKEN_ADDRESS")):
        return int((os.getenv("VVV_DECIMALS") or "18").strip() or 18)
    try:
        from web3 import Web3  # type: ignore

        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        erc20 = get_contract(w3, Web3.to_checksum_address(norm), "erc20.json")
        return int(erc20.functions.decimals().call())
    except Exception:
        return 18


def _rpc_url() -> str | None:
    """Get RPC URL with multi-RPC rotation support."""
    try:
        from libs.agentkit_ext.web3_utils import resolve_rpc_url  # type: ignore

        # Use rotation-aware resolver (supports BASE_RPC_URLS)
        return resolve_rpc_url(validate=False)
    except Exception:
        # Fallback to single RPC URL
        return os.getenv("BASE_RPC_URL")


_RESERVE_CACHE_LOCK = Lock()
_RESERVE_CACHE: dict[str, tuple[float, dict[str, int | str]]] = {}

# Bridge price cache for DIEM (short-lived TTL to reduce DEX calls during incidents)
_BRIDGE_PRICE_CACHE_LOCK = Lock()
_BRIDGE_PRICE_LOCK = Lock()
# Cache stores (price, timestamp) per key
_BRIDGE_PRICE_CACHE: dict[str, tuple[float, float]] = {}


def _reserve_cache_ttl() -> float:
    raw = os.getenv("DIEM_VVV_RESERVE_CACHE_TTL", "").strip()
    if not raw:
        # Shorter default TTL as per plan: reduce stale cache window
        return 60.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


def _reserve_cache_stale_multiplier() -> float:
    raw = os.getenv("DIEM_VVV_RESERVE_STALE_MULT", "").strip()
    if not raw:
        # Reduced stale multiplier: shorter window for stale cache
        return 4.0
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 4.0


def _reserve_cache_get(
    key: str, ttl: float, *, allow_stale: bool = False
) -> tuple[dict[str, int | str], float] | None:
    if ttl <= 0 and not allow_stale:
        return None
    now = time.monotonic()
    with _RESERVE_CACHE_LOCK:
        entry = _RESERVE_CACHE.get(key)
    if not entry:
        return None
    cached_ts, payload = entry
    age = now - cached_ts
    if allow_stale or age <= ttl:
        return dict(payload), age
    return None


def _reserve_cache_set(key: str, payload: dict[str, int | str]) -> None:
    with _RESERVE_CACHE_LOCK:
        _RESERVE_CACHE[key] = (time.monotonic(), dict(payload))


def _get_vvv_price_usd(config: EnvConfig, rpc_url: str) -> tuple[float, str] | None:
    """
    Get VVV price in USD with multiple fallback methods.
    Returns (price, method) tuple or None.
    """
    vvv_addr = _normalize(config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or "")
    quote_addr = _normalize(
        config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or ""
    )
    pool_addr = config.vvv_usdc_pool

    if not vvv_addr or not quote_addr:
        _logger.warning("bridge_vvv: Missing VVV or quote token address")
        return None

    def _call(address: str, selector: str, rpc: str | None = None) -> bytes:
        """Make RPC call with retry across multiple RPCs if available."""
        target_rpc = rpc or rpc_url
        if not target_rpc:
            return b""

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": address, "data": selector},
                "latest",
            ],
        }

        # Try primary RPC
        try:
            resp = requests.post(target_rpc, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json().get("result")
            if (
                not isinstance(result, str)
                or not result.startswith("0x")
                or len(result) < 3
            ):
                return b""
            return bytes.fromhex(result[2:])
        except Exception as exc:
            _logger.debug(f"bridge_vvv: RPC call failed on {target_rpc[:30]}: {exc}")
            # Try fallback RPCs if available
            try:
                from libs.agentkit_ext.web3_utils import (
                    rpc_url_candidates,  # type: ignore
                )

                candidates = rpc_url_candidates()
                for fallback_rpc in candidates:
                    if fallback_rpc == target_rpc:
                        continue
                    try:
                        resp = requests.post(fallback_rpc, json=payload, timeout=10)
                        resp.raise_for_status()
                        result = resp.json().get("result")
                        if (
                            isinstance(result, str)
                            and result.startswith("0x")
                            and len(result) >= 3
                        ):
                            _logger.info(
                                f"bridge_vvv: Fallback RPC succeeded: {fallback_rpc[:30]}"
                            )
                            return bytes.fromhex(result[2:])
                    except Exception:
                        continue
            except Exception:
                pass
            return b""

    # Method 1: Try Uniswap V3 pool (slot0)
    if pool_addr:
        pool = _normalize(pool_addr)
        _logger.info(f"bridge_vvv: Attempting V3 pool query for VVV/USDC at {pool}")

        try:
            slot0_raw = slot0_cache_fetch(
                f"slot0_raw:{pool}",
                lambda: _call(pool, "0x3850c7bd"),
                validator=lambda value: isinstance(value, (bytes, bytearray))
                and len(value) >= 32,
            )
            if len(slot0_raw) >= 32:
                sqrt_price_x96 = int.from_bytes(slot0_raw[0:32], "big")
                if sqrt_price_x96 > 0:
                    pool_token0_raw = _call(pool, "0x0dfe1681")
                    pool_token1_raw = _call(pool, "0xd21220a7")
                    if len(pool_token0_raw) >= 32 and len(pool_token1_raw) >= 32:
                        pool_token0 = _normalize("0x" + pool_token0_raw[-20:].hex())
                        pool_token1 = _normalize("0x" + pool_token1_raw[-20:].hex())

                        dec_pool_0 = _erc20_decimals_from_env(pool_token0)
                        dec_pool_1 = _erc20_decimals_from_env(pool_token1)
                        ratio = sqrt_price_x96 / float(1 << 96)
                        price_token1_per_token0 = ratio * ratio
                        price_token1_per_token0 *= float(
                            pow(10.0, dec_pool_0 - dec_pool_1)
                        )

                        if pool_token0 == quote_addr and pool_token1 == vvv_addr:
                            vvv_per_quote = price_token1_per_token0
                            quote_per_vvv = (
                                1.0 / vvv_per_quote if vvv_per_quote > 0 else 0.0
                            )
                        elif pool_token0 == vvv_addr and pool_token1 == quote_addr:
                            quote_per_vvv = price_token1_per_token0
                        else:
                            quote_per_vvv = 0.0

                        if _valid_price(quote_per_vvv):
                            _logger.info(
                                f"bridge_vvv: V3 pool success, VVV=${quote_per_vvv:.6f}"
                            )
                            return (float(quote_per_vvv), "v3_pool")

            _logger.warning("bridge_vvv: V3 pool query failed or returned invalid data")
        except Exception as e:
            _logger.warning(f"bridge_vvv: V3 pool exception: {e}")

    # Method 2: Try Uniswap V2 pool (getReserves)
    if pool_addr:
        _logger.info(f"bridge_vvv: Attempting V2 pool query for VVV/USDC at {pool}")
        try:
            reserves_raw = _call(pool, "0x0902f1ac")
            if len(reserves_raw) >= 96:
                reserve0 = int.from_bytes(reserves_raw[0:32], "big")
                reserve1 = int.from_bytes(reserves_raw[32:64], "big")

                if reserve0 > 0 and reserve1 > 0:
                    token0_raw = _call(pool, "0x0dfe1681")
                    token1_raw = _call(pool, "0xd21220a7")
                    if len(token0_raw) >= 32 and len(token1_raw) >= 32:
                        token0 = _normalize("0x" + token0_raw[-20:].hex())
                        token1 = _normalize("0x" + token1_raw[-20:].hex())

                        dec0 = _erc20_decimals_from_env(token0)
                        dec1 = _erc20_decimals_from_env(token1)
                        r0 = reserve0 / float(10**dec0)
                        r1 = reserve1 / float(10**dec1)

                        if token0 == vvv_addr and token1 == quote_addr:
                            quote_per_vvv = r1 / r0 if r0 > 0 else 0.0
                        elif token0 == quote_addr and token1 == vvv_addr:
                            quote_per_vvv = r0 / r1 if r1 > 0 else 0.0
                        else:
                            quote_per_vvv = 0.0

                        if _valid_price(quote_per_vvv):
                            _logger.info(
                                f"bridge_vvv: V2 pool success, VVV=${quote_per_vvv:.6f}"
                            )
                            return (float(quote_per_vvv), "v2_pool")

            _logger.warning("bridge_vvv: V2 pool query failed or returned invalid data")
        except Exception as e:
            _logger.warning(f"bridge_vvv: V2 pool exception: {e}")

    # Method 3: Try DEX aggregator as fallback
    _logger.info("bridge_vvv: Attempting DEX aggregator for VVV price")
    try:
        from libs.dex.providers import build_aggregator_from_env
        from libs.dex.routes import make_route

        agg = build_aggregator_from_env()
        # Quote 1 VVV -> USDC
        route = make_route([vvv_addr, quote_addr])
        vvv_decimals = _erc20_decimals_from_env(vvv_addr)
        amount_in = int(1 * 10**vvv_decimals)

        quote = agg.best_quote(amount_in, route)
        if quote and quote.amount_out > 0:
            usdc_decimals = _erc20_decimals_from_env(quote_addr)
            vvv_price = quote.amount_out / float(10**usdc_decimals)
            if _valid_price(vvv_price):
                _logger.info(
                    f"bridge_vvv: DEX aggregator success, VVV=${vvv_price:.6f}"
                )
                return (float(vvv_price), "dex_aggregator")

        _logger.warning("bridge_vvv: DEX aggregator returned no valid quote")
    except Exception as e:
        _logger.warning(f"bridge_vvv: DEX aggregator exception: {e}")

    _logger.error("bridge_vvv: All VVV price methods failed")
    return None


def vvv_usd_price(config: EnvConfig) -> float | None:
    """Best-effort VVV/USD price from the configured VVV/USDC UniswapV3 pool.

    This is intended as a deterministic fallback when DEX aggregator quotes are
    flaky or misconfigured.
    """

    try:
        rpc_url = _rpc_url()
    except Exception:
        rpc_url = None
    if not rpc_url:
        return None
    try:
        res = _get_vvv_price_usd(config, str(rpc_url))
    except Exception:
        res = None
    if not res:
        return None
    price, _method = res
    try:
        return float(price) if _valid_price(price) else None
    except Exception:
        return None


def _bridge_price_cache_ttl() -> float:
    """Get TTL for bridge price cache (default 30 seconds)."""
    raw = os.getenv("DIEM_BRIDGE_PRICE_CACHE_TTL", "").strip()
    if not raw:
        return 30.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _bridge_price_cache_get() -> float | None:
    """Get cached bridge price if still valid."""
    cache_ttl = _bridge_price_cache_ttl()
    if cache_ttl <= 0:
        return None
    now = time.monotonic()
    with _BRIDGE_PRICE_CACHE_LOCK:
        entry = _BRIDGE_PRICE_CACHE.get("DIEM")
    if not entry:
        return None
    cached_price, cached_ts = entry
    age = now - cached_ts
    if age <= cache_ttl:
        _logger.debug(
            "bridge_vvv: Using cached price (age=%.1fs)",
            age,
        )
        return cached_price
    return None


def _bridge_price_cache_set(price: float) -> None:
    """Store bridge price in cache."""
    cache_ttl = _bridge_price_cache_ttl()
    if cache_ttl <= 0:
        return
    with _BRIDGE_PRICE_CACHE_LOCK:
        _BRIDGE_PRICE_CACHE["DIEM"] = (float(price), time.monotonic())


def _bridge_vvv_price_uncached(config: EnvConfig) -> float | None:
    """Compute DIEM price via the bridge without using the cache."""
    pair_addr = config.diem_vvv_pair
    if not pair_addr:
        _logger.warning("bridge_vvv: DIEM/VVV pair address not configured")
        return None

    rpc_url = _rpc_url()
    if not rpc_url:
        _logger.warning("bridge_vvv: No RPC URL available")
        return None

    def _call(address: str, selector: str) -> bytes:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": address, "data": selector},
                "latest",
            ],
        }
        try:
            resp = requests.post(rpc_url, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json().get("result")
            if (
                not isinstance(result, str)
                or not result.startswith("0x")
                or len(result) < 3
            ):
                return b""
            return bytes.fromhex(result[2:])
        except Exception as e:
            _logger.debug(f"bridge_vvv: Primary RPC failed, trying fallbacks: {e}")
            # Try fallback RPCs
            try:
                from libs.agentkit_ext.web3_utils import (
                    rpc_url_candidates,  # type: ignore
                )

                candidates = rpc_url_candidates()
                for fallback_rpc in candidates:
                    if fallback_rpc == rpc_url:
                        continue
                    try:
                        resp = requests.post(fallback_rpc, json=payload, timeout=10)
                        resp.raise_for_status()
                        result = resp.json().get("result")
                        if (
                            isinstance(result, str)
                            and result.startswith("0x")
                            and len(result) >= 3
                        ):
                            _logger.info(
                                f"bridge_vvv: Fallback RPC succeeded for reserves: {fallback_rpc[:30]}"
                            )
                            return bytes.fromhex(result[2:])
                    except Exception:
                        continue
            except Exception:
                pass
            return b""

    cache_ttl = _reserve_cache_ttl()
    stale_multiplier = _reserve_cache_stale_multiplier()
    stale_window = cache_ttl * stale_multiplier if cache_ttl > 0 else 0
    try:
        pair = _normalize(pair_addr)
        cache_key = pair
        cache_hit = False
        cached_payload: dict[str, int | str] | None = None
        cache_age: float | None = None
        if cache_ttl > 0:
            cached = _reserve_cache_get(cache_key, cache_ttl)
            if cached:
                cached_payload, cache_age = cached
                cache_hit = True
                _logger.debug(
                    "bridge_vvv: Using cached DIEM/VVV reserves (age=%.1fs)",
                    cache_age or 0.0,
                )
        if not cache_hit:
            _logger.info(f"bridge_vvv: Querying DIEM/VVV pair at {pair}")

        # Step 1: Get DIEM/VVV reserves
        if cached_payload:
            reserve0 = int(cached_payload["reserve0"])
            reserve1 = int(cached_payload["reserve1"])
            token0 = str(cached_payload["token0"])
            token1 = str(cached_payload["token1"])
        else:
            reserves_raw = _call(pair, "0x0902f1ac")
            if len(reserves_raw) < 96:
                _logger.error("bridge_vvv: Failed to get DIEM/VVV reserves")
                stale = _reserve_cache_get(cache_key, stale_window, allow_stale=True)
                if stale:
                    cached_payload, cache_age = stale
                    reserve0 = int(cached_payload["reserve0"])
                    reserve1 = int(cached_payload["reserve1"])
                    token0 = str(cached_payload["token0"])
                    token1 = str(cached_payload["token1"])
                    _logger.warning(
                        "bridge_vvv: Using stale cached reserves after RPC failure (age=%.1fs)",
                        cache_age or 0.0,
                    )
                    if cache_age is not None:
                        try:
                            _metrics_inc(
                                "marketdata_cache_age_seconds",
                                value=int(cache_age),
                                labels={"symbol": "DIEM", "reason": "reserve_stale"},
                            )
                        except Exception:
                            pass
                else:
                    return None
            else:
                reserve0 = int.from_bytes(reserves_raw[0:32], "big")
                reserve1 = int.from_bytes(reserves_raw[32:64], "big")

                token0_raw = _call(pair, "0x0dfe1681")
                token1_raw = _call(pair, "0xd21220a7")
                if len(token0_raw) < 32 or len(token1_raw) < 32:
                    _logger.error("bridge_vvv: Failed to get DIEM/VVV token addresses")
                    stale = _reserve_cache_get(
                        cache_key,
                        stale_window,
                        allow_stale=True,
                    )
                    if stale:
                        cached_payload, cache_age = stale
                        reserve0 = int(cached_payload["reserve0"])
                        reserve1 = int(cached_payload["reserve1"])
                        token0 = str(cached_payload["token0"])
                        token1 = str(cached_payload["token1"])
                        _logger.warning(
                            "bridge_vvv: Falling back to cached token metadata after RPC failure (age=%.1fs)",
                            cache_age or 0.0,
                        )
                        if cache_age is not None:
                            try:
                                _metrics_inc(
                                    "marketdata_cache_age_seconds",
                                    value=int(cache_age),
                                    labels={
                                        "symbol": "DIEM",
                                        "reason": "reserve_metadata_stale",
                                    },
                                )
                            except Exception:
                                pass
                    else:
                        return None
                else:
                    token0 = _normalize("0x" + token0_raw[-20:].hex())
                    token1 = _normalize("0x" + token1_raw[-20:].hex())
                    if cache_ttl > 0:
                        _reserve_cache_set(
                            cache_key,
                            {
                                "reserve0": reserve0,
                                "reserve1": reserve1,
                                "token0": token0,
                                "token1": token1,
                            },
                        )

        diem_addr = _normalize(
            config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or ""
        )
        vvv_addr = _normalize(config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or "")

        if not diem_addr or not vvv_addr:
            _logger.error("bridge_vvv: Missing DIEM or VVV token address")
            return None
        if token0 not in {diem_addr, vvv_addr} or token1 not in {diem_addr, vvv_addr}:
            _logger.error(
                f"bridge_vvv: Pair tokens don't match. token0={token0}, token1={token1}"
            )
            return None

        dec0 = _erc20_decimals_from_env(token0)
        dec1 = _erc20_decimals_from_env(token1)
        r0 = reserve0 / float(10**dec0)
        r1 = reserve1 / float(10**dec1)

        # Debug: log token ordering detection
        _logger.debug(
            f"bridge_vvv: token ordering - token0={token0[:10]}..., token1={token1[:10]}..., "
            f"vvv_addr={vvv_addr[:10]}..., diem_addr={diem_addr[:10]}..."
        )

        # Calculate VVV per DIEM ratio based on token ordering
        # VVV per DIEM should be < 1 since DIEM is worth more VVV tokens
        # (DIEM ~$0.02, VVV ~$1.45 => 1 DIEM buys ~0.014 VVV)
        if token0 == vvv_addr and token1 == diem_addr:
            vvv_per_diem = r0 / r1 if r1 > 0 else 0.0
            branch_matched = "token0=VVV, token1=DIEM"
        elif token0 == diem_addr and token1 == vvv_addr:
            vvv_per_diem = r1 / r0 if r0 > 0 else 0.0
            branch_matched = "token0=DIEM, token1=VVV"
        else:
            _logger.error("bridge_vvv: Could not determine token ordering")
            return None

        _logger.debug(
            f"bridge_vvv: matched branch '{branch_matched}', raw ratio={vvv_per_diem:.6f}"
        )

        # Note: DIEM can be worth MORE than 1 VVV - it represents "$1/day of AI credit forever"
        # The market prices DIEM at ~100-200 VVV per DIEM as of Dec 2024
        # Do NOT invert ratios > 1 - that's the correct market price

        if not _valid_price(vvv_per_diem):
            _logger.error(f"bridge_vvv: Invalid VVV per DIEM ratio: {vvv_per_diem}")
            return None

        _logger.info(
            f"bridge_vvv: DIEM/VVV pair - reserves: {r0:.2f}/{r1:.2f}, ratio: {vvv_per_diem:.6f} VVV per DIEM"
        )

        # Step 2: Get VVV price in USD with fallbacks
        vvv_price_result = _get_vvv_price_usd(config, rpc_url)
        if not vvv_price_result:
            _logger.error("bridge_vvv: Failed to get VVV price via any method")
            return None

        vvv_price_usd, method = vvv_price_result

        # Step 3: Calculate DIEM price
        diem_price_usd = vvv_per_diem * vvv_price_usd

        if not _valid_price(diem_price_usd):
            _logger.error(f"bridge_vvv: Invalid final DIEM price: {diem_price_usd}")
            return None

        _logger.info(
            f"bridge_vvv: SUCCESS - DIEM=${diem_price_usd:.2f} (via {method}, VVV=${vvv_price_usd:.6f}, ratio={vvv_per_diem:.2f})"
        )
        final_price = float(diem_price_usd)
        return final_price

    except Exception as e:
        _logger.error(f"bridge_vvv: Unexpected exception: {e}")
        return None


def bridge_vvv_price(config: EnvConfig) -> float | None:
    """
    Calculate DIEM price via DIEM/VVV pair reserves × VVV/USD price.
    Uses multiple fallback methods for VVV price discovery.

    Caches successful prices for a short TTL (default 30s) to reduce
    repeated DEX calls during incidents and to serialize concurrent
    bridge queries at startup.
    """
    cached_price = _bridge_price_cache_get()
    if cached_price is not None:
        return cached_price

    with _BRIDGE_PRICE_LOCK:
        # Double‑check after acquiring the lock to avoid duplicate work.
        cached_price = _bridge_price_cache_get()
        if cached_price is not None:
            return cached_price

        price = _bridge_vvv_price_uncached(config)
        if price is not None and _valid_price(price):
            _bridge_price_cache_set(price)
        return price


def bridge_fallback(
    *,
    amount_in: int,
    config: EnvConfig,
    guardrails: GuardrailContext,
    policy: PolicyContext,
    mode: QuoteMode,
) -> QuoteResult | None:
    price = bridge_vvv_price(config)
    if not _valid_price(price):
        return None
    metadata = {
        "path": ["bridge", "vvv", "usdc"],
        "bridge_price": price,
    }
    result = QuoteResult(
        amount_in=amount_in,
        amount_out=0,
        price=float(price),
        provider="bridge_vvv",
        route=None,  # type: ignore[arg-type]
        score=0.0,
        guardrails=guardrails,
        policy=policy,
        mode=mode,
        source="bridge_vvv",
        metadata=metadata,
    )
    return result


def external_reference_fallback(
    *,
    token_in: str,
    token_out: str,
    amount_in: int,
    fetcher: Callable[[str], float | None],
    guardrails: GuardrailContext,
    policy: PolicyContext,
    mode: QuoteMode,
    token_symbol: str | None = None,
) -> QuoteResult | None:
    label = token_symbol or token_in
    price = fetcher(label)
    if not _valid_price(price):
        return None
    metadata = {
        "source": "external_reference",
        "token_in": token_in,
        "token_out": token_out,
    }
    return QuoteResult(
        amount_in=amount_in,
        amount_out=0,
        price=float(price),
        provider="external",
        route=None,  # type: ignore[arg-type]
        score=0.0,
        guardrails=guardrails,
        policy=policy,
        mode=mode,
        source="external_reference",
        metadata=metadata,
    )


def get_bridge_trade_path(config: EnvConfig) -> list[str] | None:
    """
    Get the explicit token path [DIEM, VVV, QUOTE] if the bridge is valid.
    Used to inject the bridge path into the execution router.
    """
    # Verify price is resolvable via bridge (implies liquidity and connectivity)
    price = bridge_vvv_price(config)
    if not _valid_price(price):
        return None

    diem = _normalize(config.diem_token)
    vvv = _normalize(config.vvv_token)
    quote = _normalize(config.quote_token)

    if not diem or not vvv or not quote:
        return None

    return [diem, vvv, quote]


def get_bridge_trade_path_with_metadata(config: EnvConfig) -> dict[str, Any] | None:
    """
    Get the bridge trade path with structured metadata for each leg.

    Returns a dict with:
    - path: List[str] - token addresses [DIEM, VVV, QUOTE]
    - legs: List[Dict] - metadata for each leg including provider type, pool address, fee tier
    """
    # Verify price is resolvable via bridge
    price = bridge_vvv_price(config)
    if not _valid_price(price):
        return None

    diem = _normalize(config.diem_token)
    vvv = _normalize(config.vvv_token)
    quote = _normalize(config.quote_token)

    if not diem or not vvv or not quote:
        return None

    path = [diem, vvv, quote]
    legs: list[dict[str, Any]] = []

    # Leg 1: DIEM -> VVV (V2 or Aerodrome pair)
    diem_vvv_pair = config.diem_vvv_pair
    if diem_vvv_pair:
        # Prefer Aerodrome for the DIEM/VVV leg; allow override via env.
        provider_override = (
            os.getenv("DIEM_VVV_BRIDGE_PROVIDER", "").strip().lower() or "aerodrome"
        )
        legs.append(
            {
                "token_in": diem,
                "token_out": vvv,
                "provider": provider_override,
                "pool_address": _normalize(diem_vvv_pair),
                "fee": None,  # V2/Aerodrome don't use fee tiers
            }
        )

    # Leg 2: VVV -> USDC (V3 pool if available, else V2)
    vvv_usdc_pool = config.vvv_usdc_pool
    if vvv_usdc_pool:
        # Try to detect if it's V3 by checking for fee tier info
        # Default assumption: V3 if pool address is provided
        # We'll check the actual pool type when quoting
        # Get fee tier from env (defaults to 3000 for V3)
        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE", "3000")
            vvv_usdc_fee = int(fee_str) if fee_str else None
        except (TypeError, ValueError):
            vvv_usdc_fee = 3000  # Default V3 fee tier
        legs.append(
            {
                "token_in": vvv,
                "token_out": quote,
                "provider": "uniswap_v3",  # Default to V3, can fallback to V2
                "pool_address": _normalize(vvv_usdc_pool),
                "fee": vvv_usdc_fee,  # Use configured fee tier (3000 for V3)
            }
        )
    else:
        # Fallback: assume V2 if no pool specified
        legs.append(
            {
                "token_in": vvv,
                "token_out": quote,
                "provider": "uniswap_v2",
                "pool_address": None,
                "fee": None,  # V2 doesn't use fee tiers
            }
        )

    return {
        "path": path,
        "legs": legs,
        "source": "bridge_vvv",
    }


__all__ = [
    "bridge_fallback",
    "bridge_vvv_price",
    "external_reference_fallback",
    "get_bridge_trade_path",
    "get_bridge_trade_path_with_metadata",
]
