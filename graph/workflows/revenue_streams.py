from __future__ import annotations

from dataclasses import dataclass

from agents.arbi_diem.agent import ArbiDiem
from libs.pricing.diem import fair_value_per_diem
from libs.telemetry.logger import get_logger
from services.marketdata.provider import MarketDataProvider

logger = get_logger("workflow.revenue")


@dataclass
class DiemMintSellWorkflow:
    market: MarketDataProvider
    arbi: ArbiDiem
    discount_rate_apy: float = 0.2

    def run_once(self, mint_rate: float = 1.0, dry_run: bool = True) -> bool:
        px = self.market.prices(["DIEM"]).get("DIEM", 1.0)
        effective_mint_rate = float(mint_rate)
        try:
            info = self.market.diem_mint_rate(ttl_s=60)
            if isinstance(info, dict):
                candidate = info.get("tokens_per_diem")
                if candidate not in (None, 0):
                    effective_mint_rate = float(candidate)  # type: ignore[arg-type]
        except Exception:
            pass
        try:
            vvv_price = float(self.market.prices(["VVV"]).get("VVV", 1.0))
        except Exception:
            vvv_price = 1.0
        # Determine if on-chain liquidity exists
        has_onchain_liquidity = True
        try:
            price_health = self.market.price_health("DIEM")
            source = price_health.get("source", "")
            if source in ("bridge_vvv", "external_reference"):
                has_onchain_liquidity = False
        except Exception:
            pass

        fair_data = fair_value_per_diem(
            vvv_price=vvv_price,
            mint_rate=effective_mint_rate,
            emissions_penalty=0.20,
            utilization_current=None,
            utilization_trend=None,
            circulating_supply=None,
            target_supply=self.arbi._target_supply(),
            discount_rate_apy=0.15,
            growth_rate_apy=0.05,
            historical_ratio=None,
            has_onchain_liquidity=has_onchain_liquidity,
            market_price=px,
        )
        if isinstance(fair_data, dict):
            fair_value = float(fair_data.get("fair_value", 0.0))
        else:
            fair_value = float(fair_data)
        decision = "mint_sell" if px > fair_value * 1.05 else "hold"
        logger.info(
            "Decision=%s (market=%.4f, fair=%.4f, mint_rate=%.4f, vvv=%.4f)",
            decision,
            px,
            fair_value,
            effective_mint_rate,
            vvv_price,
        )
        if decision == "mint_sell" and not dry_run:
            return self.arbi.evaluate_and_maybe_mint(px, effective_mint_rate)
        return decision == "mint_sell"
