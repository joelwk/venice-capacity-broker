from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InventorySnapshot:
    """Lightweight wrapper around treasury portfolio snapshot.

    Captures balances once so ArbiDiem can make consistent sizing and
    execution choices without re-querying wallet state mid-decision.
    """

    raw: dict[str, Any]

    @classmethod
    def capture(cls, include_eth: bool = False) -> InventorySnapshot:
        try:
            from services.wallet.provider import describe_treasury_portfolio

            snapshot = describe_treasury_portfolio(include_eth=include_eth) or {}
        except Exception:
            snapshot = {}
        return cls(raw=snapshot)

    def balance(self, symbol: str, default_decimals: int = 18) -> tuple[int, int]:
        info = (self.raw or {}).get("balances", {}).get(symbol) or {}
        units = int(info.get("units", 0) or 0)
        decimals = int(info.get("decimals", default_decimals) or default_decimals)
        return units, decimals

    @property
    def balances(self) -> dict[str, dict[str, int]]:
        compact: dict[str, dict[str, int]] = {}
        bal = (self.raw or {}).get("balances", {}) or {}
        for sym in ("DIEM", "USDC", "SVVV"):
            info = bal.get(sym) or {}
            units = int(info.get("units", 0) or 0)
            decimals = int(info.get("decimals", 18) or 18)
            compact[sym] = {"units": units, "decimals": decimals}
        return compact

    @property
    def has_data(self) -> bool:
        return bool(self.raw)
