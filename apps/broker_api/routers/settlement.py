from __future__ import annotations

import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query

from ..models import DexPreviewResponse, PurchaseStatus, PurchaseVerifyRequest, SettleResponse

router = APIRouter(prefix="/v1/settlement", tags=["settlement"])

_settlement_enabled: bool
_has_sql_bids: bool
_get_sess: Any
_sel: Any
_DbBid: Any
_settle_pricing: Any
_verify_purchase: Callable[[PurchaseVerifyRequest], dict]
_get_marketdata_provider: Callable[[int], Any]
_logger: Any


def init_router(
    *,
    settlement_enabled: bool,
    has_sql_bids: bool,
    get_sess: Any,
    select_func: Any,
    bid_model: Any,
    settle_pricing: Any,
    verify_purchase: Callable[[PurchaseVerifyRequest], dict],
    get_marketdata_provider: Callable[[int], Any],
    logger: Any,
) -> APIRouter:
    global _settlement_enabled, _has_sql_bids, _get_sess, _sel, _DbBid
    global _settle_pricing, _verify_purchase, _get_marketdata_provider, _logger
    
    _settlement_enabled = settlement_enabled
    _has_sql_bids = has_sql_bids
    _get_sess = get_sess
    _sel = select_func
    _DbBid = bid_model
    _settle_pricing = settle_pricing
    _verify_purchase = verify_purchase
    _get_marketdata_provider = get_marketdata_provider
    _logger = logger
    return router


@router.post("/confirm", response_model=PurchaseStatus)
def settlement_confirm(req: PurchaseVerifyRequest) -> dict:
    """Alias for settlement confirm to purchase verify (shape-compatible)."""
    if not _settlement_enabled:
        raise HTTPException(status_code=404, detail="settlement disabled")
    # Delegate to the same verification logic as /v1/purchases/verify
    return _verify_purchase(req)


@router.post("/{bid_id}/settle", response_model=SettleResponse)
def bids_settle(bid_id: str, asset: str | None = None) -> dict:
    """Settle a bid and generate a quote."""
    if not _settlement_enabled:
        raise HTTPException(status_code=404, detail="settlement disabled")
    if _settle_pricing is None:
        raise HTTPException(status_code=503, detail="pricing service unavailable")
    if not _has_sql_bids:
        raise HTTPException(status_code=503, detail="SQL dependencies unavailable")
    
    now_s = int(time.time())
    with next(_get_sess()) as s:  # type: ignore[call-arg]
        b = s.exec(_sel(_DbBid).where(_DbBid.bid_id == bid_id)).first()  # type: ignore[misc]
        if b is None:
            raise HTTPException(status_code=404, detail="bid not found")
        if b.expiry and int(b.expiry.timestamp()) <= now_s:
            raise HTTPException(status_code=400, detail="bid expired")
        
        pay_asset = (asset or b.asset or "USDC").upper()
        if pay_asset not in {"ETH", "USDC"}:
            raise HTTPException(status_code=400, detail="unsupported asset for settlement")
        if str(pay_asset) != str(b.asset or "").upper():
            raise HTTPException(status_code=400, detail="asset must match bid asset")
        
        q = _settle_pricing.get_quote(units=float(b.units), asset=pay_asset)
        if int(q.get("unitPrice") or 0) > int(b.max_price):
            raise HTTPException(status_code=409, detail="price exceeds bid max")
        
        try:
            from libs.telemetry.events import emit as _emit
            _emit("settlement.quote", {"bidId": bid_id, **q})
        except Exception:
            pass
        return q


@router.get("/quote", response_model=DexPreviewResponse)
def settlement_preview(
    fromToken: str = Query(..., description="ERC-20 address to swap from"),
    toAsset: str = Query(..., description="ETH or USDC (treasury asset)"),
    amountOut: int = Query(..., description="Desired output amount in minor units (wei or 6dp)"),
    path: str | None = Query(None, description="Optional CSV path override: addr0,addr1,[addr2]"),
) -> dict:
    """Preview an exact-out swap to fund payment in ETH or USDC.

    - Uses UniswapV2 exact-out; skips Aerodrome by design.
    - When quotes unavailable, falls back to mid-price estimate and marks approx=true.
    """
    if not _settlement_enabled:
        raise HTTPException(status_code=404, detail="settlement disabled")
    
    import os as __os
    
    frm = (fromToken or "").strip()
    if not frm or not frm.startswith("0x"):
        raise HTTPException(status_code=400, detail="invalid fromToken")
    asset_u = (toAsset or "USDC").strip().upper()
    if asset_u not in {"ETH", "USDC"}:
        raise HTTPException(status_code=400, detail="toAsset must be ETH or USDC")
    amt_out = int(amountOut)
    if amt_out <= 0:
        raise HTTPException(status_code=400, detail="amountOut must be > 0")
    
    # Resolve target token address (WETH for ETH)
    if asset_u == "ETH":
        to_token = (__os.getenv("WETH_ADDRESS") or "0x4200000000000000000000000000000000000006").strip()
    else:
        to_token = (__os.getenv("USDC_ADDRESS") or __os.getenv("QUOTE_TOKEN_ADDRESS") or "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913").strip()
    if not to_token:
        raise HTTPException(status_code=400, detail="target token address not configured")
    
    # Build path (override > default heuristics)
    if path and path.strip():
        p = [a.strip() for a in path.split(",") if a.strip()]
    else:
        # Prefer direct hop first; if not viable, client can retry with an explicit path
        p = [frm, to_token]
        # If same token, no swap needed
        if p[0].lower() == p[-1].lower():
            return {
                "provider": None,
                "fromToken": frm,
                "toToken": to_token,
                "toAsset": asset_u,
                "path": p,
                "amountIn": 0,
                "amountOut": amt_out,
                "expiresAt": int(time.time()) + 60,
                "approx": True,
                "slippageBps": 0,
            }
    
    # Try aggregator exact-out
    risk_violation_detail: Optional[str] = None
    from libs.dex.providers import build_aggregator_from_env as _build
    try:
        agg = _build()
        q = agg.best_quote_exact_out(amt_out, p)
    except Exception:
        q = None
    if q is not None:
        slip_bps_val: Optional[int] = None
        pool_take_bps_val: Optional[int] = None
        try:
            import os
            from services.marketdata.etherscan_verify import get_cached_pair_info_for_tokens
            from services.risk.policy import RiskPolicy as _RP

            mdp2 = _get_marketdata_provider(500)
            policy = _RP.from_env()

            def _token_decimals(addr: str) -> int:
                try:
                    return int(mdp2._erc20_decimals(addr))  # type: ignore[attr-defined]
                except Exception:
                    return 18

            def _normalized_exec_price(amount_in: int, dec_in: int, amount_out: int, dec_out: int) -> float | None:
                try:
                    ain = float(amount_in) / float(10 ** int(dec_in))
                    aout = float(amount_out) / float(10 ** int(dec_out))
                    if ain <= 0 or aout <= 0:
                        return None
                    return aout / ain
                except Exception:
                    return None

            slip_candidates: list[dict] = []
            di = _token_decimals(frm)
            do = _token_decimals(to_token)
            exec_price_norm = _normalized_exec_price(int(q.amount_in), di, int(q.amount_out), do)
            ref_price_norm = None
            try:
                ref_price_norm = mdp2._mid_price_from_reserves(frm, to_token)  # type: ignore[attr-defined]
            except Exception:
                ref_price_norm = None
            if not ref_price_norm or ref_price_norm <= 0:
                try:
                    bt2 = mdp2._weth_address()
                    p1_ = mdp2._mid_price_from_reserves(frm, bt2)  # type: ignore[attr-defined]
                    p2_ = mdp2._mid_price_from_reserves(bt2, to_token)  # type: ignore[attr-defined]
                    ref_price_norm = (p1_ or 0.0) * (p2_ or 0.0)
                except Exception:
                    ref_price_norm = None
            simple_slip_bps: Optional[int] = None
            if float(q.amount_in) > 0 and ref_price_norm and ref_price_norm > 0:
                try:
                    exec_simple = float(q.amount_out) / float(q.amount_in)
                    simple_slip_bps = int(round(max(0.0, (float(ref_price_norm) - float(exec_simple)) / float(ref_price_norm) * 10000.0)))
                    _logger.debug("exec_simple=%s ref=%s simple_slip=%s", exec_simple, ref_price_norm, simple_slip_bps)
                    slip_candidates.append(policy.check_slippage(exec_simple, ref_price_norm))
                except Exception:
                    pass
            first_hop = q.path[:2] if len(q.path) >= 2 else [frm, to_token]
            cap_units = None
            try:
                cap_units = mdp2.reserve_cap_units(first_hop)  # type: ignore[attr-defined]
            except Exception:
                cap_units = None
            cached = None
            try:
                cached = get_cached_pair_info_for_tokens(first_hop[0], first_hop[1])
            except Exception:
                cached = None
            input_reserve = None
            output_reserve = None
            if cached and cached.get("reserves"):
                reserves = cached["reserves"]
                tok0 = str(cached.get("token0") or "")
                tok1 = str(cached.get("token1") or "")
                if tok0 and tok1:
                    if tok0.lower() == first_hop[0].lower():
                        input_reserve = float(reserves[0])
                        output_reserve = float(reserves[1])
                    elif tok1.lower() == first_hop[0].lower():
                        input_reserve = float(reserves[1])
                        output_reserve = float(reserves[0])
            exec_price_raw = None
            ref_price_raw = None
            if input_reserve and output_reserve and float(q.amount_in) > 0:
                exec_price_raw = float(q.amount_out) / float(q.amount_in)
                if input_reserve > 0:
                    ref_price_raw = float(output_reserve) / float(input_reserve)
                pool_take_bps_val = int(round((float(q.amount_in) / float(input_reserve)) * 10000))
            if exec_price_norm is not None and ref_price_norm and ref_price_norm > 0:
                slip_candidates.append(policy.check_slippage(exec_price_norm, ref_price_norm))
            if exec_price_raw is not None and ref_price_raw and ref_price_raw > 0:
                slip_candidates.append(policy.check_slippage(exec_price_raw, ref_price_raw))
            max_slippage_bps = int(policy.slippage_bps_cap)
            if simple_slip_bps is not None:
                _logger.debug("simple_slip=%s cap=%s", simple_slip_bps, max_slippage_bps)
                if simple_slip_bps > max_slippage_bps:
                    risk_violation_detail = f"slippage {simple_slip_bps} bps exceeds cap of {max_slippage_bps} bps"
            if slip_candidates:
                worst = max(slip_candidates, key=lambda r: float(r.get("slippage_bps", 0.0) or 0.0))
                slip_bps_val = int(round(float(worst.get("slippage_bps", 0.0) or 0.0)))
                if slip_bps_val > max_slippage_bps or not bool(worst.get("ok", True)):
                    _logger.info("slippage clamp: %s bps > cap %s", slip_bps_val, max_slippage_bps)
                    risk_violation_detail = f"slippage {slip_bps_val} bps exceeds cap of {max_slippage_bps} bps"
            if cap_units is not None and int(q.amount_in) > int(cap_units):
                max_pool_take_bps = int(os.getenv("RISK_MAX_POOL_TAKE_BPS", "100"))
                if pool_take_bps_val is not None:
                    risk_violation_detail = f"input exceeds pool take cap: {pool_take_bps_val} bps > {max_pool_take_bps} bps allowed"
                else:
                    risk_violation_detail = "input exceeds pool take cap"
        except HTTPException:
            raise
        except Exception:
            slip_bps_val = None
            pool_take_bps_val = None
        if risk_violation_detail:
            _logger.info("risk violation detected: %s", risk_violation_detail)
            raise HTTPException(status_code=409, detail=risk_violation_detail)
        return {
            "provider": q.provider,
            "fromToken": frm,
            "toToken": to_token,
            "toAsset": asset_u,
            "path": q.path,
            "amountIn": int(q.amount_in),
            "amountOut": int(q.amount_out),
            "expiresAt": int(time.time()) + 60,
            "approx": False,
            "slippageBps": slip_bps_val,
            "poolTakeBps": pool_take_bps_val,
        }
    
    # Approximate via mid-price (constant-product small-size approximation)
    try:
        import math
        from services.risk.policy import RiskPolicy as _RP
        mdp = _get_marketdata_provider(500)
        policy = _RP.from_env()
        # Compute mid price fromToken->toToken; if 0, try via WETH bridge
        price = mdp._mid_price_from_reserves(frm, to_token)  # type: ignore[attr-defined]
        if not price or price <= 0:
            # Try frm->WETH and WETH->toToken, multiply
            bt = mdp._weth_address()
            p1 = mdp._mid_price_from_reserves(frm, bt)  # type: ignore[attr-defined]
            p2 = mdp._mid_price_from_reserves(bt, to_token)  # type: ignore[attr-defined]
            price = (p1 or 0.0) * (p2 or 0.0)
        if not price or price <= 0:
            raise RuntimeError("no route/mid-price available")
        # amountIn ~= amountOut / price, normalized to input decimals
        di = mdp._erc20_decimals(frm)  # type: ignore[attr-defined]
        do = mdp._erc20_decimals(to_token)  # type: ignore[attr-defined]
        amt_out_float = float(amt_out) / float(10 ** int(do))
        amt_in_float = float(amt_out_float / float(price))
        amt_in_units_norm = int(round(amt_in_float * float(10 ** int(di))))
        amt_in_units = int(amt_in_units_norm)

        # Calculate slippage for fallback path
        slip_bps_val: int | None = None
        pool_take_bps_val: int | None = None

        from services.marketdata.etherscan_verify import get_cached_pair_info_for_tokens
        first_hop = [frm, to_token]
        cached = None
        try:
            cached = get_cached_pair_info_for_tokens(first_hop[0], first_hop[1])
        except Exception:
            cached = None
        input_reserve = None
        output_reserve = None
        if cached and cached.get("reserves"):
            reserves = cached["reserves"]
            tok0 = str(cached.get("token0") or "")
            tok1 = str(cached.get("token1") or "")
            if tok0 and tok1:
                if tok0.lower() == first_hop[0].lower():
                    input_reserve = float(reserves[0])
                    output_reserve = float(reserves[1])
                elif tok1.lower() == first_hop[0].lower():
                    input_reserve = float(reserves[1])
                    output_reserve = float(reserves[0])
        if input_reserve and output_reserve and output_reserve > 0:
            amt_in_units_raw = int(math.ceil((float(amt_out) * float(input_reserve)) / float(output_reserve)))
            if amt_in_units_raw > 0:
                amt_in_units = amt_in_units_raw
            pool_take_bps_val = int(round((float(amt_in_units) / float(input_reserve)) * 10000))
        exec_price_norm = None
        if amt_in_float > 0:
            exec_price_norm = amt_out_float / amt_in_float
        slip_candidates: list[dict] = []
        if exec_price_norm is not None and price > 0:
            slip_candidates.append(policy.check_slippage(exec_price_norm, price))
        if input_reserve and output_reserve and float(amt_in_units) > 0:
            exec_price_raw = float(amt_out) / float(amt_in_units)
            ref_price_raw = float(output_reserve) / float(input_reserve) if float(input_reserve) > 0 else None
            if ref_price_raw and ref_price_raw > 0:
                slip_candidates.append(policy.check_slippage(exec_price_raw, ref_price_raw))
        if slip_candidates:
            worst = max(slip_candidates, key=lambda r: float(r.get("slippage_bps", 0.0) or 0.0))
            slip_bps_val = int(round(float(worst.get("slippage_bps", 0.0) or 0.0)))
            max_slippage_bps = int(policy.slippage_bps_cap)
            if slip_bps_val > max_slippage_bps or not bool(worst.get("ok", True)):
                raise HTTPException(status_code=409, detail=f"estimated slippage {slip_bps_val} bps exceeds cap of {max_slippage_bps} bps")

        try:
            cap_units = mdp.reserve_cap_units(first_hop)
        except Exception:
            cap_units = None
        if cap_units is not None and int(amt_in_units) > int(cap_units):
            import os
            max_pool_take_bps = int(os.getenv("RISK_MAX_POOL_TAKE_BPS", "100"))
            if pool_take_bps_val is not None:
                raise HTTPException(status_code=409, detail=f"input exceeds pool take cap: {pool_take_bps_val} bps > {max_pool_take_bps} bps allowed")
            raise HTTPException(status_code=409, detail="input exceeds pool take cap")

        return {
            "provider": None,
            "fromToken": frm,
            "toToken": to_token,
            "toAsset": asset_u,
            "path": [frm, to_token],
            "amountIn": int(amt_in_units),
            "amountOut": int(amt_out),
            "expiresAt": int(time.time()) + 60,
            "approx": True,
            "slippageBps": slip_bps_val,
            "poolTakeBps": pool_take_bps_val,
        }
    except HTTPException:
        raise
    except Exception as _e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"no route: {_e}")


__all__ = ["router", "init_router"]

