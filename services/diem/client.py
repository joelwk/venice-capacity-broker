from __future__ import annotations

import json
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
        self._last_stake: Optional[Dict[str, Any]] = None
        self._totals = {"minted": 0, "burned": 0}
        # local lock schedule (best-effort metadata only; on-chain source of truth prevails)
        self._lock_log: List[Dict[str, Any]] = []

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
            if staked <= 0:
                return None
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
        # Fall back to market data mint rate if available
        try:
            from services.marketdata.provider import MarketDataProvider  # lazy import

            info = MarketDataProvider().diem_mint_rate(ttl_s=120)
            if isinstance(info, dict):
                units = info.get("svvv_units_per_diem")
                if units not in (None, 0):
                    return int(units)  # type: ignore[arg-type]
                tokens = info.get("tokens_per_diem")
                if tokens not in (None, 0):
                    rate_tokens = float(tokens)  # type: ignore[arg-type]
                    d_dec, s_dec = self._decimals_pair()
                    ratio = rate_tokens * (10 ** s_dec) / float(10 ** d_dec)
                    return int(ratio)
        except Exception:
            pass
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

    def _maybe_lock_before_mint(self, amount: int, gate: Dict[str, Any], corr_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Attempt to lock sVVV before mint if enabled via env.

        Env:
          - DIEM_LOCK_ON_MINT=true|1
          - DIEM_UNLOCK_COOLDOWN_SECONDS (optional; metadata only)
        """
        if not self._env_flag("DIEM_LOCK_ON_MINT", default=False):
            return None
        try:
            required = gate.get("required_svvv")
            if required is None:
                rate = self._mint_rate_svvv_per_diem_units()
                if rate is None:
                    return None
                required = int(rate) * int(amount)
            act = self._get_actions()
            if not hasattr(act, "lock_svvv"):
                return None
            res = act.lock_svvv(int(required))  # type: ignore[attr-defined]
            # annotate with cooldown metadata if present
            try:
                cd = int(os.getenv("DIEM_UNLOCK_COOLDOWN_SECONDS") or 0)
            except Exception:
                cd = 0
            payload = {"amount_svvv": int(required), **dict(res)}
            if cd > 0:
                import time as _t

                payload["unlock_cooldown_s"] = cd
                payload["unlock_earliest_at"] = int(_t.time()) + cd
            if corr_id:
                payload["correlationId"] = str(corr_id)
            try:
                _emit_event("diem.lock", dict(payload))
            except Exception:
                pass
            self._lock_log.append(payload)
            return payload
        except Exception as e:  # noqa: BLE001
            err = {"status": "error", "action": "lock_svvv", "error": str(e)}
            try:
                payload = {"amount_svvv": None, **dict(err)}
                if corr_id:
                    payload["correlationId"] = str(corr_id)
                _emit_event("diem.lock.error", payload)
            except Exception:
                pass
            return err

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
        # Optional lock step
        lock_info: Optional[Dict[str, Any]] = None
        try:
            lock_info = self._maybe_lock_before_mint(amount, gate, corr_id)
        except Exception:
            pass
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
            if lock_info is not None:
                payload["lock"] = dict(lock_info)
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
        # Optional unlock step post-burn
        unlock_payload: Optional[Dict[str, Any]] = None
        try:
            if self._env_flag("DIEM_UNLOCK_AFTER_BURN", default=False):
                rate = self._mint_rate_svvv_per_diem_units()
                if rate is not None and hasattr(self._get_actions(), "unlock_svvv"):
                    required = int(rate) * int(amount)
                    try:
                        unlock_payload = self._get_actions().unlock_svvv(int(required))  # type: ignore[attr-defined]
                        try:
                            out2 = {"amount_svvv": int(required), **dict(unlock_payload)}
                            if corr_id:
                                out2["correlationId"] = str(corr_id)
                            _emit_event("diem.unlock", out2)
                        except Exception:
                            pass
                    except Exception as e:  # noqa: BLE001
                        try:
                            out2 = {"status": "error", "action": "unlock_svvv", "error": str(e), "amount_svvv": int(required)}
                            if corr_id:
                                out2["correlationId"] = str(corr_id)
                            _emit_event("diem.unlock.error", out2)
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            payload = {"amount": int(amount), **dict(res)}
            if corr_id:
                payload["correlationId"] = str(corr_id)
            if unlock_payload is not None:
                payload["unlock"] = dict(unlock_payload)
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

    def stake_for_api(
        self,
        amount: int,
        *,
        dry_run: bool = False,
        idem_key: Optional[str] = None,
        corr_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stake DIEM to realize daily API credits (\$1/day per token)."""

        if dry_run:
            return {"status": "dry_run", "action": "stake_diem", "amount": int(amount)}
        if idem_key:
            _idem_attr = getattr(self, "_idem", None)
            if _idem_attr is None:
                setattr(self, "_idem", set())
                _idem_attr = getattr(self, "_idem")
            if idem_key in _idem_attr:
                return {"status": "skipped", "action": "stake_diem", "idempotent": True}
            _idem_attr.add(idem_key)
        try:
            act = self._get_actions()
            if not hasattr(act, "stake_for_api"):
                raise NotImplementedError("stake_for_api not implemented in DIEMACTIONS")
            res = act.stake_for_api(int(amount))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            err = {"status": "error", "action": "stake_diem", "error": str(e)}
            try:
                payload = {"amount": int(amount), **dict(err)}
                if corr_id:
                    payload["correlationId"] = str(corr_id)
                _emit_event("diem.stake.error", payload)
            except Exception:
                pass
            self._last_stake = dict(err)
            return err
        try:
            payload = {"amount": int(amount), **dict(res)}
            if corr_id:
                payload["correlationId"] = str(corr_id)
            _emit_event("diem.stake", payload)
        except Exception:
            pass
        try:
            self._totals["staked"] = int(self._totals.get("staked", 0)) + int(amount)
        except Exception:
            pass
        self._last_stake = dict({"amount": int(amount)}, **dict(res))
        return res

    def _path_from_env(self) -> List[str]:
        raw_paths = os.getenv("TRADE_PATHS")
        if raw_paths:
            try:
                import json
                from services.marketdata.provider import MarketDataProvider

                parsed = json.loads(raw_paths)
                if isinstance(parsed, list) and parsed:
                    spec = MarketDataProvider._coerce_route_entry(parsed[0])
                    if spec:
                        route = MarketDataProvider._parse_route_spec(spec)
                        return [str(token) for token in route.tokens]
            except Exception:
                pass

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise EnvironmentError("TRADE_PATH must be set: comma-separated token addresses (in,out)")
        return [p.strip() for p in path_env.split(",")]

    def trade(self, side: str, amount: int, *, corr_id: Optional[str] = None) -> Dict[str, Any]:
        side_l = side.lower()
        # Path may not be set in tests; allow empty path for fake aggregators
        try:
            base_path = self._path_from_env()
        except Exception:
            base_path = []
        slippage_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
        if side_l == "sell":
            if self.aggregator is not None:
                res = self.aggregator.trade_best(amount, slippage_bps, base_path)
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
            # For buy, reverse the TRADE_PATH so output is DIEM at the end.
            path_buy = list(reversed(base_path)) if base_path else []
            # Prefer aggregator if supports exact-out; else fall back to AgentKit actions
            if (self.aggregator is not None) and hasattr(self.aggregator, "trade_best_exact_out"):
                try:
                    res = self.aggregator.trade_best_exact_out(amount, slippage_bps, path_buy)  # type: ignore[attr-defined]
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
        return {"mint": self._last_mint, "burn": self._last_burn, "stake": self._last_stake}

    def calc_mint_rate(self, ttl_s: int = 120) -> Dict[str, Any]:
        """Return a summary of the current DIEM mint rate (sVVV per DIEM)."""

        from services.marketdata.provider import MarketDataProvider

        try:
            info = MarketDataProvider().diem_mint_rate(ttl_s=ttl_s)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}

        tokens = info.get("tokens_per_diem") if isinstance(info, dict) else None
        status = "ok" if tokens not in (None, 0) else "unknown"
        return {"status": status, **(info if isinstance(info, dict) else {})}

    def totals(self) -> Dict[str, int]:
        return {
            "minted": int(self._totals.get("minted", 0)),
            "burned": int(self._totals.get("burned", 0)),
            "staked": int(self._totals.get("staked", 0)),
        }

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
