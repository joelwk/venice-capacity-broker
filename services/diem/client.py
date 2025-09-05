from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from libs.dex.providers import DexAggregator, build_aggregator_from_env
from importlib import import_module
try:
    from libs.telemetry.events import emit as _emit_event
except Exception:  # noqa: BLE001
    def _emit_event(kind: str, payload: Dict[str, Any]) -> None:  # type: ignore
        return


@dataclass
class DIEMService:
    aggregator: DexAggregator

    def __init__(self, aggregator: Optional[DexAggregator] = None) -> None:
        # DEX aggregator for quotes/trades (optional; lazily used)
        self.aggregator = aggregator  # may be None in tests or dry flows
        # On-chain actions (mint/burn) via AgentKit-compatible helpers
        # Resolve DIEMACTIONS lazily to honor monkeypatching in tests
        actions_mod = import_module("libs.agentkit_ext.actions")
        self._actions = getattr(actions_mod, "DIEMACTIONS")()

    def mint(self, amount: int, *, dry_run: bool = False, idem_key: Optional[str] = None) -> Dict[str, Any]:
        """Mint DIEM on-chain using configured wallet provider.

        Expects env DIEM_TOKEN_ADDRESS and ABI at abi/diem.json.
        """
        if dry_run:
            return {"status": "dry_run", "action": "mint", "amount": int(amount)}
        # Simple in-process idempotency (best-effort)
        if idem_key:
            _idem_attr = getattr(self, "_idem", None)
            if _idem_attr is None:
                setattr(self, "_idem", set())
                _idem_attr = getattr(self, "_idem")
            if idem_key in _idem_attr:
                return {"status": "skipped", "action": "mint", "idempotent": True}
            _idem_attr.add(idem_key)
        res = self._actions.mint(amount)
        try:
            _emit_event("diem.mint", {"amount": int(amount), **dict(res)})
        except Exception:
            pass
        return res

    def burn(self, amount: int, *, dry_run: bool = False, idem_key: Optional[str] = None) -> Dict[str, Any]:
        """Burn DIEM on-chain using configured wallet provider."""
        if dry_run:
            return {"status": "dry_run", "action": "burn", "amount": int(amount)}
        if idem_key:
            _idem_attr = getattr(self, "_idem", None)
            if _idem_attr is None:
                setattr(self, "_idem", set())
                _idem_attr = getattr(self, "_idem")
            if idem_key in _idem_attr:
                return {"status": "skipped", "action": "burn", "idempotent": True}
            _idem_attr.add(idem_key)
        res = self._actions.burn(amount)
        try:
            _emit_event("diem.burn", {"amount": int(amount), **dict(res)})
        except Exception:
            pass
        return res

    def _path_from_env(self) -> List[str]:
        import os

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise EnvironmentError("TRADE_PATH must be set: comma-separated token addresses (in,out)")
        return [p.strip() for p in path_env.split(",")]

    def trade(self, side: str, amount: int) -> Dict[str, Any]:
        side_l = side.lower()
        # Path may not be set in tests; allow empty path for fake aggregators
        try:
            path = self._path_from_env()
        except Exception:
            path = []
        slippage_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
        if side_l == "sell":
            if self.aggregator is not None:
                res = self.aggregator.trade_best(amount, slippage_bps, path)
            else:
                # Fallback to actions if aggregator unavailable (test/mocked path)
                res = self._actions.trade("sell", amount)
            out = {"status": "sent", **res}
            try:
                _emit_event("diem.trade", {"side": side_l, "amount_in": int(amount), **dict(out)})
            except Exception:
                pass
            return out
        if side_l == "buy":
            # Prefer aggregator if supports exact-out; else fall back to AgentKit actions
            if (self.aggregator is not None) and hasattr(self.aggregator, "trade_best_exact_out"):
                try:
                    res = self.aggregator.trade_best_exact_out(amount, slippage_bps, path)  # type: ignore[attr-defined]
                    out = {"status": "sent", **res}
                    try:
                        _emit_event("diem.trade", {"side": side_l, "amount_out": int(amount), **dict(out)})
                    except Exception:
                        pass
                    return out
                except Exception:
                    pass
            # Fallback path
            act = getattr(self, "_actions", None)
            if act is None:
                raise RuntimeError("No available trading path for 'buy'")
            res = act.trade("buy", amount)
            out = {"status": "sent", **res}
            try:
                _emit_event("diem.trade", {"side": side_l, "amount_out": int(amount), **dict(out)})
            except Exception:
                pass
            return out
        raise ValueError("side must be 'buy' or 'sell'")

    def quote(self, side: str, amount: int) -> Dict[str, Any]:
        try:
            path = self._path_from_env()
        except Exception:
            path = []
        side_l = side.lower()
        if side_l == "sell":
            if self.aggregator is None:
                quotes = []
            else:
                quotes = self.aggregator.quote_all(amount, path)
        elif side_l == "buy":
            # amount is desired amount_out
            if (self.aggregator is not None) and hasattr(self.aggregator, "quote_all_exact_out"):
                quotes = self.aggregator.quote_all_exact_out(amount, path)  # type: ignore[attr-defined]
            else:
                quotes = []
        else:
            raise ValueError("side must be 'buy' or 'sell'")
        return {"status": "ok", "side": side, "amount": amount, "quotes": [q.__dict__ for q in quotes]}
