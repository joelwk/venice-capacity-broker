from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
        # On-chain actions via AgentKit-compatible helpers
        # Lazily resolve DIEMACTIONS at call time to avoid importing web3 in tests
        self._actions = None  # type: ignore[assignment]
        self._actions_factory = lambda: getattr(import_module("libs.agentkit_ext.actions"), "DIEMACTIONS")()
        # Simple in-memory state tracking for observability/testing
        self._last_mint: Optional[Dict[str, Any]] = None
        self._last_burn: Optional[Dict[str, Any]] = None
        self._totals = {"minted": 0, "burned": 0}

    def _get_actions(self):  # lazy, to avoid web3 dependency during tests
        if self._actions is None:
            self._actions = self._actions_factory()
        return self._actions

    # --- optional capacity gating (sVVV locking rules) ---
    def _env_flag(self, name: str, default: bool = False) -> bool:
        v = os.getenv(name)
        if v is None:
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "on"}

    def _decimals_pair(self) -> Tuple[int, int]:
        """Return (diem_decimals, svvv_decimals) with env overrides, defaulting to 18."""
        try:
            d = int(os.getenv("DIEM_DECIMALS") or 18)
        except Exception:
            d = 18
        try:
            s = int(os.getenv("SVVV_DECIMALS") or os.getenv("VVV_DECIMALS") or 18)
        except Exception:
            s = 18
        return int(d), int(s)

    def _svvv_available_units(self) -> Optional[int]:
        """Best-effort available sVVV units for locking.

        Priority:
        - DIEM_SVVV_AVAILABLE_UNITS (explicit override, base units)
        - StakingService.status().get("staked") (treat entire staked as available if no lock info)
        - None if unavailable
        """
        env_override = os.getenv("DIEM_SVVV_AVAILABLE_UNITS")
        if env_override is not None and str(env_override).strip() != "":
            try:
                return int(env_override)
            except Exception:
                return None
        # Try staking status
        try:
            from services.staking.client import StakingService  # lazy import
            from libs.agentkit_ext.actions import VVVActions  # type: ignore

            svc = StakingService(VVVActions())
            st = svc.status() or {}
            staked = int(st.get("staked") or 0)
            # We do not know the currently locked portion; conservatively return staked as available
            return staked
        except Exception:
            return None

    def _mint_rate_svvv_per_diem_units(self) -> Optional[int]:
        """Return mint rate as sVVV base units required per 1 DIEM base unit.

        Sources (in order):
        - DIEM_MINT_RATE_SVVV_PER_DIEM (integer ratio in base units)
        - DIEM_MINT_RATE (float svvv_per_diem in token units) scaled by decimals
        - None if not configured
        """
        # Exact base-units ratio if provided
        v = os.getenv("DIEM_MINT_RATE_SVVV_PER_DIEM")
        if v is not None and str(v).strip() != "":
            try:
                return int(v)
            except Exception:
                pass
        # Float tokens-per-token rate
        v2 = os.getenv("DIEM_MINT_RATE")
        if v2 is not None and str(v2).strip() != "":
            try:
                rate_tokens = float(v2)
                d_dec, s_dec = self._decimals_pair()
                # Convert tokens->base-units ratio: (rate_tokens * 10^s) / (10^d)
                # i.e., svvv_units_per_diem_unit
                ratio = rate_tokens * (10 ** s_dec) / float(10 ** d_dec)
                return int(ratio)
            except Exception:
                return None
        return None

    def _check_capacity_for_mint(self, amount: int) -> Dict[str, Any]:
        """Optional pre-check for sVVV capacity before mint.

        Enabled by DIEM_ENABLE_SVVV_GATE. Returns a dict with check details.
        """
        enabled = self._env_flag("DIEM_ENABLE_SVVV_GATE", default=False)
        if not enabled:
            return {"enabled": False}
        rate = self._mint_rate_svvv_per_diem_units()
        avail = self._svvv_available_units()
        if rate is None or avail is None:
            return {"enabled": True, "ok": True, "reason": "insufficient_data"}
        required = int(rate) * int(amount)
        ok = required <= int(avail)
        return {
            "enabled": True,
            "ok": bool(ok),
            "required_svvv": int(required),
            "available_svvv": int(avail),
            "mint_rate_svvv_per_diem": int(rate),
        }

    def mint(
        self,
        amount: int,
        *,
        dry_run: bool = False,
        idem_key: Optional[str] = None,
        corr_id: Optional[str] = None,
    ) -> Dict[str, Any]:
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
        # Optional capacity gate (sVVV locking rules)
        gate = self._check_capacity_for_mint(amount)
        if gate.get("enabled") and (gate.get("ok") is False):
            out = {"status": "denied", "action": "mint", "reason": "insufficient_capacity", **gate}
            try:
                if corr_id:
                    out["correlationId"] = str(corr_id)
                _emit_event("diem.mint.denied", dict(out))
            except Exception:
                pass
            self._last_mint = dict(out)
            return out
        try:
            res = self._get_actions().mint(amount)
        except Exception as e:  # noqa: BLE001
            err = {"status": "error", "action": "mint", "error": str(e)}
            try:
                payload = {"amount": int(amount), **dict(err)}
                if corr_id:
                    payload["correlationId"] = str(corr_id)
                _emit_event("diem.mint.error", payload)
            except Exception:
                pass
            self._last_mint = dict(err)
            return err
        try:
            payload = {"amount": int(amount), **dict(res)}
            if corr_id:
                payload["correlationId"] = str(corr_id)
            if gate.get("enabled"):
                payload["capacity_gate"] = dict(gate)
            _emit_event("diem.mint", payload)
        except Exception:
            pass
        # Track state
        try:
            self._totals["minted"] = int(self._totals.get("minted", 0)) + int(amount)
        except Exception:
            pass
        self._last_mint = dict({"amount": int(amount)}, **dict(res))
        return res

    def burn(
        self,
        amount: int,
        *,
        dry_run: bool = False,
        idem_key: Optional[str] = None,
        corr_id: Optional[str] = None,
    ) -> Dict[str, Any]:
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
        try:
            res = self._get_actions().burn(amount)
        except Exception as e:  # noqa: BLE001
            err = {"status": "error", "action": "burn", "error": str(e)}
            try:
                payload = {"amount": int(amount), **dict(err)}
                if corr_id:
                    payload["correlationId"] = str(corr_id)
                _emit_event("diem.burn.error", payload)
            except Exception:
                pass
            self._last_burn = dict(err)
            return err
        try:
            payload = {"amount": int(amount), **dict(res)}
            if corr_id:
                payload["correlationId"] = str(corr_id)
            _emit_event("diem.burn", payload)
        except Exception:
            pass
        # Track state
        try:
            self._totals["burned"] = int(self._totals.get("burned", 0)) + int(amount)
        except Exception:
            pass
        self._last_burn = dict({"amount": int(amount)}, **dict(res))
        return res

    def _path_from_env(self) -> List[str]:
        import os

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise EnvironmentError("TRADE_PATH must be set: comma-separated token addresses (in,out)")
        return [p.strip() for p in path_env.split(",")]

    def trade(self, side: str, amount: int, *, corr_id: Optional[str] = None) -> Dict[str, Any]:
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
                res = self._get_actions().trade("sell", amount)
            out = {"status": "sent", **res}
            try:
                payload = {"side": side_l, "amount_in": int(amount), **dict(out)}
                if corr_id:
                    payload["correlationId"] = str(corr_id)
                _emit_event("diem.trade", payload)
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
                        payload = {"side": side_l, "amount_out": int(amount), **dict(out)}
                        if corr_id:
                            payload["correlationId"] = str(corr_id)
                        _emit_event("diem.trade", payload)
                    except Exception:
                        pass
                    return out
                except Exception:
                    pass
            # Fallback path
            act = self._get_actions()
            res = act.trade("buy", amount)
            out = {"status": "sent", **res}
            try:
                payload = {"side": side_l, "amount_out": int(amount), **dict(out)}
                if corr_id:
                    payload["correlationId"] = str(corr_id)
                _emit_event("diem.trade", payload)
            except Exception:
                pass
            return out
        raise ValueError("side must be 'buy' or 'sell'")

    # --- state accessors ---
    def last_results(self) -> Dict[str, Any]:
        return {"mint": self._last_mint, "burn": self._last_burn}

    def totals(self) -> Dict[str, int]:
        return {"minted": int(self._totals.get("minted", 0)), "burned": int(self._totals.get("burned", 0))}

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
