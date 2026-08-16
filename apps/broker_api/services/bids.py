"""
Bids helper functions for Venice Broker API.

Handles EIP-712 signature verification, asset price conversion, and bid classification.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

if TYPE_CHECKING:
    from services.marketdata.provider import MarketDataProvider

logger = logging.getLogger("broker.api.services.bids")


def expiry_as_utc(value: datetime | float | None) -> datetime | None:
    """Interpret stored bid expiry as UTC, including naive datetimes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def expiry_epoch(value: datetime | float | None) -> int:
    aware = expiry_as_utc(value)
    if aware is None:
        return 0
    return int(aware.timestamp())


def recover_buyer(
    req: Any,
    sign_domain_name: str,
    sign_domain_version: str,
    chain_id_env: str,
) -> str:
    """
    Recover buyer address from EIP-712 signature.

    Constructs EIP-712 typed data for PurchaseIntent and recovers
    the signer address from the signature.

    Args:
        req: BidRequest object with buyer, units, maxPrice, asset, expiry, etc.
        sign_domain_name: EIP-712 domain name (e.g., "Venice Broker")
        sign_domain_version: EIP-712 domain version (e.g., "1")
        chain_id_env: Expected chain ID from environment

    Returns:
        Recovered buyer address (lowercase, 0x-prefixed)

    Raises:
        HTTPException: If signature is invalid or dependencies unavailable
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"eth-account unavailable: {e}")

    # Construct EIP-712 typed data
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "PurchaseIntent": [
                {"name": "buyer", "type": "address"},
                {"name": "units", "type": "uint256"},
                {"name": "maxPrice", "type": "uint256"},
                {"name": "asset", "type": "string"},
                {"name": "expiry", "type": "uint256"},
                {"name": "slippageBps", "type": "uint16"},
                {"name": "nonce", "type": "uint256"},
                {"name": "chainId", "type": "uint256"},
            ],
        },
        "primaryType": "PurchaseIntent",
        "domain": {
            "name": sign_domain_name,
            "version": sign_domain_version,
            "chainId": int(req.chainId),
        },
        "message": {
            "buyer": req.buyer,
            "units": int(req.units),
            "maxPrice": int(req.maxPrice),
            "asset": str(req.asset),
            "expiry": int(req.expiry),
            "slippageBps": int(req.slippageBps),
            "nonce": int(req.nonce),
            "chainId": int(req.chainId),
        },
    }

    if int(req.chainId) != int(chain_id_env):
        raise HTTPException(
            status_code=400,
            detail=f"chainId mismatch: got {req.chainId}, expected {chain_id_env}",
        )

    try:
        msg = encode_typed_data(full_message=typed)
        addr = Account.recover_message(msg, signature=req.signature)
        return "0x" + str(addr).lower().removeprefix("0x")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid signature: {e}")


def price_usdc_per_unit_from_asset(
    max_price_minor: int,
    asset: str,
    get_marketdata_provider: Callable[[], MarketDataProvider],
) -> float:
    """
    Convert asset price from minor units to USDC per unit.

    Handles USDC (6 decimals) and ETH (18 decimals) with market price lookup.
    Falls back to USDC conversion for unsupported assets.

    Args:
        max_price_minor: Price in minor units (wei for ETH, 1e6 for USDC)
        asset: Asset symbol (e.g., "USDC", "ETH")
        get_marketdata_provider: Function to get MarketDataProvider instance

    Returns:
        Price in USDC per unit (float)
    """
    try:
        a = str(asset or "").upper()

        if a == "USDC":
            return float(max_price_minor) / 1_000_000.0

        if a == "ETH":
            try:
                mdp = get_marketdata_provider()
            except HTTPException as http_exc:
                raise RuntimeError(
                    f"marketdata unavailable: {http_exc.detail}"
                ) from http_exc

            px = mdp.prices(["ETH"]) or {}
            eth_usd = float(px.get("ETH") or 0.0)

            if eth_usd <= 0:
                raise RuntimeError("ETH price unavailable")

            return (float(max_price_minor) / 1e18) * float(eth_usd)

        if a == "WBTC":
            try:
                mdp = get_marketdata_provider()
            except HTTPException as http_exc:
                raise RuntimeError(
                    f"marketdata unavailable: {http_exc.detail}"
                ) from http_exc

            px = mdp.prices(["WBTC"]) or {}
            wbtc_usd = float(px.get("WBTC") or 0.0)

            if wbtc_usd <= 0:
                raise RuntimeError("WBTC price unavailable")

            return (float(max_price_minor) / 1e8) * float(wbtc_usd)

        # Default/unsupported asset: treat as USDC (conservative)
        return float(max_price_minor) / 1_000_000.0
    except Exception as e:
        logger.warning("bids: price convert failed: %s", e)
        return 0.0


def classify_bid_status(
    max_price_usdc: float,
    now_s: int,
    expiry_s: int,
    compute_clearing_price: Callable[[], dict],
) -> tuple[str, dict]:
    """
    Classify bid status based on clearing price and expiry.

    Returns status as one of:
    - "expired": Bid past expiry time
    - "out_of_band": Price below clearing band minimum
    - "accepted_window": Price at or above clearing price
    - "in_band": Price within clearing band but below clearing price
    - "received": Clearing price unavailable (fallback)

    Args:
        max_price_usdc: Maximum price willing to pay in USDC per unit
        now_s: Current timestamp in seconds
        expiry_s: Bid expiry timestamp in seconds
        compute_clearing_price: Function that returns clearing price dict

    Returns:
        Tuple of (status, context_dict)
    """
    # Check expiry first
    if now_s >= expiry_s:
        return "expired", {"reason": "time"}

    # Try to get clearing price
    try:
        cp = compute_clearing_price()
        price = float(cp.get("price") or 0.0)
        lo = float(cp.get("bandMin") or 0.0)
        ctx = {"clearing": cp}

        if max_price_usdc < lo:
            return "out_of_band", ctx
        if max_price_usdc >= price:
            return "accepted_window", ctx
        return "in_band", ctx
    except Exception:
        # Degrade gracefully if clearing price unavailable
        return "received", {"reason": "no_clearing"}
