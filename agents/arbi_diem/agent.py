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

    def evaluate_and_maybe_mint(self, market_price: float, mint_rate: float = 1.0, desired_units: int | None = None) -> bool:
        # Import lazily so tests can monkeypatch libs.pricing.diem
        fair_value_per_diem = import_module("libs.pricing.diem").fair_value_per_diem  # type: ignore[attr-defined]
        fair = fair_value_per_diem(self.discount_rate_apy) * mint_rate / 365.0
        logger.info(f"Market px={market_price:.4f}, fair/day={fair:.4f}")
        if market_price > fair * 1.05:  # 5% threshold
            # Risk-gated sizing
            want = int(desired_units) if desired_units is not None else self._desired_units()
            suggested = self.risk.suggest_trade_units(want, market_price)
            if suggested <= 0:
                logger.info("Risk rejected mint/trade (suggested=0)")
                return False
            # Slippage gate using aggregator preview
            exec_px = self._preview_exec_price(suggested)
            if exec_px > 0:
                slip = self.risk.check_slippage(exec_px, market_price)
                if not bool(slip.get("ok", False)):
                    logger.info(
                        f"Rejected due to slippage_bps={slip.get('slippage_bps'):.2f} cap={self.risk.slippage_bps_cap}"
                    )
                    return False
            else:
                logger.info("No quotes available for preview; proceeding without slippage gate")
            logger.info(f"Signal: Mint and sell DIEM (units={suggested}, want={want})")
            _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": "mint_sell"})
            self.diem.mint(suggested)
            self.diem.trade("sell", suggested)
            return True
        logger.info("No-op: market not favorable")
        _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": "hold"})
        return False

