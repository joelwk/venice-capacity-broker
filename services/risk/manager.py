from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskManager:
    max_inventory_usd: float = 100_000.0
    max_single_trade_usd: float = 10_000.0

    def check_inventory(self, inventory_usd: float) -> dict[str, bool | float]:
        return {
            "ok": inventory_usd <= self.max_inventory_usd,
            "inventory": inventory_usd,
        }

    def check_trade(self, trade_usd: float) -> dict[str, bool | float]:
        return {"ok": trade_usd <= self.max_single_trade_usd, "trade": trade_usd}
