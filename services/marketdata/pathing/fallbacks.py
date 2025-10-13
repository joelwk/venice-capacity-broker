from __future__ import annotations

import os
from typing import Callable, Dict, Optional

import requests

from .env import EnvConfig
from .models import GuardrailContext, PolicyContext, QuoteMode, QuoteResult


def _valid_price(value: Optional[float]) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v > 0 and v < 1e6


def _normalize(addr: Optional[str]) -> str:
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
        from libs.agentkit_ext.web3_utils import get_contract, get_web3
        from web3 import Web3  # type: ignore

        w3 = get_web3()
        erc20 = get_contract(w3, Web3.to_checksum_address(norm), "erc20.json")
        return int(erc20.functions.decimals().call())
    except Exception:
        return 18


def _rpc_url() -> Optional[str]:
    try:
        from libs.agentkit_ext.web3_utils import resolve_rpc_url  # type: ignore

        return resolve_rpc_url()
    except Exception:
        return os.getenv("BASE_RPC_URL")


def bridge_vvv_price(config: EnvConfig) -> Optional[float]:
    pair_addr = config.diem_vvv_pair
    pool_addr = config.vvv_usdc_pool
    if not pair_addr or not pool_addr:
        return None

    rpc_url = _rpc_url()
    if not rpc_url:
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
            if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
                return b""
            return bytes.fromhex(result[2:])
        except Exception:
            return b""

    try:
        pair = _normalize(pair_addr)
        pool = _normalize(pool_addr)

        reserves_raw = _call(pair, "0x0902f1ac")
        if len(reserves_raw) < 96:
            return None
        reserve0 = int.from_bytes(reserves_raw[0:32], "big")
        reserve1 = int.from_bytes(reserves_raw[32:64], "big")

        token0_raw = _call(pair, "0x0dfe1681")
        token1_raw = _call(pair, "0xd21220a7")
        if len(token0_raw) < 32 or len(token1_raw) < 32:
            return None
        token0 = _normalize("0x" + token0_raw[-20:].hex())
        token1 = _normalize("0x" + token1_raw[-20:].hex())

        diem_addr = _normalize(config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or "")
        vvv_addr = _normalize(config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or "")
        quote_addr = _normalize(config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or "")

        if not diem_addr or not vvv_addr or not quote_addr:
            return None
        if token0 not in {diem_addr, vvv_addr} or token1 not in {diem_addr, vvv_addr}:
            return None

        dec0 = _erc20_decimals_from_env(token0)
        dec1 = _erc20_decimals_from_env(token1)
        r0 = reserve0 / float(10 ** dec0)
        r1 = reserve1 / float(10 ** dec1)

        if token0 == vvv_addr and token1 == diem_addr:
            vvv_per_diem = r0 / r1 if r1 > 0 else 0.0
        elif token0 == diem_addr and token1 == vvv_addr:
            vvv_per_diem = r1 / r0 if r0 > 0 else 0.0
        else:
            return None
        if not _valid_price(vvv_per_diem):
            return None

        slot0_raw = _call(pool, "0x3850c7bd")
        if len(slot0_raw) < 32:
            return None
        sqrt_price_x96 = int.from_bytes(slot0_raw[0:32], "big")
        if sqrt_price_x96 <= 0:
            return None

        pool_token0_raw = _call(pool, "0x0dfe1681")
        pool_token1_raw = _call(pool, "0xd21220a7")
        if len(pool_token0_raw) < 32 or len(pool_token1_raw) < 32:
            return None
        pool_token0 = _normalize("0x" + pool_token0_raw[-20:].hex())
        pool_token1 = _normalize("0x" + pool_token1_raw[-20:].hex())

        dec_pool_0 = _erc20_decimals_from_env(pool_token0)
        dec_pool_1 = _erc20_decimals_from_env(pool_token1)
        ratio = sqrt_price_x96 / float(1 << 96)
        price_token1_per_token0 = ratio * ratio
        price_token1_per_token0 *= float(pow(10.0, dec_pool_0 - dec_pool_1))

        if pool_token0 == quote_addr and pool_token1 == vvv_addr:
            vvv_per_quote = price_token1_per_token0
            if not _valid_price(vvv_per_quote):
                return None
            quote_per_vvv = 1.0 / vvv_per_quote if vvv_per_quote > 0 else 0.0
        elif pool_token0 == vvv_addr and pool_token1 == quote_addr:
            quote_per_vvv = price_token1_per_token0
        else:
            return None
        if not _valid_price(quote_per_vvv):
            return None
        price_diem_quote = vvv_per_diem * quote_per_vvv
        if not _valid_price(price_diem_quote):
            return None
        return float(price_diem_quote)
    except Exception:
        return None


def bridge_fallback(
    *,
    amount_in: int,
    config: EnvConfig,
    guardrails: GuardrailContext,
    policy: PolicyContext,
    mode: QuoteMode,
) -> Optional[QuoteResult]:
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
    fetcher: Callable[[str], Optional[float]],
    guardrails: GuardrailContext,
    policy: PolicyContext,
    mode: QuoteMode,
    token_symbol: Optional[str] = None,
) -> Optional[QuoteResult]:
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


__all__ = ["bridge_vvv_price", "bridge_fallback", "external_reference_fallback"]
