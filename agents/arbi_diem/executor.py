from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.arbi_diem.decider import InventorySnapshot


@dataclass
class ExecutionContext:
    """Execution-scoped context shared across decision and execution layers."""

    inventory: InventorySnapshot | None = None

    @property
    def snapshot(self) -> dict[str, Any] | None:
        return self.inventory.raw if self.inventory else None

    @property
    def balances(self) -> dict[str, dict[str, int]]:
        if self.inventory is None:
            return {}
        return self.inventory.balances

    def balance_units(self, symbol: str) -> int:
        return int((self.balances.get(symbol) or {}).get("units", 0) or 0)
