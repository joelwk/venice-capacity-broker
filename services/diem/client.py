from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from libs.dex.providers import DexAggregator, build_aggregator_from_env


@dataclass
class DIEMService:
    aggregator: DexAggregator

    def __init__(self, aggregator: Optional[DexAggregator] = None) -> None:
        self.aggregator = aggregator or build_aggregator_from_env()

    def mint(self, amount: int) -> Dict[str, Any]:
        # Minting is protocol-specific; keep stub to maintain interface
        return {"status": "stub", "action": "mint", "amount": amount}

    def burn(self, amount: int) -> Dict[str, Any]:
        return {"status": "stub", "action": "burn", "amount": amount}

    def _path_from_env(self) -> List[str]:
        import os

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise EnvironmentError("TRADE_PATH must be set: comma-separated token addresses (in,out)")
        return [p.strip() for p in path_env.split(",")]

    def trade(self, side: str, amount: int) -> Dict[str, Any]:
        if side.lower() != "sell":
            raise NotImplementedError("Only 'sell' trades are implemented")
        path = self._path_from_env()
        slippage_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
        res = self.aggregator.trade_best(amount, slippage_bps, path)
        return {"status": "sent", **res}

    def quote(self, side: str, amount: int) -> Dict[str, Any]:
        path = self._path_from_env()
        quotes = self.aggregator.quote_all(amount, path)
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [q.__dict__ for q in quotes],
        }
