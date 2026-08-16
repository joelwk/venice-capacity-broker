"""
DIEM-specific execution fallbacks.

This module provides fallback mechanisms for DIEM trades when router calls fail
but on-chain reserves are available.
"""

import math
import os
import time
from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, Any

from libs.dex.routes import RoutePlan, make_route
from services.marketdata.pathing.env import load_env_config

if TYPE_CHECKING:  # pragma: no cover
    from libs.dex.providers import Quote  # circular at runtime
else:
    Quote = Any


_SLOT0_CACHE: dict[str, tuple[float, Any]] = {}
_SLOT0_CACHE_LOCK = Lock()


def _slot0_cache_ttl_seconds() -> float:
    raw = (os.getenv("DEX_SLOT0_CACHE_TTL_SECONDS") or "").strip()
    if not raw:
        return 10.0
    try:
        return max(0.0, float(raw))
    except Exception:
        return 10.0


def _slot0_cache_fetch(
    key: str,
    fetcher: Callable[[], Any],
    *,
    validator: Callable[[Any], bool] | None = None,
) -> Any:
    ttl = _slot0_cache_ttl_seconds()
    if ttl <= 0:
        return fetcher()
    now = time.time()
    with _SLOT0_CACHE_LOCK:
        cached = _SLOT0_CACHE.get(key)
    if cached:
        ts, value = cached
        if (now - ts) < ttl:
            return value
    value = fetcher()
    if validator is not None and not validator(value):
        return value
    if value is None:
        return None
    with _SLOT0_CACHE_LOCK:
        _SLOT0_CACHE[key] = (now, value)
    return value


def slot0_cache_fetch(
    key: str,
    fetcher: Callable[[], Any],
    *,
    validator: Callable[[Any], bool] | None = None,
) -> Any:
    return _slot0_cache_fetch(key, fetcher, validator=validator)


def _normalize_address(addr: str) -> str:
    """Normalize address to lowercase."""
    if not addr:
        return ""
    return addr.strip().lower()


def _erc20_decimals_from_env(addr: str) -> int:
    """Get ERC20 decimals from env or default to 18."""
    norm = _normalize_address(addr)
    if not norm:
        return 18
    if norm == _normalize_address(os.getenv("QUOTE_TOKEN_ADDRESS")):
        return int((os.getenv("QUOTE_TOKEN_DECIMALS") or "6").strip() or 6)
    if norm == _normalize_address(os.getenv("DIEM_TOKEN_ADDRESS")):
        return int((os.getenv("DIEM_DECIMALS") or "18").strip() or 18)
    if norm == _normalize_address(os.getenv("VVV_TOKEN_ADDRESS")):
        return int((os.getenv("VVV_DECIMALS") or "18").strip() or 18)
    try:
        from web3 import Web3  # type: ignore

        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        erc20_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "stateMutability": "view",
                "type": "function",
            }
        ]
        erc20 = get_contract(w3, Web3.to_checksum_address(norm), abi=erc20_abi)
        return int(erc20.functions.decimals().call())
    except Exception:
        return 18


def _get_diem_vvv_reserves() -> tuple[int, int, str, str] | None:
    """
    Get DIEM/VVV pair reserves directly from on-chain.

    Returns:
        Tuple of (reserve0, reserve1, token0, token1) or None if unavailable
    """
    try:
        from libs.agentkit_ext.web3_utils import get_web3

        pair_addr = os.getenv("DIEM_VVV_PAIR_ADDRESS")
        if not pair_addr:
            return None

        w3 = get_web3()
        # Build a minimal contract instance using an inline ABI to avoid relying
        # on the repository ABI loader (the DIEM/VVV pair is a V2-style pool).
        pair_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "getReserves",
                "outputs": [
                    {"name": "_reserve0", "type": "uint112"},
                    {"name": "_reserve1", "type": "uint112"},
                    {"name": "_blockTimestampLast", "type": "uint32"},
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [],
                "name": "token0",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [],
                "name": "token1",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
        ]

        pair = w3.eth.contract(address=w3.to_checksum_address(pair_addr), abi=pair_abi)
        reserves = pair.functions.getReserves().call()
        token0 = pair.functions.token0().call()
        token1 = pair.functions.token1().call()

        return (reserves[0], reserves[1], token0, token1)
    except Exception:
        return None


def diem_vvv_quote_from_reserves(
    amount_out: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Compute DIEM/VVV quote using constant-product math from reserves (exact-out).

    This is a fallback when router calls fail but reserves are available.
    """
    if os.getenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None

    reserves_data = _get_diem_vvv_reserves()
    if not reserves_data:
        return None

    reserve0, reserve1, token0_addr, token1_addr = reserves_data

    token_in_norm = _normalize_address(token_in)
    token_out_norm = _normalize_address(token_out)
    token0_norm = _normalize_address(token0_addr)
    token1_norm = _normalize_address(token1_addr)

    if token_in_norm == token0_norm and token_out_norm == token1_norm:
        reserve_in = reserve0
        reserve_out = reserve1
    elif token_in_norm == token1_norm and token_out_norm == token0_norm:
        reserve_in = reserve1
        reserve_out = reserve0
    else:
        return None

    if amount_out <= 0 or reserve_out <= amount_out:
        return None

    numerator = reserve_in * amount_out * 1000
    denominator = (reserve_out - amount_out) * 997
    if denominator <= 0:
        return None

    amount_in = (numerator // denominator) + 1

    route = make_route([token_in, token_out])
    try:
        from libs.dex.providers import Quote as _Quote
    except Exception:
        return None
    return _Quote(
        provider="diem_pair_math",
        amount_in=amount_in,
        amount_out=amount_out,
        route=route,
    )


def diem_vvv_quote_exact_in_from_reserves(
    amount_in: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Compute DIEM/VVV quote using constant-product math from reserves (exact-in).
    """
    if os.getenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None

    reserves_data = _get_diem_vvv_reserves()
    if not reserves_data:
        return None

    reserve0, reserve1, token0_addr, token1_addr = reserves_data

    token_in_norm = _normalize_address(token_in)
    token_out_norm = _normalize_address(token_out)
    token0_norm = _normalize_address(token0_addr)
    token1_norm = _normalize_address(token1_addr)

    if token_in_norm == token0_norm and token_out_norm == token1_norm:
        reserve_in = reserve0
        reserve_out = reserve1
    elif token_in_norm == token1_norm and token_out_norm == token0_norm:
        reserve_in = reserve1
        reserve_out = reserve0
    else:
        return None

    if amount_in <= 0:
        return None

    amount_in_with_fee = amount_in * 997
    numerator = amount_in_with_fee * reserve_out
    denominator = (reserve_in * 1000) + amount_in_with_fee

    if denominator <= 0:
        return None

    amount_out = numerator // denominator

    route = make_route([token_in, token_out])
    try:
        from libs.dex.providers import Quote as _Quote
    except Exception:
        return None
    return _Quote(
        provider="diem_pair_math",
        amount_in=amount_in,
        amount_out=amount_out,
        route=route,
    )


def build_two_stage_diem_route(
    token_in: str,
    token_out: str,
    config=None,
) -> tuple[RoutePlan, RoutePlan] | None:
    """
    Build a two-stage DIEM route: DIEM↔VVV then VVV↔USDC.

    Returns:
        Tuple of (stage1_route, stage2_route) or None if not applicable
    """
    if config is None:
        config = load_env_config()

    diem_addr = _normalize_address(
        config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or ""
    )
    vvv_addr = _normalize_address(
        config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or ""
    )
    quote_addr = _normalize_address(
        config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or ""
    )

    if not diem_addr or not vvv_addr or not quote_addr:
        return None

    token_in_norm = _normalize_address(token_in)
    token_out_norm = _normalize_address(token_out)

    # DIEM -> USDC: Stage1: DIEM -> VVV, Stage2: VVV -> USDC
    if token_in_norm == diem_addr and token_out_norm == quote_addr:
        if not config.diem_vvv_pair or not config.vvv_usdc_pool:
            return None

        stage1 = make_route([token_in, vvv_addr])
        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
            vvv_usdc_fee = int(fee_str)
        except Exception:
            vvv_usdc_fee = 3000
        stage2 = make_route([vvv_addr, token_out], fees=[vvv_usdc_fee])
        return (stage1, stage2)

    # USDC -> DIEM: Stage1: USDC -> VVV, Stage2: VVV -> DIEM
    if token_in_norm == quote_addr and token_out_norm == diem_addr:
        if not config.diem_vvv_pair or not config.vvv_usdc_pool:
            return None

        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
            vvv_usdc_fee = int(fee_str)
        except Exception:
            vvv_usdc_fee = 3000
        stage1 = make_route([token_in, vvv_addr], fees=[vvv_usdc_fee])
        stage2 = make_route([vvv_addr, token_out])
        return (stage1, stage2)

    return None


def get_diem_fallback_error_reason(
    error: Exception,
    route: RoutePlan,
) -> str:
    """
    Extract structured error reason from DIEM trade failures.

    Returns error codes like:
    - no_liquidity_diem_vvv
    - router_revert_spl
    - router_revert_underflow
    - etc.
    """
    error_msg = str(error).lower()

    # Check for common revert reasons
    if "spl" in error_msg or "insufficient liquidity" in error_msg:
        return "no_liquidity_diem_vvv"
    if "ds-math-sub-underflow" in error_msg or "underflow" in error_msg:
        return "router_revert_underflow"
    if "execution reverted" in error_msg:
        return "router_revert_execution"
    if "no data" in error_msg or "no quotes" in error_msg:
        return "no_quotes_available"

    # Check route-specific issues
    tokens = route.tokens
    diem_addr = _normalize_address(os.getenv("DIEM_TOKEN_ADDRESS") or "")
    vvv_addr = _normalize_address(os.getenv("VVV_TOKEN_ADDRESS") or "")

    if diem_addr and vvv_addr:
        if diem_addr in [_normalize_address(t) for t in tokens] and vvv_addr in [
            _normalize_address(t) for t in tokens
        ]:
            return "diem_vvv_leg_failed"

    return "unknown_error"


def check_diem_vvv_liquidity_threshold(
    min_reserve_out: int = 1000000000000000000,
) -> bool:
    """
    Check if DIEM/VVV pair has sufficient liquidity for trading.

    Args:
        min_reserve_out: Minimum reserve amount (in base units) required for trading.
                        Default: 1 token (1e18 for 18-decimal tokens).

    Returns:
        True if reserves are above threshold, False otherwise.
    """
    reserves_data = _get_diem_vvv_reserves()
    if not reserves_data:
        return False

    reserve0, reserve1, _, _ = reserves_data
    # Check if either reserve is above threshold
    return reserve0 >= min_reserve_out or reserve1 >= min_reserve_out


def _slot0_flag_enabled(env_var: str, default: str = "0") -> bool:
    raw = os.getenv(env_var, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _vvv_usdc_v3_slot0_quote(
    *,
    amount: int,
    token_in: str,
    token_out: str,
    mode: str,
    provider_name: str,
    executable: bool,
    flag_env: str,
    default_flag: str,
    log_name: str,
) -> Quote | None:
    """
    Generic slot0-based quote helper for VVV/USDC Uniswap V3 pool.

    Supports exact-in and exact-out, with configurable provider name and
    executability so analytic and executable fallbacks share the same math.
    """

    if not _slot0_flag_enabled(flag_env, default_flag):
        return None

    try:
        from libs.agentkit_ext.web3_utils import get_web3
        from libs.telemetry.logger import get_logger

        logger = get_logger("diem.fallbacks")

        pool_addr = (
            os.getenv("VVV_USDC_POOL_V3_ADDRESS")
            or os.getenv("VVV_USDC_POOL_ADDRESS")
            or ""
        ).strip()
        if not pool_addr:
            logger.debug(f"{log_name}: No pool address configured")
            return None

        w3 = get_web3()

        pool_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "slot0",
                "outputs": [
                    {"name": "sqrtPriceX96", "type": "uint160"},
                    {"name": "tick", "type": "int24"},
                    {"name": "observationIndex", "type": "uint16"},
                    {"name": "observationCardinality", "type": "uint16"},
                    {"name": "observationCardinalityNext", "type": "uint16"},
                    {"name": "feeProtocol", "type": "uint8"},
                    {"name": "unlocked", "type": "bool"},
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [],
                "name": "token0",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [],
                "name": "token1",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
        ]

        pool = w3.eth.contract(address=w3.to_checksum_address(pool_addr), abi=pool_abi)
        slot0 = _slot0_cache_fetch(
            f"slot0:{pool_addr.lower()}",
            lambda: pool.functions.slot0().call(),
            validator=lambda value: bool(value),
        )
        if not slot0:
            logger.debug(f"{log_name}: slot0 call returned empty data")
            return None
        sqrt_price_x96 = slot0[0]
        token0_addr = pool.functions.token0().call()
        token1_addr = pool.functions.token1().call()

        token_in_norm = _normalize_address(token_in)
        token_out_norm = _normalize_address(token_out)
        token0_norm = _normalize_address(token0_addr)
        token1_norm = _normalize_address(token1_addr)

        token_in_decimals = _erc20_decimals_from_env(token_in)
        token_out_decimals = _erc20_decimals_from_env(token_out)
        token0_decimals = _erc20_decimals_from_env(token0_addr)
        token1_decimals = _erc20_decimals_from_env(token1_addr)

        Q96 = 2**96
        sqrt_price = float(sqrt_price_x96) / Q96
        price_raw = sqrt_price * sqrt_price
        price_token1_per_token0 = float(price_raw) * (
            10 ** (token0_decimals - token1_decimals)
        )

        if token_in_norm == token0_norm and token_out_norm == token1_norm:
            price_out_per_in = float(price_token1_per_token0)
        elif token_in_norm == token1_norm and token_out_norm == token0_norm:
            price_out_per_in = (
                1.0 / float(price_token1_per_token0)
                if price_token1_per_token0 > 0
                else 0.0
            )
        else:
            logger.debug(
                f"{log_name}: Token order mismatch",
                extra={
                    "token_in": token_in_norm,
                    "token_out": token_out_norm,
                    "token0": token0_norm,
                    "token1": token1_norm,
                },
            )
            return None

        if price_out_per_in <= 0:
            return None

        fee_tier = 3000
        try:
            raw_fee = os.getenv("VVV_USDC_POOL_FEE") or ""
            if raw_fee.strip():
                fee_tier = int(raw_fee.strip())
        except Exception:
            fee_tier = 3000
        fee_multiplier = 1.0 + (float(fee_tier) / 1_000_000.0)

        if mode == "exact_in":
            amount_in_decimal = float(amount) / (10**token_in_decimals)
            amount_out_decimal = (amount_in_decimal * price_out_per_in) / fee_multiplier
            amount_out = int(amount_out_decimal * (10**token_out_decimals))

            if amount_out <= 0:
                return None

            quote_amount_in = int(amount)
            quote_amount_out = int(amount_out)
        else:
            amount_out_decimal = float(amount) / (10**token_out_decimals)
            amount_in_decimal = (amount_out_decimal / price_out_per_in) * fee_multiplier
            amount_in = int(amount_in_decimal * (10**token_in_decimals))

            if amount_in <= 0:
                return None

            quote_amount_in = int(amount_in)
            quote_amount_out = int(amount)

        route = make_route([token_in, token_out])
        try:
            from libs.dex.providers import Quote as _Quote
        except Exception:
            return None

        quote = _Quote(
            provider=provider_name,
            amount_in=quote_amount_in,
            amount_out=quote_amount_out,
            route=route,
            executable=bool(executable),
        )
        try:
            quote.pool_address = pool_addr
            quote.slot0_price = price_out_per_in
            quote.slot0_fee = fee_tier
        except Exception:
            pass
        return quote
    except Exception as e:
        try:
            from libs.telemetry.logger import get_logger as _get_logger

            _get_logger("diem.fallbacks").debug(
                f"{log_name}: Exception: {e}", exc_info=True
            )
        except Exception:
            pass
        return None


def vvv_usdc_v3_mid_price_quote_exact_out(
    amount_out: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Preview-only analytic fallback using slot0 mid-price (exact-out).
    """

    return _vvv_usdc_v3_slot0_quote(
        amount=amount_out,
        token_in=token_in,
        token_out=token_out,
        mode="exact_out",
        provider_name="composite_analytic",
        executable=False,
        flag_env="DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE",
        default_flag="1",
        log_name="vvv_usdc_v3_mid_price_quote_exact_out",
    )


def vvv_usdc_v3_mid_price_quote(
    amount_in: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Preview-only analytic fallback using slot0 mid-price (exact-in).
    """

    return _vvv_usdc_v3_slot0_quote(
        amount=amount_in,
        token_in=token_in,
        token_out=token_out,
        mode="exact_in",
        provider_name="composite_analytic",
        executable=False,
        flag_env="DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE",
        default_flag="1",
        log_name="vvv_usdc_v3_mid_price_quote",
    )


def vvv_usdc_v3_slot0_quote(
    amount_in: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Executable fallback for VVV/USDC exact-in quotes using slot0 mid-price.
    """

    return _vvv_usdc_v3_slot0_quote(
        amount=amount_in,
        token_in=token_in,
        token_out=token_out,
        mode="exact_in",
        provider_name="vvv_usdc_v3_slot0",
        executable=True,
        flag_env="DIEM_VVV_USDC_V3_SLOT0_FALLBACK_ENABLE",
        default_flag="1",
        log_name="vvv_usdc_v3_slot0_quote",
    )


def vvv_usdc_v3_slot0_quote_exact_out(
    amount_out: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Executable fallback for VVV/USDC exact-out quotes using slot0 mid-price.
    """

    return _vvv_usdc_v3_slot0_quote(
        amount=amount_out,
        token_in=token_in,
        token_out=token_out,
        mode="exact_out",
        provider_name="vvv_usdc_v3_slot0",
        executable=True,
        flag_env="DIEM_VVV_USDC_V3_SLOT0_FALLBACK_ENABLE",
        default_flag="1",
        log_name="vvv_usdc_v3_slot0_quote_exact_out",
    )


# =============================================================================
# DIEM/USDC SlipStream slot0-based quoting
# =============================================================================


def _diem_usdc_slot0_quote(
    *,
    amount: int,
    token_in: str,
    token_out: str,
    mode: str,
    provider_name: str,
    executable: bool,
    log_name: str,
) -> Quote | None:
    """
    Generic slot0-based quote helper for DIEM/USDC Aerodrome SlipStream pool.

    Supports exact-in and exact-out, using sqrtPriceX96 from the pool's slot0.
    This is the primary mechanism for quoting the DIEM/USDC direct route since
    Aerodrome SlipStream pools use CL (V3-style) mechanics but a different quoter.
    """
    try:
        from libs.agentkit_ext.web3_utils import get_web3
        from libs.telemetry.logger import get_logger

        logger = get_logger("diem.fallbacks")

        pool_addr = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
        if not pool_addr:
            logger.info(f"{log_name}: No DIEM_USDC_POOL_ADDRESS configured")
            return None

        diem_addr = _normalize_address(os.getenv("DIEM_TOKEN_ADDRESS") or "")
        usdc_addr = _normalize_address(os.getenv("QUOTE_TOKEN_ADDRESS") or "")
        if not diem_addr or not usdc_addr:
            logger.info(
                f"{log_name}: Missing DIEM or USDC token address (diem={diem_addr}, usdc={usdc_addr})"
            )
            return None

        token_in_norm = _normalize_address(token_in)
        token_out_norm = _normalize_address(token_out)

        logger.info(
            f"{log_name}: Checking route - token_in={token_in_norm[:10]}..., token_out={token_out_norm[:10]}..., "
            f"diem={diem_addr[:10]}..., usdc={usdc_addr[:10]}..."
        )

        # Verify this is a DIEM/USDC route
        tokens_ok = (token_in_norm == diem_addr and token_out_norm == usdc_addr) or (
            token_in_norm == usdc_addr and token_out_norm == diem_addr
        )
        if not tokens_ok:
            logger.info(f"{log_name}: Not a DIEM/USDC route - tokens don't match")
            return None

        w3 = get_web3()

        # Aerodrome SlipStream pools have a different slot0 structure than Uniswap V3
        # (8 return values instead of 7). We use raw eth_call and decode only the
        # sqrtPriceX96 (first 32 bytes) which is compatible across implementations.
        pool_addr_checksum = w3.to_checksum_address(pool_addr)

        # slot0() selector = keccak256("slot0()")[:4] = 0x3850c7bd
        slot0_selector = bytes.fromhex("3850c7bd")
        slot0_data = _slot0_cache_fetch(
            f"slot0_raw:{pool_addr_checksum.lower()}",
            lambda: w3.eth.call({"to": pool_addr_checksum, "data": slot0_selector}),
            validator=lambda value: isinstance(value, (bytes, bytearray))
            and len(value) >= 32,
        )

        # sqrtPriceX96 is the first 32 bytes (uint160 padded to 32 bytes)
        if len(slot0_data) < 32:
            logger.info(
                f"{log_name}: slot0 response too short: {len(slot0_data)} bytes"
            )
            return None
        sqrt_price_x96 = int.from_bytes(slot0_data[:32], "big")

        logger.info(
            f"{log_name}: Raw slot0 call success, sqrtPriceX96={sqrt_price_x96}"
        )

        # token0() and token1() - use standard ERC20 interface
        token_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "token0",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [],
                "name": "token1",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
        ]

        pool = w3.eth.contract(address=pool_addr_checksum, abi=token_abi)
        token0_addr = pool.functions.token0().call()
        token1_addr = pool.functions.token1().call()

        token0_norm = _normalize_address(token0_addr)
        token1_norm = _normalize_address(token1_addr)

        token_in_decimals = _erc20_decimals_from_env(token_in)
        token_out_decimals = _erc20_decimals_from_env(token_out)
        token0_decimals = _erc20_decimals_from_env(token0_addr)
        token1_decimals = _erc20_decimals_from_env(token1_addr)

        Q96 = 2**96
        sqrt_price = float(sqrt_price_x96) / Q96
        price_raw = sqrt_price * sqrt_price
        price_token1_per_token0 = float(price_raw) * (
            10 ** (token0_decimals - token1_decimals)
        )

        if token_in_norm == token0_norm and token_out_norm == token1_norm:
            price_out_per_in = float(price_token1_per_token0)
            logger.info(
                f"{log_name}: Price calculated (token_in=token0): {price_out_per_in:.8f}"
            )
        elif token_in_norm == token1_norm and token_out_norm == token0_norm:
            price_out_per_in = (
                1.0 / float(price_token1_per_token0)
                if price_token1_per_token0 > 0
                else 0.0
            )
            logger.info(
                f"{log_name}: Price calculated (token_in=token1): {price_out_per_in:.8f}"
            )
        else:
            logger.info(
                f"{log_name}: Token order mismatch - token_in={token_in_norm[:10]}..., "
                f"token_out={token_out_norm[:10]}..., pool_token0={token0_norm[:10]}..., "
                f"pool_token1={token1_norm[:10]}..."
            )
            return None

        if price_out_per_in <= 0:
            logger.info(f"{log_name}: price_out_per_in <= 0 ({price_out_per_in})")
            return None

        # SlipStream fee tier (default 500 = 0.05%)
        fee_tier = 500
        try:
            raw_fee = os.getenv("DIEM_USDC_POOL_FEE") or ""
            if raw_fee.strip():
                fee_tier = int(raw_fee.strip())
        except Exception:
            fee_tier = 500
        fee_multiplier = 1.0 + (float(fee_tier) / 1_000_000.0)

        def _log_dust(min_required: int) -> None:
            # Use DEBUG level - dust rejections are expected during price discovery
            try:
                logger.debug(
                    f"{log_name}: dust_amount (mode={mode}) amount={amount} min_required={min_required}",
                    extra={
                        "status": "dust_amount",
                        "mode": mode,
                        "amount": int(amount),
                        "min_required": int(min_required),
                        "token_in": token_in_norm,
                        "token_out": token_out_norm,
                    },
                )
            except Exception:
                logger.debug(
                    f"{log_name}: dust_amount (mode={mode}) amount={amount} min_required={min_required}"
                )

        if mode == "exact_in":
            try:
                min_out_decimal = 1.0 / float(10**token_out_decimals)
                min_in_decimal = (min_out_decimal * fee_multiplier) / price_out_per_in
                min_in_units = int(
                    math.ceil(min_in_decimal * float(10**token_in_decimals))
                )
                if min_in_units > 0 and amount < min_in_units:
                    _log_dust(min_in_units)
                    return None
            except Exception:
                pass
            amount_in_decimal = float(amount) / (10**token_in_decimals)
            amount_out_decimal = (amount_in_decimal * price_out_per_in) / fee_multiplier
            amount_out = int(amount_out_decimal * (10**token_out_decimals))

            if amount_out <= 0:
                logger.info(
                    f"{log_name}: exact_in failed - amount_out <= 0 ({amount_out})"
                )
                return None

            quote_amount_in = int(amount)
            quote_amount_out = int(amount_out)
        else:
            # exact_out: calculate required input for desired output
            logger.info(
                f"{log_name}: exact_out calc - amount={amount}, token_out_dec={token_out_decimals}, "
                f"token_in_dec={token_in_decimals}, price={price_out_per_in:.8f}"
            )
            try:
                min_in_decimal = 1.0 / float(10**token_in_decimals)
                min_out_decimal = (min_in_decimal * price_out_per_in) / fee_multiplier
                min_out_units = int(
                    math.ceil(min_out_decimal * float(10**token_out_decimals))
                )
                if min_out_units > 0 and amount < min_out_units:
                    _log_dust(min_out_units)
                    return None
            except Exception:
                pass
            amount_out_decimal = float(amount) / (10**token_out_decimals)
            amount_in_decimal = (amount_out_decimal / price_out_per_in) * fee_multiplier
            amount_in = int(amount_in_decimal * (10**token_in_decimals))

            logger.info(
                f"{log_name}: exact_out result - out_dec={amount_out_decimal:.8f}, "
                f"in_dec={amount_in_decimal:.8f}, amount_in={amount_in}"
            )

            if amount_in <= 0:
                logger.info(
                    f"{log_name}: exact_out failed - amount_in <= 0 ({amount_in})"
                )
                return None

            quote_amount_in = int(amount_in)
            quote_amount_out = int(amount)

        route = make_route([token_in, token_out])
        try:
            from libs.dex.providers import Quote as _Quote
        except Exception:
            return None

        quote = _Quote(
            provider=provider_name,
            amount_in=quote_amount_in,
            amount_out=quote_amount_out,
            route=route,
            executable=bool(executable),
        )
        try:
            quote.pool_address = pool_addr
            quote.slot0_price = price_out_per_in
            quote.slot0_fee = fee_tier
        except Exception:
            pass

        logger.info(
            f"{log_name}: SUCCESS in={quote_amount_in}, out={quote_amount_out}, "
            f"price={price_out_per_in:.6f}, fee={fee_tier}bps, mode={mode}"
        )
        return quote
    except Exception as e:
        try:
            from libs.telemetry.logger import get_logger as _get_logger

            _get_logger("diem.fallbacks").info(
                f"{log_name}: Exception during slot0 quote: {e}"
            )
        except Exception:
            pass
        return None


def diem_usdc_slot0_quote(
    amount_in: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Executable slot0-based quote for DIEM/USDC direct route (exact-in).

    Uses the DIEM/USDC Aerodrome SlipStream pool configured via DIEM_USDC_POOL_ADDRESS.
    """
    return _diem_usdc_slot0_quote(
        amount=amount_in,
        token_in=token_in,
        token_out=token_out,
        mode="exact_in",
        provider_name="aerodrome_cl",
        executable=True,
        log_name="diem_usdc_slot0_quote",
    )


def diem_usdc_slot0_quote_exact_out(
    amount_out: int,
    token_in: str,
    token_out: str,
) -> Quote | None:
    """
    Preview-only slot0-based quote for DIEM/USDC direct route (exact-out).

    Uses the DIEM/USDC Aerodrome SlipStream pool configured via DIEM_USDC_POOL_ADDRESS.
    """
    return _diem_usdc_slot0_quote(
        amount=amount_out,
        token_in=token_in,
        token_out=token_out,
        mode="exact_out",
        provider_name="aerodrome_cl",
        executable=False,
        log_name="diem_usdc_slot0_quote_exact_out",
    )


def build_diem_route_preferences(
    token_in: str,
    token_out: str,
    config=None,
) -> list[RoutePlan]:
    """
    Build DIEM route preferences with policy: prefer high-liquidity routes.

    Returns routes in priority order:
    1. DIEM→VVV→USDC (2-hop via VVV direct, if VVV/USDC has liquidity)
    2. DIEM→VVV→WETH→USDC (3-hop via WETH, uses high-liquidity Aerodrome pools)
    3. DIEM→WETH→USDC (2-hop via WETH, fallback)

    Controlled by:
    - DIEM_MAX_ROUTE_HOPS (default 2) - cap route hops (set to 3 for three-hop when SlipStream support is added)
    - DIEM_ROUTE_AVOID_WETH (default 0) - if 1, skip WETH routes when VVV route available
    - DIEM_ENABLE_THREE_HOP_WETH (default 1) - enable 3-hop VVV→WETH→USDC route
    """
    if config is None:
        config = load_env_config()

    diem_addr = _normalize_address(
        config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or ""
    )
    vvv_addr = _normalize_address(
        config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or ""
    )
    quote_addr = _normalize_address(
        config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or ""
    )
    weth_addr = _normalize_address(
        os.getenv("WETH_ADDRESS") or "0x4200000000000000000000000000000000000006"
    )

    if not diem_addr or not quote_addr:
        return []

    token_in_norm = _normalize_address(token_in)
    token_out_norm = _normalize_address(token_out)

    routes: list[RoutePlan] = []

    # Get max hops config (default 2; set to 3 to enable three-hop routes when SlipStream support is added)
    max_hops = 2
    try:
        max_hops = int(os.getenv("DIEM_MAX_ROUTE_HOPS", "2") or 2)
        max_hops = max(2, min(max_hops, 4))  # Clamp between 2 and 4
    except Exception:
        pass

    avoid_weth = os.getenv("DIEM_ROUTE_AVOID_WETH", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # Check if canonical WETH routes should be completely disabled
    disable_canonical_weth = os.getenv(
        "DIEM_DISABLE_CANONICAL_WETH", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Check if three-hop WETH route is enabled (default: disabled until SlipStream support)
    enable_three_hop_weth = os.getenv(
        "DIEM_ENABLE_THREE_HOP_WETH", "0"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Get VVV/WETH pool config for three-hop routes
    vvv_weth_pool = (
        getattr(config, "vvv_weth_pool", None)
        or os.getenv("VVV_WETH_POOL_ADDRESS")
        or ""
    )
    vvv_weth_fee = 500  # Default 0.05% for Aerodrome SlipStream
    try:
        vvv_weth_fee = int(os.getenv("VVV_WETH_POOL_FEE") or "500")
    except Exception:
        pass

    # DIEM -> USDC: prefer direct route if available, then VVV route, then WETH route
    if token_in_norm == diem_addr and token_out_norm == quote_addr:
        # Priority 0: DIEM→USDC (direct via Aerodrome SlipStream if configured)
        # This pool has ~$121K liquidity and is the most efficient route
        diem_usdc_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
        prefer_direct = os.getenv("DIEM_PREFER_DIRECT_ROUTE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if prefer_direct and diem_usdc_pool:
            try:
                diem_usdc_fee = None
                try:
                    fee_str = os.getenv("DIEM_USDC_POOL_FEE") or "500"
                    diem_usdc_fee = int(fee_str)
                except Exception:
                    diem_usdc_fee = 500  # SlipStream default 0.05%
                route = make_route([token_in, token_out], fees=[diem_usdc_fee])
                routes.append(route)
            except Exception:
                pass

        if max_hops >= 2:
            # Priority 1: DIEM→VVV→USDC (if VVV available and VVV/USDC pool exists)
            if vvv_addr and config.diem_vvv_pair and config.vvv_usdc_pool:
                try:
                    vvv_usdc_fee = None
                    try:
                        fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
                        vvv_usdc_fee = int(fee_str)
                    except Exception:
                        vvv_usdc_fee = 3000
                    route = make_route(
                        [token_in, vvv_addr, token_out], fees=[None, vvv_usdc_fee]
                    )
                    routes.append(route)
                except Exception:
                    pass

            # Priority 2: DIEM→VVV→WETH→USDC (3-hop via high-liquidity Aerodrome pools)
            # This uses: DIEM/VVV Aerodrome ($910K) + VVV/WETH Aerodrome ($5.4M) + WETH/USDC ($20M+)
            # Skip when DIEM_DISABLE_CANONICAL_WETH is set (disables all WETH routes)
            if (
                not disable_canonical_weth
                and max_hops >= 3
                and enable_three_hop_weth
                and vvv_addr
                and weth_addr
                and vvv_weth_pool
            ):
                try:
                    route = make_route(
                        [token_in, vvv_addr, weth_addr, token_out],
                        fees=[None, vvv_weth_fee, None],
                    )
                    routes.append(route)
                except Exception:
                    pass

            # Priority 3: DIEM→WETH→USDC (only if not avoiding WETH or other routes unavailable)
            # Skip entirely when DIEM_DISABLE_CANONICAL_WETH is set
            if (
                not disable_canonical_weth
                and (not avoid_weth or not routes)
                and weth_addr
            ):
                try:
                    route = make_route([token_in, weth_addr, token_out])
                    routes.append(route)
                except Exception:
                    pass

    # USDC -> DIEM: prefer direct route if available, then VVV route, then WETH route
    elif token_in_norm == quote_addr and token_out_norm == diem_addr:
        # Priority 0: USDC→DIEM (direct via Aerodrome SlipStream if configured)
        # This pool has ~$121K liquidity and is the most efficient route
        diem_usdc_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
        prefer_direct = os.getenv("DIEM_PREFER_DIRECT_ROUTE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if prefer_direct and diem_usdc_pool:
            try:
                diem_usdc_fee = None
                try:
                    fee_str = os.getenv("DIEM_USDC_POOL_FEE") or "500"
                    diem_usdc_fee = int(fee_str)
                except Exception:
                    diem_usdc_fee = 500  # SlipStream default 0.05%
                route = make_route([token_in, token_out], fees=[diem_usdc_fee])
                routes.append(route)
            except Exception:
                pass

        if max_hops >= 2:
            # Priority 1: USDC→VVV→DIEM (if VVV available)
            if vvv_addr and config.diem_vvv_pair and config.vvv_usdc_pool:
                try:
                    vvv_usdc_fee = None
                    try:
                        fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
                        vvv_usdc_fee = int(fee_str)
                    except Exception:
                        vvv_usdc_fee = 3000
                    route = make_route(
                        [token_in, vvv_addr, token_out], fees=[vvv_usdc_fee, None]
                    )
                    routes.append(route)
                except Exception:
                    pass

            # Priority 2: USDC→WETH→VVV→DIEM (3-hop via high-liquidity Aerodrome pools)
            # This uses: WETH/USDC ($20M+) + VVV/WETH Aerodrome ($5.4M) + DIEM/VVV Aerodrome ($910K)
            # Skip when DIEM_DISABLE_CANONICAL_WETH is set (disables all WETH routes)
            if (
                not disable_canonical_weth
                and max_hops >= 3
                and enable_three_hop_weth
                and vvv_addr
                and weth_addr
                and vvv_weth_pool
            ):
                try:
                    route = make_route(
                        [token_in, weth_addr, vvv_addr, token_out],
                        fees=[None, vvv_weth_fee, None],
                    )
                    routes.append(route)
                except Exception:
                    pass

            # Priority 3: USDC→WETH→DIEM (only if not avoiding WETH or other routes unavailable)
            # Skip entirely when DIEM_DISABLE_CANONICAL_WETH is set
            if (
                not disable_canonical_weth
                and (not avoid_weth or not routes)
                and weth_addr
            ):
                try:
                    route = make_route([token_in, weth_addr, token_out])
                    routes.append(route)
                except Exception:
                    pass

    return routes


def check_reserve_fallback_available() -> dict:
    """
    Self-check helper to verify reserve math fallback is enabled and working.

    Returns a dict with:
    - enabled: bool - Whether DIEM_ENABLE_PAIR_MATH_FALLBACK is set
    - reserves_available: bool - Whether reserves can be fetched
    - test_quote: Optional[Quote] - A test quote if both enabled and reserves available
    - error: Optional[str] - Error message if check fails
    """
    result = {
        "enabled": False,
        "reserves_available": False,
        "test_quote": None,
        "error": None,
    }

    # Check if fallback is enabled
    fallback_enabled = os.getenv(
        "DIEM_ENABLE_PAIR_MATH_FALLBACK", "0"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    result["enabled"] = fallback_enabled

    if not fallback_enabled:
        result["error"] = "DIEM_ENABLE_PAIR_MATH_FALLBACK is not enabled"
        return result

    # Try to get reserves
    try:
        reserves_data = _get_diem_vvv_reserves()
        if reserves_data:
            result["reserves_available"] = True
            reserve0, reserve1, token0_addr, token1_addr = reserves_data

            # Try to generate a small test quote (1e15 base units = 0.001 tokens)
            test_amount_out = 1000000000000000  # 0.001 tokens
            if reserve1 > test_amount_out:  # Use reserve1 as output reserve
                test_quote = diem_vvv_quote_from_reserves(
                    test_amount_out,
                    token0_addr,
                    token1_addr,
                )
                if test_quote:
                    result["test_quote"] = {
                        "amount_in": test_quote.amount_in,
                        "amount_out": test_quote.amount_out,
                        "provider": test_quote.provider,
                    }
            elif reserve0 > test_amount_out:  # Try reverse direction
                test_quote = diem_vvv_quote_from_reserves(
                    test_amount_out,
                    token1_addr,
                    token0_addr,
                )
                if test_quote:
                    result["test_quote"] = {
                        "amount_in": test_quote.amount_in,
                        "amount_out": test_quote.amount_out,
                        "provider": test_quote.provider,
                    }
        else:
            result["error"] = "Could not fetch DIEM/VVV reserves from on-chain"
    except Exception as exc:
        result["error"] = f"Error checking reserves: {exc}"

    return result
