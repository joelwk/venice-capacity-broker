from __future__ import annotations

from dataclasses import dataclass, field

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

            path = self.diem._path_from_env()
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
        try:
            path = self.diem._path_from_env()
            q = self.diem.aggregator.best_quote(units_in, path)
            if q is None:
                return 0.0
            dec_in = self.risk._diem_decimals()
            dec_out = self._decimals_out()
            amt_in = q.amount_in / float(10 ** dec_in)
            amt_out = q.amount_out / float(10 ** dec_out)
            if amt_in <= 0:
                return 0.0
            return float(amt_out / amt_in)
        except Exception:
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

    def evaluate_and_maybe_mint(
        self,
        market_price: float,
        mint_rate: float = 1.0,
        desired_units: int | None = None,
        current_inventory_usd: float | None = None,
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
            "desired_units": None,
            "suggested_units": None,
            "exec_price_preview": None,
            "slippage_bps": None,
            "slippage_ok": None,
            "decision": "hold",
            "reason": None,
        }
        if market_price > fair * threshold_mult:  # premium over threshold
            # Risk-gated sizing
            want = int(desired_units) if desired_units is not None else self._desired_units()
            suggested = self.risk.suggest_trade_units(want, market_price, current_inventory_usd)
            rationale.update({"desired_units": int(want), "suggested_units": int(suggested)})
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
        logger.info("No-op: market not favorable")
        _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": "hold"})
        rationale.update({"decision": "hold", "reason": "market_not_favorable"})
        setattr(self, "_last_rationale", rationale)
        return False

