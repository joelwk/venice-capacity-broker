from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from libs.telemetry.logger import get_logger
from importlib import import_module
from services.diem.client import DIEMService
from services.risk.policy import RiskPolicy
try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # noqa: BLE001
    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return


logger = get_logger("agent.arbi_diem")


@dataclass
class ArbiDiem:
    diem: DIEMService
    discount_rate_apy: float = 0.2
    risk: RiskPolicy = field(default_factory=RiskPolicy.from_env)
    market: object | None = None
    _market_cached: object | None = field(default=None, init=False, repr=False)

    def _market_provider(self) -> Any:
        if self.market is not None:
            return self.market
        if self._market_cached is None:
            from services.marketdata.provider import MarketDataProvider  # lazy import

            self._market_cached = MarketDataProvider()
        return self._market_cached

    def _trade_routes(self):
        try:
            routes = self.diem.trade_routes()
            if routes:
                return routes
        except Exception:
            pass
        try:
            tokens = self.diem._path_from_env()
            if tokens:
                from libs.dex.routes import make_route

                return [make_route(tokens)]
        except Exception:
            return []
        return []

    def _desired_units(self) -> int:
        try:
            return int((__import__("os").getenv("ARBI_DIEM_MINT_UNITS") or "1000").strip() or 1000)
        except Exception:
            return 1000

    def _decimals_out(self) -> int:
        try:
            import os
            from libs.agentkit_ext.web3_utils import get_contract, get_web3
            from web3 import Web3  # type: ignore

            routes = self._trade_routes()
            path = routes[0].tokens if routes else []
            w3 = get_web3()
            erc20 = get_contract(w3, Web3.to_checksum_address(path[-1]), "erc20.json")
            return int(erc20.functions.decimals().call())
        except Exception:
            # Default to USDC 6
            return 6

    def _preview_exec_price(self, units_in: int) -> float:
        """Quote execution price (USD per DIEM) for the given input units.

        Uses aggregator.best_quote; falls back to None (0.0) when unavailable.
        """
        if self.diem.aggregator is None:
            return 0.0
        routes = self._trade_routes()
        if not routes:
            return 0.0
        for route in routes:
            try:
                quote = self.diem.aggregator.best_quote(units_in, route)
            except Exception:
                continue
            if quote is None:
                continue
            try:
                dec_in = self.risk._diem_decimals()
                dec_out = self._decimals_out()
                amt_in = quote.amount_in / float(10 ** dec_in)
                amt_out = quote.amount_out / float(10 ** dec_out)
                if amt_in <= 0:
                    continue
                return float(amt_out / amt_in)
            except Exception:
                continue
        return 0.0

    def _erc20_decimals(self, address: str) -> int:
        try:
            from libs.agentkit_ext.web3_utils import get_contract, get_web3
            from web3 import Web3  # type: ignore

            w3 = get_web3()
            erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
            return int(erc20.functions.decimals().call())
        except Exception:
            return 18

    def _preview_exec_price_buy(self, units_out: int) -> float:
        """Quote execution price (USD per DIEM) for exact-out buy of DIEM units.

        Uses aggregator.best_quote_exact_out on the reversed TRADE_PATH (QUOTE->...->DIEM).
        Returns 0.0 when unavailable.
        """
        if self.diem.aggregator is None or not hasattr(self.diem.aggregator, "best_quote_exact_out"):
            return 0.0
        routes = self._trade_routes()
        if not routes:
            return 0.0
        for route in routes:
            try:
                rev_route = route.reversed()
                quote = self.diem.aggregator.best_quote_exact_out(units_out, rev_route)  # type: ignore[attr-defined]
            except Exception:
                continue
            if quote is None:
                continue
            try:
                dec_in = self._erc20_decimals(rev_route.tokens[0])
                dec_out = self.risk._diem_decimals()
                amt_in = quote.amount_in / float(10 ** dec_in)
                amt_out = quote.amount_out / float(10 ** dec_out)
                if amt_out <= 0:
                    continue
                return float(amt_in / amt_out)
            except Exception:
                continue
        return 0.0

    def _slippage_bucket(self, bps: float) -> str:
        try:
            s = float(bps)
        except Exception:
            return "nan"
        if s < 25:
            return "lt_25bps"
        if s < 50:
            return "lt_50bps"
        if s < 100:
            return "lt_100bps"
        if s < 150:
            return "lt_150bps"
        if s < 300:
            return "lt_300bps"
        if s < 1000:
            return "lt_1000bps"
        return "ge_1000bps"

    def _adjust_for_liquidity(self, units_in: int, market_price: float) -> tuple[int, float | None]:
        """Conservatively reduce units until preview slippage is within cap.

        Returns (adjusted_units, last_slippage_bps or None if no preview).
        Does nothing if no aggregator preview is available.
        """
        # If no units or no price, bail fast
        if units_in <= 0 or market_price <= 0:
            return 0, None
        # Try preview once; if unavailable, keep original units
        exec_px = self._preview_exec_price(units_in)
        if exec_px <= 0:
            return units_in, None
        slip = self.risk.check_slippage(exec_px, market_price)
        bps = float(slip.get("slippage_bps", 0.0)) if isinstance(slip, dict) else 0.0
        if bool(slip.get("ok", False)):
            try:
                _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "false"})
                _metrics_inc("risk_liquidity_slippage_bucket_total", labels={"bucket": self._slippage_bucket(bps)})
            except Exception:
                pass
            return int(units_in), bps
        # If not ok, reduce progressively (binary-like) up to a few steps
        adjusted = int(units_in)
        last_bps = bps
        last_px = exec_px
        for _ in range(6):  # up to 6 halvings (~1.5% of original)
            adjusted = max(0, adjusted // 2)
            if adjusted <= 0:
                break
            px = self._preview_exec_price(adjusted)
            if px <= 0:
                # cannot preview smaller size; stop
                break
            slip2 = self.risk.check_slippage(px, market_price)
            bps2 = float(slip2.get("slippage_bps", 0.0)) if isinstance(slip2, dict) else float("inf")
            last_bps, last_px = bps2, px
            if bool(slip2.get("ok", False)):
                try:
                    _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "true"})
                    _metrics_inc("risk_liquidity_slippage_bucket_total", labels={"bucket": self._slippage_bucket(bps2)})
                except Exception:
                    pass
                return int(adjusted), bps2
            # If slippage does not improve materially, break to avoid infinite loop
            try:
                if abs(bps2 - bps) < 1e-6:
                    break
            except Exception:
                pass
        try:
            _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "true"})
            _metrics_inc("risk_liquidity_slippage_bucket_total", labels={"bucket": self._slippage_bucket(last_bps if last_bps is not None else float("inf"))})
        except Exception:
            pass
        return int(max(0, adjusted)), last_bps

    def _check_slippage_buy(self, exec_price: float, ref_price: float) -> dict:
        try:
            if ref_price <= 0 or exec_price <= 0:
                return {"ok": False, "slippage_bps": float("inf")}
            slip = max(0.0, (exec_price - ref_price) / ref_price * 10_000.0)
            return {"ok": slip <= float(self.risk.slippage_bps_cap), "slippage_bps": float(slip)}
        except Exception:
            return {"ok": False, "slippage_bps": float("inf")}

    def _adjust_for_liquidity_buy(self, units_out: int, market_price: float) -> tuple[int, float | None]:
        """Reduce DIEM units to buy until preview slippage is within cap using exact-out quotes.

        Returns (adjusted_units_out, last_slippage_bps or None if no preview).
        """
        if units_out <= 0 or market_price <= 0:
            return 0, None
        exec_px = self._preview_exec_price_buy(units_out)
        if exec_px <= 0:
            return units_out, None
        slip = self._check_slippage_buy(exec_px, market_price)
        bps = float(slip.get("slippage_bps", 0.0)) if isinstance(slip, dict) else 0.0
        if bool(slip.get("ok", False)):
            try:
                _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "false"})
                _metrics_inc("risk_liquidity_slippage_bucket_total", labels={"bucket": self._slippage_bucket(bps)})
            except Exception:
                pass
            return int(units_out), bps
        adjusted = int(units_out)
        last_bps = bps
        for _ in range(6):
            adjusted = max(0, adjusted // 2)
            if adjusted <= 0:
                break
            px = self._preview_exec_price_buy(adjusted)
            if px <= 0:
                break
            slip2 = self._check_slippage_buy(px, market_price)
            bps2 = float(slip2.get("slippage_bps", 0.0)) if isinstance(slip2, dict) else float("inf")
            last_bps = bps2
            if bool(slip2.get("ok", False)):
                try:
                    _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "true"})
                    _metrics_inc("risk_liquidity_slippage_bucket_total", labels={"bucket": self._slippage_bucket(bps2)})
                except Exception:
                    pass
                return int(adjusted), bps2
        try:
            _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "true"})
            _metrics_inc("risk_liquidity_slippage_bucket_total", labels={"bucket": self._slippage_bucket(last_bps if last_bps is not None else float("inf"))})
        except Exception:
            pass
        return int(max(0, adjusted)), last_bps

    def evaluate_and_maybe_mint(
        self,
        market_price: float,
        mint_rate: float = 1.0,
        desired_units: int | None = None,
        current_inventory_usd: float | None = None,
        utilization_ratio: float | None = None,
        vol_bps: float | None = None,
        corr_id: str | None = None,
        simulate: bool = False,
    ) -> bool:
        # Import lazily so tests can monkeypatch libs.pricing.diem
        fair_value_per_diem = import_module("libs.pricing.diem").fair_value_per_diem  # type: ignore[attr-defined]
        fair = fair_value_per_diem(self.discount_rate_apy) * mint_rate / 365.0
        logger.info(f"Market px={market_price:.4f}, fair/day={fair:.4f}")
        threshold_mult = 1.05
        # Initialize rationale holder
        rationale = {
            "market_price": float(market_price),
            "fair_per_day": float(fair),
            "threshold_mult": float(threshold_mult),
            "premium": (float(market_price / fair) if fair > 0 else None),
            "mint_rate": float(mint_rate),
            "desired_units": None,
            "suggested_units": None,
            "exec_price_preview": None,
            "slippage_bps": None,
            "slippage_ok": None,
            "decision": "hold",
            "reason": None,
        }
        if market_price > fair * threshold_mult:  # premium over threshold
            # Risk-gated sizing (utilization/vol-aware if available)
            want = int(desired_units) if desired_units is not None else self._desired_units()
            # Optional pool reserve cap (best-effort)
            reserve_cap: int | None = None
            pool_take_bps: int | None = None
            try:
                md = self._market_provider()
                routes = self._trade_routes()
                path = routes[0] if routes else None
                try:
                    pool_take_bps = int((__import__("os").getenv("RISK_MAX_POOL_TAKE_BPS") or "100").strip() or 100)
                except Exception:
                    pool_take_bps = 100
                reserve_cap = md.reserve_cap_units(path, take_bps=pool_take_bps) if path else None
            except Exception:
                reserve_cap = None
            try:
                suggested = self.risk.size_with_risk(
                    want,
                    market_price,
                    current_inventory_usd=current_inventory_usd,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                    reserve_cap_units=reserve_cap,
                )
            except Exception:
                suggested = self.risk.suggest_trade_units(want, market_price, current_inventory_usd)
            rationale.update({
                "desired_units": int(want),
                "suggested_units": int(suggested),
                "reserve_cap_units": (int(reserve_cap) if isinstance(reserve_cap, int) else None),
                "pool_take_bps": (int(pool_take_bps) if pool_take_bps is not None else None),
                "utilization_ratio": (float(utilization_ratio) if utilization_ratio is not None else None),
                "vol_bps": (float(vol_bps) if vol_bps is not None else None),
            })
            if suggested <= 0:
                logger.info("Risk rejected mint/trade (suggested=0)")
                rationale.update({"decision": "hold", "reason": "risk_rejected"})
                setattr(self, "_last_rationale", rationale)
                return False
            # Liquidity-aware sizing using aggregator preview; falls back to plain gate
            adjusted, last_bps = self._adjust_for_liquidity(suggested, market_price)
            rationale["exec_price_preview"] = float(self._preview_exec_price(adjusted)) if adjusted > 0 else None
            if last_bps is not None:
                rationale.update({
                    "slippage_bps": float(last_bps),
                    "slippage_ok": bool(last_bps <= float(self.risk.slippage_bps_cap)),
                })
            # If adjusted dropped to zero due to slippage, hold
            if adjusted <= 0:
                logger.info("Rejected due to liquidity/slippage after adjustment")
                rationale.update({"decision": "hold", "reason": "slippage_exceeded"})
                setattr(self, "_last_rationale", rationale)
                return False
            if adjusted != suggested:
                rationale.update({"liquidity_adjusted_units": int(adjusted)})
            suggested = adjusted
            logger.info(f"Signal: Mint and sell DIEM (units={suggested}, want={want}) simulate={simulate}")
            rationale.update({"decision": "mint_sell"})
            _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": "mint_sell"})
            if not simulate:
                self.diem.mint(suggested, corr_id=corr_id)
                self.diem.trade("sell", suggested, corr_id=corr_id)
            setattr(self, "_last_rationale", rationale)
            return True
        # Discount branch: consider buy and burn when price is sufficiently below fair
        if fair > 0 and market_price < (fair / threshold_mult):
            # Require exact-out support from aggregator to enable buy/burn in v1
            try:
                if (self.diem.aggregator is None) or (not hasattr(self.diem.aggregator, "trade_best_exact_out")):
                    logger.info("Buy/burn skipped: exact-out unsupported by aggregator")
                    rationale.update({"decision": "hold", "reason": "buy_unsupported"})
                    setattr(self, "_last_rationale", rationale)
                    return False
            except Exception:
                rationale.update({"decision": "hold", "reason": "buy_unsupported"})
                setattr(self, "_last_rationale", rationale)
                return False
            want = int(desired_units) if desired_units is not None else self._desired_units()
            # Reserve cap for reversed path (QUOTE->...->DIEM)
            reserve_cap: int | None = None
            pool_take_bps: int | None = None
            try:
                md = self._market_provider()
                routes = self._trade_routes()
                path_buy = routes[0].reversed() if routes else None
                try:
                    pool_take_bps = int((__import__("os").getenv("RISK_MAX_POOL_TAKE_BPS") or "100").strip() or 100)
                except Exception:
                    pool_take_bps = 100
                reserve_cap = md.reserve_cap_units(path_buy, take_bps=pool_take_bps) if path_buy else None
            except Exception:
                reserve_cap = None
            try:
                suggested = self.risk.size_with_risk(
                    want,
                    market_price,
                    current_inventory_usd=current_inventory_usd,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                    reserve_cap_units=reserve_cap,
                )
            except Exception:
                suggested = self.risk.suggest_trade_units(want, market_price, current_inventory_usd)
            rationale.update({
                "desired_units": int(want),
                "suggested_units": int(suggested),
                "reserve_cap_units": (int(reserve_cap) if isinstance(reserve_cap, int) else None),
                "pool_take_bps": (int(pool_take_bps) if pool_take_bps is not None else None),
                "utilization_ratio": (float(utilization_ratio) if utilization_ratio is not None else None),
                "vol_bps": (float(vol_bps) if vol_bps is not None else None),
            })
            if suggested <= 0:
                logger.info("Risk rejected buy/burn (suggested=0)")
                rationale.update({"decision": "hold", "reason": "risk_rejected"})
                setattr(self, "_last_rationale", rationale)
                return False
            adjusted, last_bps = self._adjust_for_liquidity_buy(suggested, market_price)
            if last_bps is None:
                # Cannot preview exact-out; avoid falling back to action-based buy in v1
                logger.info("Buy/burn skipped: no exact-out preview available")
                rationale.update({"decision": "hold", "reason": "no_exact_out_preview"})
                setattr(self, "_last_rationale", rationale)
                return False
            if last_bps is not None:
                rationale.update({
                    "slippage_bps": float(last_bps),
                    "slippage_ok": bool(last_bps <= float(self.risk.slippage_bps_cap)),
                })
            if adjusted <= 0:
                logger.info("Rejected buy due to liquidity/slippage after adjustment")
                rationale.update({"decision": "hold", "reason": "slippage_exceeded"})
                setattr(self, "_last_rationale", rationale)
                return False
            if adjusted != suggested:
                rationale.update({"liquidity_adjusted_units": int(adjusted)})
            suggested = adjusted
            logger.info(f"Signal: Buy and burn DIEM (units={suggested}, want={want}) simulate={simulate}")
            rationale.update({"decision": "buy_burn"})
            _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": "buy_burn"})
            if not simulate:
                self.diem.trade("buy", suggested, corr_id=corr_id)
                self.diem.burn(suggested, corr_id=corr_id)
            setattr(self, "_last_rationale", rationale)
            return True
        logger.info("No-op: market not favorable")
        _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": "hold"})
        rationale.update({"decision": "hold", "reason": "market_not_favorable"})
        setattr(self, "_last_rationale", rationale)
        return False

