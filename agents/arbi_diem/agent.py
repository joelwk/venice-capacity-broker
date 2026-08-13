from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
import time
from typing import Any

from agents.arbi_diem.decider import InventorySnapshot
from agents.arbi_diem.executor import ExecutionContext
from libs.agentkit_ext.web3_utils import get_web3
from libs.dex.composite import attach_composite_metadata, get_composite_bridge_legs
from libs.dex.diem_fallbacks import check_diem_vvv_liquidity_threshold
from libs.dex.routes import RoutePlan
from libs.telemetry.logger import get_logger
from scripts.register_bridge_pools import (
    check_aerodrome_registration,
    check_uniswap_v3_registration,
    load_addresses,
)
from services.diem.client import DIEMService, _classify_route_health
from services.diem.execution import (
    ExecutionIntent,
    ExecutionStatus,
    TradeSide,
)
from services.risk.policy import RiskPolicy

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:

    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:
        return


try:
    from libs.telemetry.metrics import set_gauge as _metrics_set_gauge
except Exception:

    def _metrics_set_gauge(name: str, value: float, labels: dict | None = None) -> None:
        return


try:
    from libs.dex.diagnostics import log_event as _dex_diag_log_event  # type: ignore
except Exception:

    def _dex_diag_log_event(event: dict[str, Any]) -> None:  # type: ignore
        return


logger = get_logger("agent.arbi_diem")


_UNLOCK_FALLBACK_BLOCKED_REASONS = {
    "no_executable_quotes",
    "incoherent_preview_mute",
}

_UNLOCK_REVERT_BLOCKED_REASONS = {
    "execution_revert",
    "unlock_route_revert",
}


def _utilization_log_value(utilization_ratio: float | None) -> str:
    """Format utilization for logs without conflating missing data with real 0%."""

    if utilization_ratio is None:
        try:
            _metrics_inc("arbi_diem_utilization_missing_total")
        except Exception:
            pass
        return "n/a"
    try:
        return f"{float(utilization_ratio) * 100.0:.2f}%"
    except Exception:
        try:
            _metrics_inc("arbi_diem_utilization_missing_total")
        except Exception:
            pass
        return "n/a"


def _route_health_summary(diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate per-route success/failure counts for quick dashboards."""
    if not diagnostics:
        return []
    summary: dict[tuple[str, ...], dict[str, int]] = {}
    for entry in diagnostics:
        route_tokens = entry.get("route") or entry.get("route_tokens") or []
        if not route_tokens:
            continue
        key = tuple(str(t).lower() for t in route_tokens)
        bucket = summary.setdefault(key, {"success": 0, "fail": 0})
        status = str(entry.get("status", "")).lower()
        executable = bool(entry.get("executable", True))
        if status == "ok" and executable:
            bucket["success"] += 1
        else:
            bucket["fail"] += 1
    out: list[dict[str, Any]] = []
    for key, counts in summary.items():
        total = max(1, counts["success"] + counts["fail"])
        out.append(
            {
                "route": list(key),
                "success": counts["success"],
                "fail": counts["fail"],
                "success_rate": counts["success"] / float(total),
            }
        )
    return out


@dataclass
class _RecoveryPlan:
    steps_total: int
    steps_done: int
    target_bps: int
    cap_bps: int
    started_ts: float = field(default_factory=time.time)
    last_action_ts: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps_total": int(self.steps_total),
            "steps_done": int(self.steps_done),
            "target_bps": int(self.target_bps),
            "cap_bps": int(self.cap_bps),
            "started_ts": float(self.started_ts),
            "last_action_ts": float(self.last_action_ts)
            if self.last_action_ts is not None
            else None,
        }


@dataclass
class ArbiDiem:
    diem: DIEMService
    discount_rate_apy: float = 0.2
    risk: RiskPolicy = field(default_factory=RiskPolicy.from_env)
    market: object | None = None
    _market_cached: object | None = field(default=None, init=False, repr=False)
    _util_history: list[float] = field(default_factory=list, init=False, repr=False)
    _ratio_history: list[float] = field(default_factory=list, init=False, repr=False)
    _run_mode: str = field(default="unknown", init=False, repr=False)
    _last_run_mode: str | None = field(default=None, init=False, repr=False)
    _factory_registration_cache: bool | None = field(
        default=None, init=False, repr=False
    )
    _last_action_ts: float | None = field(default=None, init=False, repr=False)
    _mint_sell_action_ts: list[float] = field(
        default_factory=list, init=False, repr=False
    )
    _recovery_plan: _RecoveryPlan | None = field(default=None, init=False, repr=False)
    _slippage_hold_streak: int = field(default=0, init=False, repr=False)
    _capacity_recovery_pending_stake: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    _pending_recovery_action: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    _capacity_recovery_unlock_revert_streak: int = field(
        default=0, init=False, repr=False
    )
    _diem_buy_route_failures: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        # Track utilization and price ratios for trend-aware fair value inputs
        self._util_history = []
        self._ratio_history = []
        self._run_mode = "unknown"
        self._last_run_mode = None
        self._last_action_ts = None
        self._mint_sell_action_ts = []
        self._recovery_plan = None
        self._slippage_hold_streak = 0
        self._capacity_recovery_pending_stake = None
        self._pending_recovery_action = None
        self._capacity_recovery_unlock_revert_streak = 0
        self._diem_buy_route_failures = 0
        # Quote cache for reuse between simulate=True and simulate=False calls
        # Fields: quote object, timestamp, adjusted units, slippage bps
        self._cached_fallback_quote: dict | None = None
        self._cached_fallback_quote_ttl_seconds = float(
            os.getenv("DIEM_FALLBACK_QUOTE_CACHE_TTL_SECONDS", "30.0") or 30.0
        )

    def _slippage_override_enabled(self) -> bool:
        return os.getenv("DIEM_SLIPPAGE_OVERRIDE_ENABLE", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _update_slippage_hold_streak(self) -> None:
        last = getattr(self, "_last_rationale", None)
        if not isinstance(last, dict):
            self._slippage_hold_streak = 0
            return

        decision = str(last.get("decision") or "").strip().lower()
        reason = str(last.get("reason") or "").strip().lower()

        slippage_reasons = {
            "slippage_exceeded_policy",
            "slippage_exceeded",
            "extreme_slippage",
        }

        if decision == "hold" and reason in slippage_reasons:
            self._slippage_hold_streak = min(
                1_000_000, int(self._slippage_hold_streak) + 1
            )
            return
        self._slippage_hold_streak = 0

    def _adaptive_slippage_cap_bps(
        self,
        base_cap_bps: float,
        *,
        trade_usd: float | None,
        liquidity_depth_usd: float | None,
        route_health: str | None,
    ) -> tuple[float, dict[str, Any]]:
        """Optionally widen slippage cap based on trade size, liquidity, and hold streak.

        This only applies when DIEM_SLIPPAGE_OVERRIDE_ENABLE=1 so operators opt in.
        """
        meta: dict[str, Any] = {
            "enabled": False,
            "applied": False,
            "base_cap_bps": float(base_cap_bps)
            if math.isfinite(float(base_cap_bps))
            else None,
            "cap_bps": float(base_cap_bps)
            if math.isfinite(float(base_cap_bps))
            else None,
            "trade_usd": float(trade_usd) if trade_usd is not None else None,
            "liquidity_depth_usd": (
                float(liquidity_depth_usd) if liquidity_depth_usd is not None else None
            ),
            "route_health": str(route_health) if route_health is not None else None,
            "hold_streak": int(self._slippage_hold_streak),
        }

        if not self._slippage_override_enabled():
            return float(base_cap_bps), meta
        meta["enabled"] = True

        try:
            base = float(base_cap_bps)
        except Exception:
            return float(base_cap_bps), meta
        if not math.isfinite(base) or base <= 0:
            return float(base_cap_bps), meta

        bad_health = {"no_pool", "zero_liquidity", "revert"}
        if route_health is not None and str(route_health).strip().lower() in bad_health:
            meta["blocked_reason"] = "route_unhealthy"
            return float(base_cap_bps), meta

        # Operator-tunable knobs (defaults mirror the recommendation).
        try:
            small_trade_usd = float(
                os.getenv("ARBI_DIEM_ADAPTIVE_SLIPPAGE_SMALL_TRADE_USD", "10") or 10.0
            )
        except Exception:
            small_trade_usd = 10.0
        try:
            small_mult = float(
                os.getenv("ARBI_DIEM_ADAPTIVE_SLIPPAGE_SMALL_TRADE_MULT", "1.5") or 1.5
            )
        except Exception:
            small_mult = 1.5
        if not math.isfinite(small_mult) or small_mult <= 0:
            small_mult = 1.5

        step = 0.1
        try:
            step = float(os.getenv("ARBI_DIEM_SLIPPAGE_DECAY_STEP", "0.1") or 0.1)
        except Exception:
            step = 0.1
        if not math.isfinite(step) or step <= 0:
            step = 0.1

        max_cap = 500.0
        try:
            override_ceiling_raw = os.getenv("DIEM_SLIPPAGE_OVERRIDE_MAX_BPS")
            if override_ceiling_raw and str(override_ceiling_raw).strip() != "":
                override_ceiling = float(override_ceiling_raw)
                if math.isfinite(override_ceiling) and override_ceiling > 0:
                    max_cap = min(float(max_cap), float(override_ceiling))
        except Exception:
            pass

        cap = float(base)

        # Liquidity-aware small-trade boost (and optional depth ratio).
        ratio = None
        try:
            if (
                trade_usd is not None
                and liquidity_depth_usd is not None
                and float(trade_usd) > 0
                and float(liquidity_depth_usd) > 0
            ):
                ratio = float(trade_usd) / float(liquidity_depth_usd)
        except Exception:
            ratio = None
        if ratio is not None and math.isfinite(float(ratio)):
            meta["trade_to_depth_ratio"] = float(ratio)

        small_trade = False
        try:
            if trade_usd is not None and math.isfinite(float(trade_usd)):
                small_trade = float(trade_usd) < float(small_trade_usd)
        except Exception:
            small_trade = False
        if small_trade or (
            ratio is not None and math.isfinite(float(ratio)) and float(ratio) < 0.01
        ):
            cap = max(cap, min(float(base) * float(small_mult), float(max_cap)))
            meta["small_trade_boost"] = True
            meta["small_trade_mult"] = float(small_mult)

        # Slippage decay during hold streaks: widen after repeated slippage holds.
        if int(self._slippage_hold_streak) > 3:
            widened = float(base) * (
                1.0 + float(step) * float(int(self._slippage_hold_streak))
            )
            cap = max(cap, min(float(widened), float(max_cap)))
            meta["decay_hold_streak"] = int(self._slippage_hold_streak)
            meta["decay_step"] = float(step)

        cap = max(float(base), min(float(cap), float(max_cap), 10_000.0))
        meta["cap_bps"] = float(cap)
        meta["applied"] = not math.isclose(float(cap), float(base), rel_tol=1e-12)
        return float(cap), meta

    def _route_health(self, route: Any) -> str | None:
        agg = getattr(self.diem, "aggregator", None)
        if route is None or agg is None:
            return None
        diagnostics = getattr(agg, "_last_quote_diagnostics", None)
        if not isinstance(diagnostics, list):
            diagnostics = []
        route_tokens: list[str] = []
        try:
            route_tokens = list(route.tokens) if hasattr(route, "tokens") else []
        except Exception:
            route_tokens = []
        route_diagnostics: list[dict[str, Any]] = []
        if route_tokens and diagnostics:
            for entry in diagnostics:
                try:
                    diag_route = entry.get("route", [])
                    if (
                        isinstance(diag_route, list)
                        and len(diag_route) == len(route_tokens)
                        and all(
                            str(diag_route[i]).lower() == str(route_tokens[i]).lower()
                            for i in range(len(route_tokens))
                        )
                    ):
                        route_diagnostics.append(entry)
                except Exception:
                    continue
        try:
            return str(
                self.diem._classify_route_health(route, route_diagnostics or None)
            )
        except Exception:
            try:
                return str(
                    _classify_route_health(route, agg, route_diagnostics or None)
                )
            except Exception:
                return None

    def _min_action_interval_seconds(self) -> int:
        raw = os.getenv("ARBI_DIEM_MIN_ACTION_INTERVAL_SECONDS", "300")
        try:
            return max(0, int(float(str(raw).strip())))
        except Exception:
            return 300

    def _max_mint_sell_per_hour(self) -> int:
        raw = os.getenv("ARBI_DIEM_MAX_MINT_SELL_PER_HOUR", "12")
        try:
            return max(0, int(float(str(raw).strip())))
        except Exception:
            return 12

    def _recovery_bypass_interval(self) -> bool:
        raw = os.getenv("ARBI_DIEM_RECOVERY_BYPASS_INTERVAL", "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _recovery_preferred_action(self) -> str:
        raw = os.getenv("ARBI_DIEM_RECOVERY_PREFERRED_ACTION", "auto")
        val = str(raw or "auto").strip().lower()
        if val in {"stake", "burn", "auto"}:
            return val
        return "auto"

    def _recovery_max_slippage_bps(self) -> int:
        raw = os.getenv("ARBI_DIEM_RECOVERY_MAX_SLIPPAGE_BPS", "500")
        try:
            return max(0, min(10_000, int(float(str(raw).strip()))))
        except Exception:
            return 500

    def _recovery_small_trade_usd(self) -> float:
        raw = os.getenv("ARBI_DIEM_RECOVERY_SMALL_TRADE_USD", "5")
        try:
            return max(0.0, float(str(raw).strip()))
        except Exception:
            return 5.0

    def _recovery_price_sanity_max_rel_diff(self) -> float:
        raw = os.getenv("ARBI_DIEM_RECOVERY_PRICE_SANITY_MAX_REL_DIFF", "0.75")
        try:
            return max(0.0, float(str(raw).strip()))
        except Exception:
            return 0.75

    def _recovery_unlock_revert_fallback_threshold(self) -> int:
        """Consecutive revert failures required before forcing stake fallback."""

        raw = os.getenv("ARBI_DIEM_RECOVERY_UNLOCK_REVERT_FALLBACK_STREAK", "2").strip()
        if raw == "":
            return 2
        try:
            threshold = int(raw, 0)
        except Exception:
            return 2
        return max(0, threshold)

    def _pacing_snapshot(self, now_ts: float | None = None) -> dict[str, Any]:
        now = float(time.time() if now_ts is None else now_ts)

        interval_s = int(self._min_action_interval_seconds())
        max_mint_per_hour = int(self._max_mint_sell_per_hour())

        last_ts = self._last_action_ts
        since_last: float | None = None
        next_in: float | None = None
        if last_ts is not None:
            try:
                since_last = max(0.0, float(now) - float(last_ts))
            except Exception:
                since_last = None
        if since_last is not None and interval_s > 0:
            next_in = max(0.0, float(interval_s) - float(since_last))

        cutoff = float(now) - 3600.0
        recent_mints = [ts for ts in self._mint_sell_action_ts if float(ts) >= cutoff]
        self._mint_sell_action_ts = recent_mints
        mint_count = len(recent_mints)

        remaining = None
        if max_mint_per_hour > 0:
            remaining = max(0, int(max_mint_per_hour) - int(mint_count))

        return {
            "now_ts": float(now),
            "min_action_interval_seconds": int(interval_s),
            "last_action_ts": float(last_ts) if last_ts is not None else None,
            "seconds_since_last_action": (
                float(since_last) if since_last is not None else None
            ),
            "next_action_in_seconds": float(next_in) if next_in is not None else None,
            "max_mint_sell_per_hour": int(max_mint_per_hour),
            "mint_sell_actions_last_hour": int(mint_count),
            "mint_sell_actions_remaining": remaining,
        }

    def _pacing_check(
        self,
        *,
        action: str,
        now_ts: float | None = None,
        bypass_interval: bool = False,
    ) -> dict[str, Any]:
        snap = self._pacing_snapshot(now_ts=now_ts)
        interval_s = int(snap.get("min_action_interval_seconds") or 0)
        since_last = snap.get("seconds_since_last_action")

        if (
            not bypass_interval
            and interval_s > 0
            and since_last is not None
            and float(since_last) < float(interval_s)
        ):
            return {
                "ok": False,
                "reason": "min_action_interval",
                "action": str(action),
                "bypass_interval": bool(bypass_interval),
                "pacing": snap,
            }

        if action == "mint_sell":
            max_mint_per_hour = int(snap.get("max_mint_sell_per_hour") or 0)
            mint_count = int(snap.get("mint_sell_actions_last_hour") or 0)
            if max_mint_per_hour > 0 and mint_count >= max_mint_per_hour:
                return {
                    "ok": False,
                    "reason": "max_mint_sell_per_hour",
                    "action": str(action),
                    "bypass_interval": bool(bypass_interval),
                    "pacing": snap,
                }

        return {
            "ok": True,
            "reason": None,
            "action": str(action),
            "bypass_interval": bool(bypass_interval),
            "pacing": snap,
        }

    def _pacing_record_action(
        self, *, action: str, now_ts: float | None = None
    ) -> None:
        now = float(time.time() if now_ts is None else now_ts)
        self._last_action_ts = float(now)
        if str(action) == "mint_sell":
            self._mint_sell_action_ts.append(float(now))
        cutoff = float(now) - 3600.0
        self._mint_sell_action_ts = [
            ts for ts in self._mint_sell_action_ts if float(ts) >= cutoff
        ]

    def _build_stake_recommendation(
        self,
        *,
        mint_needed_units: int,
        mint_check: dict[str, Any] | None,
        mint_rate: float | None,
        corr_id: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(mint_check, dict):
            return None
        required = mint_check.get("required_svvv")
        available = mint_check.get("available_svvv")
        try:
            required_units = int(required)
            available_units = int(available)
        except Exception:
            return None
        shortfall_units = max(0, required_units - available_units)
        if shortfall_units <= 0:
            return None
        rec: dict[str, Any] = {
            "action": "stake_vvv",
            "reason": "insufficient_svvv",
            "required_units": required_units,
            "available_units": available_units,
            "shortfall_units": shortfall_units,
            "mint_needed_units": int(mint_needed_units),
            "mint_rate": mint_rate,
            "source": "arbi_diem",
            "requested_svvv_units": shortfall_units,
            "ts": time.time(),
        }
        try:
            decimals = int(os.getenv("VVV_DECIMALS", "18"))
            if decimals >= 0:
                scale = 10**decimals
                rec["shortfall_tokens"] = float(shortfall_units) / float(scale)
        except Exception:
            pass
        if corr_id:
            rec["correlation_id"] = corr_id
        return rec

    def _liquidity_max_adjust_steps(self) -> int:
        """Maximum number of iterations to shrink trade size when slippage exceeds cap.

        Default: 10 steps (allows halving from initial size up to ~1000x reduction).
        """
        try:
            raw = os.getenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10")
            return max(1, int(raw))
        except Exception:
            return 10

    def _liquidity_min_trade_usd(self) -> float:
        """Minimum trade notional in USD below which we stop shrinking.

        Default: $2.0 (aligned with ARBI_DIEM_TRADE_USD default).
        Trades smaller than this threshold will not execute even if slippage is
        acceptable.
        """
        try:
            raw = os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "1.0")
            return max(0.0, float(raw))
        except Exception:
            return 1.0

    def _recovery_min_trade_usd(self) -> float:
        """Minimum trade notional in USD for capacity recovery actions.

        Default: ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD (shared floor).
        """
        raw = os.getenv("DIEM_RECOVERY_MIN_TRADE_USD")
        if raw is None or str(raw).strip() == "":
            return float(self._liquidity_min_trade_usd())
        try:
            return max(0.0, float(raw))
        except Exception:
            return float(self._liquidity_min_trade_usd())

    def _recovery_max_trade_usd(self) -> float | None:
        """Maximum trade notional in USD for capacity recovery actions.

        Default: unset (no additional cap beyond wallet + risk policy).
        """
        raw = os.getenv("DIEM_RECOVERY_MAX_USD_PER_CYCLE")
        if raw is None or str(raw).strip() == "":
            raw = os.getenv("DIEM_RECOVERY_MAX_TRADE_USD")
        if raw is None or str(raw).strip() == "":
            return None
        try:
            cap = float(raw)
        except Exception:
            return None
        if not math.isfinite(cap) or cap <= 0:
            return None
        return float(cap)

    def _recovery_converge_steps(self) -> int:
        """Target number of successful recovery actions to reach the target ratio.

        Default: 5 cycles.
        """
        raw = os.getenv("DIEM_RECOVERY_CONVERGE_STEPS")
        if raw is None or str(raw).strip() == "":
            return 5
        try:
            return max(1, int(float(str(raw).strip())))
        except Exception:
            return 5

    def _recovery_max_steps(self) -> int:
        """Maximum number of size adjustment steps for capacity recovery previews.

        Default: 10 (halving each step).
        """
        raw = os.getenv("DIEM_RECOVERY_MAX_STEPS")
        if raw is None or str(raw).strip() == "":
            return 10
        try:
            return max(1, int(float(str(raw).strip())))
        except Exception:
            return 10

    def _recovery_burn_premium_mult(self) -> float:
        """Allowed premium for buy+burn recovery vs buy+stake per sVVV.

        Default: 1.15 (burn can be up to 15% more expensive per sVVV before we
        prefer staking).
        """
        raw = os.getenv("DIEM_RECOVERY_BURN_PREMIUM_MULT")
        if raw is None or str(raw).strip() == "":
            return 1.15
        try:
            mult = float(str(raw).strip())
        except Exception:
            return 1.15
        if not math.isfinite(mult) or mult <= 0:
            return 1.15
        return max(1.0, float(mult))

    def _get_input_token_info(self) -> dict:
        """Fetch input token balance for decision visibility."""
        try:
            from services.wallet.provider import describe_treasury_portfolio

            snapshot = describe_treasury_portfolio(include_eth=False)
            usdc_info = snapshot.get("balances", {}).get("USDC", {})
            if isinstance(usdc_info, dict):
                units = int(usdc_info.get("units", 0))
                decimals = int(usdc_info.get("decimals", 6))
                return {
                    "token": "USDC",
                    "balance_wei": units,
                    "balance_usd": float(units) / (10**decimals),
                    "sufficient": float(units) / (10**decimals)
                    >= self._liquidity_min_trade_usd(),
                }
        except Exception:
            pass
        return {
            "token": "USDC",
            "balance_wei": 0,
            "balance_usd": 0.0,
            "sufficient": False,
        }

    def _locked_svvv_ratio_cap(self) -> float:
        """Maximum allowed locked_svvv / total_svvv before triggering recovery.

        Default: 1.0 (disabled).
        """
        raw = os.getenv("DIEM_LOCKED_SVVV_RATIO_CAP")
        if raw is None or str(raw).strip() == "":
            raw = os.getenv("ARBI_DIEM_LOCKED_SVVV_RATIO_CAP")
        if raw is None or str(raw).strip() == "":
            raw = os.getenv("ARBI_DIEM_SVVV_LOCKED_RATIO_CAP")
        if raw is None or str(raw).strip() == "":
            return 1.0
        try:
            cap = float(raw)
        except Exception:
            return 1.0
        if not math.isfinite(cap):
            return 1.0
        return max(0.0, min(1.0, cap))

    def _locked_svvv_ratio_target(self, cap: float) -> float:
        """Target locked ratio to reach after recovery (hysteresis).

        Default: cap (no hysteresis).
        """
        raw = os.getenv("DIEM_LOCKED_SVVV_RATIO_TARGET")
        if raw is None or str(raw).strip() == "":
            return float(cap)
        try:
            target = float(raw)
        except Exception:
            return float(cap)
        if not math.isfinite(target):
            return float(cap)
        target = max(0.0, min(1.0, target))
        if target > cap:
            return float(cap)
        return float(target)

    def _locked_svvv_ratio_min_total_units(self) -> int:
        """Minimum total sVVV units required before ratio-based recovery triggers.

        Default: 0 (no minimum).
        """
        raw = os.getenv("DIEM_LOCKED_SVVV_RATIO_MIN_TOTAL_SVVV_UNITS")
        if raw is None or str(raw).strip() == "":
            raw = os.getenv("DIEM_LOCKED_SVVV_RATIO_MIN_TOTAL_UNITS")
        if raw is None or str(raw).strip() == "":
            return 0
        try:
            return max(0, int(str(raw).strip(), 0))
        except Exception:
            try:
                return max(0, int(float(str(raw).strip())))
            except Exception:
                return 0

    def _vvv_fv_prefer_stake_discount_mult(self) -> float:
        """Prefer staking VVV when intrinsic FV / spot price exceeds this multiple.

        Default: 1.0 (any discount).
        """
        raw = os.getenv("VVV_FV_PREFER_STAKE_DISCOUNT_MULT")
        if raw is None or str(raw).strip() == "":
            return 1.0
        try:
            mult = float(raw)
        except Exception:
            return 1.0
        if not math.isfinite(mult):
            return 1.0
        return max(1.0, float(mult))

    def _svvv_lock_state(self, wallet_balances: dict[str, Any]) -> dict[str, Any]:
        total_units = int((wallet_balances.get("SVVV") or {}).get("units", 0) or 0)
        decimals = int((wallet_balances.get("SVVV") or {}).get("decimals", 18) or 18)

        locked_units: int | None = None
        try:
            locked_fn = getattr(self.diem, "_locked_svvv_for_wallet_safe", None)
            if callable(locked_fn):
                locked_units = locked_fn()
            else:
                locked_fn = getattr(self.diem, "_locked_svvv_for_wallet", None)
                if callable(locked_fn):
                    locked_units = locked_fn()
        except Exception:
            locked_units = None

        if locked_units is not None:
            try:
                locked_units = max(0, int(locked_units))
            except Exception:
                locked_units = None

        unlocked_units: int | None = None
        locked_ratio: float | None = None
        if locked_units is not None and total_units > 0:
            unlocked_units = max(0, int(total_units) - int(locked_units))
            locked_ratio = float(locked_units) / float(total_units)

        if locked_ratio is not None:
            try:
                _metrics_set_gauge("arbi_diem_locked_svvv_ratio", float(locked_ratio))
            except Exception:
                pass

        return {
            "total_units": int(total_units),
            "decimals": int(decimals),
            "locked_units": locked_units,
            "unlocked_units": unlocked_units,
            "locked_ratio": locked_ratio,
        }

    def _mint_rate_units_per_diem_unit(
        self, mint_rate_tokens: float | None
    ) -> int | None:
        """Return sVVV base units required per 1 DIEM base unit (if available)."""
        if mint_rate_tokens is None:
            return None
        try:
            d_dec, s_dec = self.diem._decimals_pair()
        except Exception:
            try:
                d_dec = int(os.getenv("DIEM_DECIMALS", "18"))
            except Exception:
                d_dec = 18
            try:
                s_dec = int(
                    os.getenv("SVVV_DECIMALS") or os.getenv("VVV_DECIMALS") or "18"
                )
            except Exception:
                s_dec = 18
        try:
            ratio = float(mint_rate_tokens) * float(10**s_dec) / float(10**d_dec)
            if ratio <= 0:
                return None
            return int(ratio)
        except Exception:
            return None

    def _mint_rate_tokens_per_diem_from_units(
        self, mint_rate_units: int | None
    ) -> float | None:
        """Convert sVVV_units_per_diem_unit -> sVVV_tokens_per_diem_token."""
        if mint_rate_units is None:
            return None
        try:
            units_i = int(mint_rate_units)
        except Exception:
            return None
        if units_i <= 0:
            return None
        try:
            d_dec, s_dec = self.diem._decimals_pair()
        except Exception:
            try:
                d_dec = int(os.getenv("DIEM_DECIMALS", "18"))
            except Exception:
                d_dec = 18
            try:
                s_dec = int(
                    os.getenv("SVVV_DECIMALS") or os.getenv("VVV_DECIMALS") or "18"
                )
            except Exception:
                s_dec = 18
        try:
            return float(units_i) * float(10**d_dec) / float(10**s_dec)
        except Exception:
            return None

    def _resolve_mint_rate_for_decision(
        self,
        *,
        mint_rate_arg: float | None,
        mint_rate_source_arg: str | None = None,
        simulate: bool,
    ) -> dict[str, Any]:
        """Resolve the DIEM mint rate used for fair-value and recovery economics.

        Returns a dict with:
        - mint_rate_svvv_per_diem (float, token units)
        - mint_rate_units_per_diem_unit (int, base units)
        - mint_rate_source (str)
        - mint_rate_source_detail (str)
        - mint_rate_env_override_present (bool)
        """
        env_override_present = bool(
            (os.getenv("DIEM_MINT_RATE_SVVV_PER_DIEM") or "").strip()
            or (os.getenv("DIEM_MINT_RATE") or "").strip()
            or (os.getenv("DIEM_FAKE_MINT_RATE") or "").strip()
        )

        def _normalize_source(source: str | None) -> tuple[str, str]:
            raw = str(source or "").strip()
            low = raw.lower()
            if not low:
                return ("unknown", "unknown")
            # Map everything into one explicit dimension for downstream analytics:
            # onchain vs env_override.
            envish = low.startswith("env") or low in {"param", "default", "override"}
            if envish:
                return ("env_override", raw)
            return ("onchain", raw)

        # If the caller provides the mint-rate (common in orchestrator-driven runs),
        # preserve the value and use the caller's reported source when available.
        if mint_rate_arg is not None:
            units = self._mint_rate_units_per_diem_unit(mint_rate_arg)
            semantic, detail = _normalize_source(mint_rate_source_arg or "provided")
            return {
                "mint_rate_svvv_per_diem": float(mint_rate_arg),
                "mint_rate_units_per_diem_unit": int(units)
                if units not in (None, 0)
                else None,
                "mint_rate_source": semantic,
                "mint_rate_source_detail": detail,
                "mint_rate_env_override_present": env_override_present,
            }

        # Offline simulation override.
        fake = (os.getenv("DIEM_FAKE_MINT_RATE") or "").strip()
        if fake:
            try:
                mr = float(fake)
            except Exception:
                mr = None
            units = self._mint_rate_units_per_diem_unit(mr) if mr is not None else None
            if mr is not None and mr > 0:
                if self._run_mode != "dry" and not simulate:
                    logger.warning(
                        "DIEM_FAKE_MINT_RATE is set in non-dry mode; using it for mint-rate decisions"
                    )
                return {
                    "mint_rate_svvv_per_diem": float(mr),
                    "mint_rate_units_per_diem_unit": int(units)
                    if units not in (None, 0)
                    else None,
                    "mint_rate_source": "env_override",
                    "mint_rate_source_detail": "env_fake",
                    "mint_rate_env_override_present": env_override_present,
                }

        # Prefer on-chain mint rate in live mode (and in dry-run when available).
        mint_rate_units: int | None = None
        onchain_info: dict[str, Any] | None = None
        try:
            fetch_fn = getattr(self.diem, "fetch_mint_rate_onchain", None)
            if callable(fetch_fn):
                onchain_info = fetch_fn(ttl_s=300)
        except Exception:
            onchain_info = None
        if isinstance(onchain_info, dict) and str(onchain_info.get("status")) == "ok":
            try:
                mint_rate_units = int(onchain_info.get("svvv_units_per_diem") or 0)
            except Exception:
                mint_rate_units = None
        if mint_rate_units in (None, 0):
            try:
                query_fn = getattr(self.diem, "_query_mint_rate_onchain_safe", None)
                if callable(query_fn):
                    try:
                        mint_rate_units = query_fn()
                    except TypeError:
                        mint_rate_units = query_fn(self.diem)  # type: ignore[arg-type]
            except Exception:
                mint_rate_units = None

        if mint_rate_units not in (None, 0):
            mr_tokens = self._mint_rate_tokens_per_diem_from_units(int(mint_rate_units))
            if mr_tokens is not None and mr_tokens > 0:
                if env_override_present and self._run_mode == "live" and not simulate:
                    logger.info(
                        "Mint-rate env override present, but using on-chain mint rate for decisions",
                        extra={"mint_rate_units_per_diem_unit": int(mint_rate_units)},
                    )
                return {
                    "mint_rate_svvv_per_diem": float(mr_tokens),
                    "mint_rate_units_per_diem_unit": int(mint_rate_units),
                    "mint_rate_source": "onchain",
                    "mint_rate_source_detail": "onchain",
                    "mint_rate_env_override_present": env_override_present,
                }

        # Fallback: env-configured mint rate (logged when used in live mode).
        env_units = (os.getenv("DIEM_MINT_RATE_SVVV_PER_DIEM") or "").strip()
        if env_units:
            try:
                mint_rate_units = int(env_units)
            except Exception:
                mint_rate_units = None
            mr_tokens = self._mint_rate_tokens_per_diem_from_units(mint_rate_units)
            if mr_tokens is not None and mr_tokens > 0:
                if self._run_mode == "live" and not simulate:
                    logger.warning(
                        "Using DIEM_MINT_RATE_SVVV_PER_DIEM env override for mint-rate decisions in live mode",
                        extra={"mint_rate_units_per_diem_unit": mint_rate_units},
                    )
                return {
                    "mint_rate_svvv_per_diem": float(mr_tokens),
                    "mint_rate_units_per_diem_unit": int(mint_rate_units)
                    if mint_rate_units not in (None, 0)
                    else None,
                    "mint_rate_source": "env_override",
                    "mint_rate_source_detail": "env_override_units",
                    "mint_rate_env_override_present": env_override_present,
                }

        env_tokens = (os.getenv("DIEM_MINT_RATE") or "").strip()
        if env_tokens:
            try:
                mr = float(env_tokens)
            except Exception:
                mr = None
            units = self._mint_rate_units_per_diem_unit(mr) if mr is not None else None
            if mr is not None and mr > 0:
                if self._run_mode == "live" and not simulate:
                    logger.warning(
                        "Using DIEM_MINT_RATE env override for mint-rate decisions in live mode",
                        extra={"mint_rate_svvv_per_diem": mr},
                    )
                return {
                    "mint_rate_svvv_per_diem": float(mr),
                    "mint_rate_units_per_diem_unit": int(units)
                    if units not in (None, 0)
                    else None,
                    "mint_rate_source": "env_override",
                    "mint_rate_source_detail": "env_override_tokens",
                    "mint_rate_env_override_present": env_override_present,
                }

        # Last resort: client helper (may read cached on-chain or market data, depending on availability).
        try:
            fallback_fn = getattr(self.diem, "_mint_rate_svvv_per_diem_units", None)
            if callable(fallback_fn):
                try:
                    mint_rate_units = fallback_fn()
                except TypeError:
                    mint_rate_units = fallback_fn(self.diem)  # type: ignore[arg-type]
        except Exception:
            mint_rate_units = None
        mr_tokens = self._mint_rate_tokens_per_diem_from_units(mint_rate_units)
        if mr_tokens is not None and mr_tokens > 0:
            return {
                "mint_rate_svvv_per_diem": float(mr_tokens),
                "mint_rate_units_per_diem_unit": int(mint_rate_units)
                if mint_rate_units not in (None, 0)
                else None,
                "mint_rate_source": "onchain",
                "mint_rate_source_detail": "fallback_client",
                "mint_rate_env_override_present": env_override_present,
            }

        return {
            "mint_rate_svvv_per_diem": None,
            "mint_rate_units_per_diem_unit": None,
            "mint_rate_source": "unavailable",
            "mint_rate_source_detail": "unavailable",
            "mint_rate_env_override_present": env_override_present,
        }

    @staticmethod
    def _ceil_div(n: int, d: int) -> int:
        if d <= 0:
            return 0
        return int((int(n) + int(d) - 1) // int(d))

    def _execute_capacity_recovery_buy_burn(
        self,
        *,
        diem_amount_units: int,
        slippage_bps: int,
        pool_take_bps: int | None,
        corr_id: str | None,
        simulate: bool,
        portfolio_snapshot: dict | None,
    ) -> dict[str, Any]:
        res = self.diem.wallet_first_buy_and_burn(
            diem_amount=int(diem_amount_units),
            slippage_bps=int(slippage_bps),
            pool_take_bps=pool_take_bps,
            simulate=bool(simulate),
            portfolio_snapshot=portfolio_snapshot,
        )
        if isinstance(res, dict) and corr_id:
            res.setdefault("correlation_id", corr_id)
        return res if isinstance(res, dict) else {"status": "unknown", "raw": res}

    def _execute_capacity_recovery_buy_vvv_and_stake(
        self,
        *,
        usdc_amount_units: int,
        slippage_bps: int,
        corr_id: str | None,
        simulate: bool,
    ) -> dict[str, Any]:
        aggregator = getattr(self.diem, "aggregator", None)
        if aggregator is None:
            return {"status": "error", "error": "missing_aggregator"}

        quote_token = os.getenv("QUOTE_TOKEN_ADDRESS") or os.getenv(
            "USDC_TOKEN_ADDRESS"
        )
        vvv_token = os.getenv("VVV_TOKEN_ADDRESS")
        if not quote_token or not vvv_token:
            return {"status": "error", "error": "missing_token_addresses"}

        try:
            from libs.dex.routes import make_route

            route = make_route([quote_token, vvv_token])
        except Exception as exc:
            return {"status": "error", "error": f"route_error:{exc}"}
        if route is None:
            return {"status": "error", "error": "route_unavailable"}

        quote = None
        try:
            quote = aggregator.best_quote(int(usdc_amount_units), route)
        except Exception as exc:
            return {"status": "error", "error": f"quote_error:{exc}"}
        if quote is None:
            return {"status": "error", "error": "no_quote"}

        vvv_out_units = int(getattr(quote, "amount_out", 0) or 0)
        try:
            min_out_units = int(
                int(vvv_out_units) * (10_000 - int(slippage_bps)) // 10_000
            )
        except Exception:
            min_out_units = 0

        payload: dict[str, Any] = {
            "status": "dry_run" if simulate else "pending",
            "action": "buy_vvv_and_stake",
            "usdc_in_units": int(usdc_amount_units),
            "vvv_out_units": int(vvv_out_units),
            "vvv_min_out_units": int(min_out_units),
            "slippage_bps": int(slippage_bps),
            "route": list(getattr(quote, "path", []) or []),
            "provider": getattr(quote, "provider", None),
        }
        if corr_id:
            payload["correlation_id"] = corr_id

        if simulate:
            payload["status"] = "simulated"
            return payload

        try:
            from libs.agentkit_ext.actions import VVVActions
            from libs.agentkit_ext.agentkit_wallet import get_address
            from services.staking.client import (
                StakingService,
                run_with_stake_overflow_backoff,
            )
            from services.staking.limits import (
                IdleStakeLimits,
                idle_stake_limits_payload,
            )

            staking = StakingService(actions=VVVActions())
        except Exception as exc:
            payload.update({"status": "error", "error": f"staking_init_error:{exc}"})
            return payload

        # Capacity recovery staking needs to be able to move quickly.
        # Apply the wallet buffer guardrail by default, but do not enforce a per-cycle
        # cap unless explicitly configured via STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS.
        limits = IdleStakeLimits.from_env(default_max_per_cycle_units=0)
        payload["stake_limits"] = idle_stake_limits_payload(limits)

        def _wallet_vvv_balance_units() -> int | None:
            try:
                bal_fn = getattr(staking.actions, "balance_of", None)
                if callable(bal_fn):
                    return int(bal_fn())
            except Exception:
                pass
            try:
                token = getattr(staking.actions, "erc20", None)
                if token is None:
                    return None
                wallet = get_address()
                if not wallet:
                    return None
                try:
                    from web3 import Web3 as _Web3  # type: ignore

                    wallet = _Web3.to_checksum_address(wallet)
                except Exception:
                    pass
                return int(token.functions.balanceOf(wallet).call())
            except Exception:
                return None

        def _log_capacity_recovery_partial_failure(
            *,
            swap_tx_hash: str | None,
            reason: str,
            remaining_vvv_units: int | None,
        ) -> None:
            try:
                logger.warning(
                    "capacity_recovery_partial_failure",
                    extra={
                        "event": "capacity_recovery_partial_failure",
                        "reason": str(reason),
                        "swap_tx_hash": str(swap_tx_hash) if swap_tx_hash else None,
                        "remaining_vvv_units": (
                            int(remaining_vvv_units)
                            if remaining_vvv_units is not None
                            else None
                        ),
                        "correlation_id": corr_id,
                    },
                )
            except Exception:
                pass

        pending = getattr(self, "_capacity_recovery_pending_stake", None)
        if isinstance(pending, dict) and pending:
            payload["action"] = "stake_existing_vvv"
            payload["pending_stake"] = dict(pending)
            payload["swap"] = {"status": "skipped", "reason": "pending_vvv_stake"}

            try:
                requested_units = int(pending.get("vvv_units") or 0)
            except Exception:
                requested_units = 0
            available_units = _wallet_vvv_balance_units()
            if available_units is not None:
                payload["vvv_balance_units"] = int(available_units)
            if requested_units <= 0 and available_units is not None:
                requested_units = int(available_units)
            requested_units = limits.apply(
                requested_units=int(requested_units),
                wallet_balance_units=available_units,
            )

            if requested_units <= 0:
                self._capacity_recovery_pending_stake = None
                payload.update(
                    {
                        "status": "error",
                        "error": "pending_vvv_stake_empty",
                    }
                )
                return payload

            payload["vvv_stake_requested_units"] = int(requested_units)

            approve_res = None
            try:
                approve_res = staking.approve(int(requested_units))
            except Exception as exc:
                payload.update({"status": "error", "error": f"approve_error:{exc}"})
                return payload

            try:
                stake_tx, staked_units, attempts, stop_reason = (
                    run_with_stake_overflow_backoff(
                        staking.stake,
                        int(requested_units),
                    )
                )
            except Exception as exc:
                remaining_units = _wallet_vvv_balance_units()
                self._capacity_recovery_pending_stake = {
                    **dict(pending),
                    "vvv_units": int(remaining_units or requested_units),
                    "reason": f"stake_error:{exc}",
                    "ts": float(time.time()),
                }
                _log_capacity_recovery_partial_failure(
                    swap_tx_hash=(
                        str(pending.get("swap_tx_hash"))
                        if pending.get("swap_tx_hash")
                        else None
                    ),
                    reason=f"stake_error:{exc}",
                    remaining_vvv_units=remaining_units,
                )
                payload.update(
                    {
                        "status": "partial_failure",
                        "error": f"stake_error:{exc}",
                        "approve": approve_res,
                        "stake": None,
                        "stake_overflow_attempts": [],
                    }
                )
                return payload

            payload["approve"] = approve_res
            payload["stake"] = stake_tx
            payload["stake_overflow_attempts"] = attempts
            payload["vvv_stake_submitted_units"] = int(staked_units)
            if stop_reason is None and stake_tx is not None:
                self._capacity_recovery_pending_stake = None
                payload["status"] = "submitted"
                return payload

            remaining_units = _wallet_vvv_balance_units()
            self._capacity_recovery_pending_stake = {
                **dict(pending),
                "vvv_units": int(remaining_units or requested_units),
                "reason": f"overflow_backoff_exhausted:{stop_reason}",
                "ts": float(time.time()),
            }
            _log_capacity_recovery_partial_failure(
                swap_tx_hash=(
                    str(pending.get("swap_tx_hash"))
                    if pending.get("swap_tx_hash")
                    else None
                ),
                reason=f"overflow_backoff_exhausted:{stop_reason}",
                remaining_vvv_units=remaining_units,
            )
            payload.update(
                {
                    "status": "partial_failure",
                    "error": f"overflow_backoff_exhausted:{stop_reason}",
                    "remaining_vvv_units": (
                        int(remaining_units) if remaining_units is not None else None
                    ),
                }
            )
            return payload

        # Precheck using quote-derived expected out, capped by max-per-cycle.
        # Wallet buffer is enforced later using post-swap wallet balance.
        stake_intended_units = int(vvv_out_units)
        if int(limits.max_per_cycle_units) > 0:
            stake_intended_units = min(
                int(stake_intended_units), int(limits.max_per_cycle_units)
            )
        payload["vvv_stake_intended_units"] = int(stake_intended_units)
        try:
            _pre_res, feasible_units, pre_attempts, pre_stop = (
                run_with_stake_overflow_backoff(
                    staking.estimate_stake, int(stake_intended_units)
                )
            )
        except Exception as exc:
            payload.update({"status": "error", "error": f"stake_precheck_error:{exc}"})
            return payload

        payload["stake_precheck"] = {
            "attempts": pre_attempts,
            "stop_reason": pre_stop,
            "feasible_units": int(feasible_units),
        }
        if pre_stop is not None:
            payload.update(
                {
                    "status": "error",
                    "error": f"stake_precheck_unfeasible:{pre_stop}",
                }
            )
            return payload

        vvv_balance_before = _wallet_vvv_balance_units()
        if vvv_balance_before is not None:
            payload["vvv_balance_before_units"] = int(vvv_balance_before)

        try:
            swap_kwargs: dict[str, Any] = {}
            if corr_id:
                swap_kwargs["correlation_id"] = corr_id
            swap_res = aggregator.trade_best(
                int(usdc_amount_units), int(slippage_bps), route, **swap_kwargs
            )
        except Exception as exc:
            payload.update({"status": "error", "error": f"swap_error:{exc}"})
            return payload

        payload["swap"] = swap_res

        swap_tx_hash = None
        if isinstance(swap_res, dict):
            swap_tx_hash = swap_res.get("tx_hash") or swap_res.get("hash")
        if swap_tx_hash:
            payload["swap_tx_hash"] = str(swap_tx_hash)

        vvv_received_units: int | None = None
        vvv_balance_after: int | None = None
        if swap_tx_hash:
            try:
                timeout_raw = os.getenv(
                    "ARBI_DIEM_CAPACITY_RECOVERY_SWAP_RECEIPT_TIMEOUT_S", "90"
                )
                timeout_s = int(str(timeout_raw), 0)
            except Exception:
                timeout_s = 90
            timeout_s = max(0, int(timeout_s))
            if timeout_s > 0:
                try:
                    w3 = getattr(staking.actions, "w3", None) or get_web3()
                    receipt = w3.eth.wait_for_transaction_receipt(
                        str(swap_tx_hash), timeout=timeout_s
                    )
                    receipt_status = None
                    if isinstance(receipt, dict):
                        receipt_status = receipt.get("status")
                    else:
                        receipt_status = getattr(receipt, "status", None)
                    if receipt_status is not None:
                        payload["swap_receipt_status"] = int(receipt_status)
                except Exception as exc:
                    payload["swap_receipt_error"] = str(exc)

            vvv_balance_after = _wallet_vvv_balance_units()
            if vvv_balance_after is not None:
                payload["vvv_balance_after_units"] = int(vvv_balance_after)
                if vvv_balance_before is not None:
                    vvv_received_units = max(
                        0, int(vvv_balance_after) - int(vvv_balance_before)
                    )
                else:
                    vvv_received_units = int(vvv_balance_after)
                payload["vvv_received_units"] = int(vvv_received_units)

        if vvv_balance_after is None:
            # Best-effort: still try to read wallet balance even when receipt tracking fails.
            vvv_balance_after = _wallet_vvv_balance_units()
            if vvv_balance_after is not None:
                payload["vvv_balance_after_units"] = int(vvv_balance_after)

        stake_request_units = (
            int(vvv_received_units)
            if vvv_received_units not in (None, 0, 0.0)
            else int(feasible_units)
        )
        if int(feasible_units) > 0:
            stake_request_units = min(int(stake_request_units), int(feasible_units))
        stake_request_units = limits.apply(
            requested_units=int(stake_request_units),
            wallet_balance_units=vvv_balance_after,
        )

        payload["vvv_stake_requested_units"] = int(stake_request_units)
        approve_res = None
        stake_tx: dict[str, Any] | None = None
        stake_attempts: list[dict[str, Any]] = []
        stake_units_int = int(stake_request_units)
        if stake_units_int <= 0:
            error_msg = "zero_stake_units"
            raise RuntimeError(error_msg)
        try:
            approve_res = staking.approve(stake_units_int)
        except Exception as exc:
            remaining_units = (
                vvv_balance_after
                if vvv_balance_after is not None
                else _wallet_vvv_balance_units()
            )
            self._capacity_recovery_pending_stake = {
                "swap_tx_hash": str(swap_tx_hash) if swap_tx_hash else None,
                "vvv_units": int(remaining_units or 0),
                "reason": f"approve_error:{exc}",
                "ts": float(time.time()),
                "correlation_id": corr_id,
            }
            _log_capacity_recovery_partial_failure(
                swap_tx_hash=str(swap_tx_hash) if swap_tx_hash else None,
                reason=f"approve_error:{exc}",
                remaining_vvv_units=remaining_units,
            )
            payload.update(
                {
                    "status": "partial_failure",
                    "error": f"approve_error:{exc}",
                    "approve": approve_res,
                    "stake": None,
                    "remaining_vvv_units": (
                        int(remaining_units) if remaining_units is not None else None
                    ),
                }
            )
            return payload

        try:
            stake_tx, staked_units, stake_attempts, stop_reason = (
                run_with_stake_overflow_backoff(
                    staking.stake,
                    int(stake_request_units),
                )
            )
        except Exception as exc:
            remaining_units = (
                vvv_balance_after
                if vvv_balance_after is not None
                else _wallet_vvv_balance_units()
            )
            self._capacity_recovery_pending_stake = {
                "swap_tx_hash": str(swap_tx_hash) if swap_tx_hash else None,
                "vvv_units": int(remaining_units or stake_request_units),
                "reason": f"stake_error:{exc}",
                "ts": float(time.time()),
                "correlation_id": corr_id,
            }
            _log_capacity_recovery_partial_failure(
                swap_tx_hash=str(swap_tx_hash) if swap_tx_hash else None,
                reason=f"stake_error:{exc}",
                remaining_vvv_units=remaining_units,
            )
            payload.update(
                {
                    "status": "partial_failure",
                    "error": f"stake_error:{exc}",
                    "approve": approve_res,
                    "stake": None,
                    "stake_overflow_attempts": [],
                    "remaining_vvv_units": (
                        int(remaining_units) if remaining_units is not None else None
                    ),
                }
            )
            return payload

        payload["approve"] = approve_res
        payload["stake"] = stake_tx
        payload["stake_overflow_attempts"] = stake_attempts
        payload["vvv_stake_submitted_units"] = int(staked_units)
        if stop_reason is None and stake_tx is not None:
            payload["status"] = "submitted"
            return payload

        remaining_units = (
            vvv_balance_after
            if vvv_balance_after is not None
            else _wallet_vvv_balance_units()
        )
        self._capacity_recovery_pending_stake = {
            "swap_tx_hash": str(swap_tx_hash) if swap_tx_hash else None,
            "vvv_units": int(remaining_units or stake_request_units),
            "reason": f"overflow_backoff_exhausted:{stop_reason}",
            "ts": float(time.time()),
            "correlation_id": corr_id,
        }
        _log_capacity_recovery_partial_failure(
            swap_tx_hash=str(swap_tx_hash) if swap_tx_hash else None,
            reason=f"overflow_backoff_exhausted:{stop_reason}",
            remaining_vvv_units=remaining_units,
        )
        payload.update(
            {
                "status": "partial_failure",
                "error": f"overflow_backoff_exhausted:{stop_reason}",
                "remaining_vvv_units": (
                    int(remaining_units) if remaining_units is not None else None
                ),
            }
        )
        return payload

    def _maybe_capacity_recovery(
        self,
        *,
        market_price: float,
        fair_value: float,
        threshold_mult: float,
        mint_rate: float,
        slippage_cap_bps: float,
        pool_take_bps: int | None,
        corr_id: str | None,
        simulate: bool,
        exec_ctx: ExecutionContext,
        svvv_lock_state: dict[str, Any] | None = None,
        rationale: dict[str, Any],
        utilization_ratio: float | None,
        vol_bps: float | None,
        current_inventory_usd: float | None,
        vvv_price_usd: float,
        force_stake_only: bool = False,
    ) -> bool:
        cap = float(self._locked_svvv_ratio_cap())

        # Stable rationale keys for downstream logging/analytics.
        rationale.setdefault("locked_ratio", None)
        rationale.setdefault("ratio_cap", float(cap))
        rationale.setdefault("ratio_target", None)
        rationale.setdefault("recovery_action", None)
        rationale.setdefault("recovery_units", None)
        rationale.setdefault("vvv_fair_value_usd", None)
        rationale.setdefault("vvv_fair_value_components", None)

        if cap >= 1.0:
            return False

        target = float(self._locked_svvv_ratio_target(cap))

        try:
            rationale["ratio_target"] = float(target)
        except Exception:
            rationale["ratio_target"] = None

        preferred_action = str(self._recovery_preferred_action() or "auto")

        slippage_cap_bps_base = None
        try:
            slippage_cap_bps_base = float(slippage_cap_bps)
        except Exception:
            slippage_cap_bps_base = None

        # Base recovery slippage cap stays aligned with the global risk cap,
        # but we can widen it for *recovery-only* actions on small notional.
        try:
            slippage_cap_bps = min(
                float(slippage_cap_bps), float(self.risk.slippage_bps_cap)
            )
        except Exception:
            pass
        slippage_cap_bps_base_risk = None
        try:
            slippage_cap_bps_base_risk = float(slippage_cap_bps)
        except Exception:
            slippage_cap_bps_base_risk = None

        recovery_max_slippage_bps = float(self._recovery_max_slippage_bps())
        recovery_small_trade_usd = float(self._recovery_small_trade_usd())
        recovery_price_sanity_max_rel_diff = float(
            self._recovery_price_sanity_max_rel_diff()
        )

        def _recovery_slippage_cap(
            *, estimated_trade_usd: float | None
        ) -> tuple[float, bool, str | None]:
            cap_bps = float(slippage_cap_bps)
            applied = False
            reason = None
            if (
                estimated_trade_usd is not None
                and math.isfinite(float(estimated_trade_usd))
                and float(estimated_trade_usd) > 0
                and float(estimated_trade_usd) <= float(recovery_small_trade_usd)
                and float(recovery_max_slippage_bps) > float(cap_bps)
            ):
                cap_bps = min(10_000.0, float(recovery_max_slippage_bps))
                applied = True
                reason = "small_trade"
            return float(cap_bps), bool(applied), str(reason) if reason else None

        # Stable recovery-only slippage logging fields.
        rationale.setdefault("recovery_slippage_cap_bps", None)
        rationale.setdefault("recovery_slippage_applied", None)
        rationale.setdefault("recovery_slippage_applied_reason", None)
        rationale.setdefault("capacity_recovery_blocked_reason", None)

        # Ensure recovery buys honor the pool-take cap when available.
        if pool_take_bps is None:
            try:
                pool_take_bps = int(getattr(self.risk, "pool_take_bps_cap", 25))
            except Exception:
                pool_take_bps = 25
        if pool_take_bps is not None and int(pool_take_bps) <= 0:
            pool_take_bps = None

        lock_state = (
            svvv_lock_state
            if isinstance(svvv_lock_state, dict) and svvv_lock_state
            else self._svvv_lock_state(exec_ctx.balances)
        )
        locked_units = lock_state.get("locked_units")
        total_units = int(lock_state.get("total_units") or 0)
        locked_ratio = lock_state.get("locked_ratio")

        rationale["svvv_lock_state"] = lock_state
        rationale["svvv_locked_ratio_cap"] = cap

        try:
            rationale["locked_ratio"] = (
                float(locked_ratio) if locked_ratio is not None else None
            )
        except Exception:
            rationale["locked_ratio"] = None

        if locked_units is None or total_units <= 0 or locked_ratio is None:
            return False

        locked_ratio_f = None
        try:
            locked_ratio_f = float(locked_ratio)
        except Exception:
            locked_ratio_f = None

        # Emit diagnostics for locked sVVV ratio calculations to aid recovery visibility.
        try:
            _dex_diag_log_event(
                {
                    "event": "svvv_locked_ratio",
                    "locked_units": int(locked_units)
                    if locked_units is not None
                    else None,
                    "total_units": int(total_units),
                    "locked_ratio": float(locked_ratio_f)
                    if locked_ratio_f is not None
                    else None,
                    "ratio_cap": float(cap),
                    "ratio_target": float(target),
                    "simulate": bool(simulate),
                    "correlation_id": corr_id,
                }
            )
        except Exception:
            pass

        def _log_recovery_campaign_attempt(
            *,
            selected_option: str,
            blocked_reason: str | None,
            trade_usd: float | None,
            stage: str,
        ) -> None:
            payload = {
                "event": "recovery_campaign_attempt",
                "stage": str(stage),
                "selected_option": str(selected_option),
                "blocked_reason": (
                    str(blocked_reason) if blocked_reason is not None else None
                ),
                "current_locked_ratio": (
                    float(locked_ratio_f) if locked_ratio_f is not None else None
                ),
                "target_ratio": float(target),
                "ratio_cap": float(cap),
                "slippage_bps": float(slippage_cap_bps),
                "trade_usd": float(trade_usd) if trade_usd is not None else None,
                "simulate": bool(simulate),
                "correlation_id": corr_id,
            }
            try:
                logger.info("recovery_campaign_attempt", extra=payload)
            except Exception:
                pass

        min_total_units = int(self._locked_svvv_ratio_min_total_units())
        if min_total_units > 0 and total_units < min_total_units:
            rationale["capacity_recovery_blocked_reason"] = "below_min_total_svvv"
            rationale["capacity_recovery_min_total_svvv_units"] = int(min_total_units)
            _log_recovery_campaign_attempt(
                selected_option="none",
                blocked_reason="below_min_total_svvv",
                trade_usd=None,
                stage="blocked",
            )
            return False

        cap_bps = max(0, min(10_000, round(float(cap) * 10_000)))
        if cap_bps <= 0:
            return False

        # Target threshold (hysteresis) - aim to reduce ratio to `target` (<= cap).
        target_bps = max(0, min(10_000, round(float(target) * 10_000)))
        if target_bps <= 0:
            return False

        # Trigger threshold (cap) - only start recovery when ratio exceeds cap.
        max_locked_units_trigger = int(total_units) * int(cap_bps) // 10_000
        max_locked_units_target = int(total_units) * int(target_bps) // 10_000

        # Stateful recovery plan:
        # - Start only when ratio exceeds cap.
        # - Once started, continue until ratio reaches target (<= cap).
        plan = self._recovery_plan
        if plan is not None and (
            int(plan.target_bps) != int(target_bps) or int(plan.cap_bps) != int(cap_bps)
        ):
            plan = None
            self._recovery_plan = None

        if int(locked_units) <= int(max_locked_units_target):
            self._recovery_plan = None
            self._capacity_recovery_unlock_revert_streak = 0
            return False

        if int(locked_units) > int(max_locked_units_trigger):
            if plan is None:
                plan = _RecoveryPlan(
                    steps_total=int(self._recovery_converge_steps()),
                    steps_done=0,
                    target_bps=int(target_bps),
                    cap_bps=int(cap_bps),
                )
                self._recovery_plan = plan
        elif plan is None:
            return False

        steps_total = int(plan.steps_total) if plan is not None else 1
        steps_done = int(plan.steps_done) if plan is not None else 0
        steps_remaining = max(1, int(steps_total) - int(steps_done))

        unlocked_units = int(lock_state.get("unlocked_units") or 0)
        target_unlocked_units = (
            int(total_units) * int(10_000 - int(target_bps)) // 10_000
        )
        need_unlock_units = max(0, int(target_unlocked_units) - int(unlocked_units))
        excess_locked_units = max(0, int(locked_units) - int(max_locked_units_target))

        # Compute DIEM burn amount required to unlock `need_unlock_units` sVVV.
        mint_rate_units: int | None = None
        try:
            query_fn = getattr(self.diem, "_query_mint_rate_onchain_safe", None)
            if callable(query_fn):
                try:
                    mint_rate_units = query_fn()
                except TypeError:
                    mint_rate_units = query_fn(self.diem)  # type: ignore[arg-type]
        except Exception:
            mint_rate_units = None
        if mint_rate_units in (None, 0):
            try:
                fallback_fn = getattr(self.diem, "_mint_rate_svvv_per_diem_units", None)
                if callable(fallback_fn):
                    try:
                        mint_rate_units = fallback_fn()
                    except TypeError:
                        mint_rate_units = fallback_fn(self.diem)  # type: ignore[arg-type]
            except Exception:
                mint_rate_units = None
        burn_units_target = 0
        burn_unlock_units = 0
        burn_probe: dict[str, Any] | None = None
        scale = 10**18
        try:
            diem_decimals, _ = self.diem._decimals_pair()
            scale = 10 ** max(int(diem_decimals), 0)
        except Exception:
            try:
                scale = 10 ** max(int(os.getenv("DIEM_DECIMALS", "18")), 0)
            except Exception:
                scale = 10**18

        if mint_rate_units not in (None, 0) and need_unlock_units > 0:
            unlock_step_units = self._ceil_div(
                int(excess_locked_units), int(steps_remaining)
            )
            burn_units_target = self._ceil_div(
                int(unlock_step_units) * int(scale), int(mint_rate_units)
            )
            try:
                burn_fn = getattr(self.diem, "_can_burn_diem", None)
                if callable(burn_fn):
                    try:
                        burn_probe = burn_fn(int(burn_units_target))
                    except TypeError:
                        burn_probe = burn_fn(self.diem, int(burn_units_target))  # type: ignore[arg-type]
            except Exception:
                burn_probe = None
            if isinstance(burn_probe, dict):
                try:
                    burn_unlock_units = int(burn_probe.get("required_svvv") or 0)
                except Exception:
                    burn_unlock_units = 0
                if not bool(burn_probe.get("can_burn", False)):
                    if (
                        str(burn_probe.get("reason") or "").strip().lower()
                        == "insufficient_locked_svvv"
                    ):
                        try:
                            locked_svvv = int(burn_probe.get("locked_svvv") or 0)
                            mint_rate_eff = int(
                                burn_probe.get("mint_rate") or mint_rate_units or 0
                            )
                            max_burnable = (
                                int(locked_svvv) * int(scale) // int(mint_rate_eff)
                                if mint_rate_eff > 0
                                else 0
                            )
                        except Exception:
                            max_burnable = 0
                        if max_burnable > 0 and int(burn_units_target) > int(
                            max_burnable
                        ):
                            burn_units_target = int(max_burnable)
                            try:
                                burn_probe = burn_fn(int(burn_units_target))  # type: ignore[misc]
                                if isinstance(burn_probe, dict):
                                    burn_unlock_units = int(
                                        burn_probe.get("required_svvv") or 0
                                    )
                            except Exception:
                                pass

        # Compute stake amount (VVV units) required to lower ratio via denominator growth.
        stake_units_needed = 0
        required_total = self._ceil_div(int(locked_units) * 10_000, int(target_bps))
        if required_total > total_units:
            stake_units_needed = int(required_total) - int(total_units)
        stake_units_target = (
            self._ceil_div(int(stake_units_needed), int(steps_remaining))
            if stake_units_needed > 0
            else 0
        )

        vvv_fv_result: dict[str, Any] | None = None
        vvv_fair_value = None
        try:
            vvv_pricing = import_module("libs.pricing.vvv")
            emissions_per_day = None
            diem_per_day = None

            env_emissions = os.getenv("VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV")
            if env_emissions not in (None, ""):
                try:
                    emissions_per_day = float(env_emissions)
                except Exception:
                    emissions_per_day = None

            env_diem = os.getenv("VVV_FV_DIEM_PER_DAY_PER_STAKED_VVV")
            if env_diem not in (None, ""):
                try:
                    diem_per_day = float(env_diem)
                except Exception:
                    diem_per_day = None

            if os.getenv("PYTEST_CURRENT_TEST") in (None, "") and (
                emissions_per_day is None or diem_per_day is None
            ):
                try:
                    md = self._market_provider()
                    metrics_fn = getattr(md, "vvv_metrics", None)
                    metrics = metrics_fn(ttl_s=60) if callable(metrics_fn) else None
                    if isinstance(metrics, dict):
                        if emissions_per_day is None:
                            raw = metrics.get("emissions_vvv_per_day_per_staked_vvv")
                            if raw is not None:
                                emissions_per_day = float(raw)
                        if diem_per_day is None:
                            raw = metrics.get("diem_per_day_per_staked_vvv")
                            if raw is not None:
                                diem_per_day = float(raw)
                except Exception:
                    pass

            if emissions_per_day is not None and env_emissions in (None, ""):
                try:
                    os.environ["VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV"] = str(
                        float(emissions_per_day)
                    )
                except Exception:
                    pass

            if diem_per_day is not None and env_diem in (None, ""):
                try:
                    os.environ["VVV_FV_DIEM_PER_DAY_PER_STAKED_VVV"] = str(
                        float(diem_per_day)
                    )
                except Exception:
                    pass

            fv = vvv_pricing.fair_value_per_vvv(
                vvv_price_usd=float(vvv_price_usd),
                locked_ratio=0.0,
                emissions_vvv_per_day_per_staked_vvv=emissions_per_day,
                diem_per_day_per_staked_vvv=diem_per_day,
            )
            if isinstance(fv, dict):
                vvv_fv_result = fv
                vvv_fair_value = fv.get("vvv_fair_value_usd")
        except Exception:
            vvv_fv_result = None
            vvv_fair_value = None

        vvv_discount_ratio = None
        try:
            if vvv_fair_value is not None and float(vvv_price_usd) > 0:
                vvv_discount_ratio = float(vvv_fair_value) / float(vvv_price_usd)
        except Exception:
            vvv_discount_ratio = None

        prefer_stake_discount_mult = float(self._vvv_fv_prefer_stake_discount_mult())
        prefer_stake_by_fv = False
        try:
            if (
                vvv_discount_ratio is not None
                and vvv_discount_ratio >= prefer_stake_discount_mult
            ):
                prefer_stake_by_fv = True
        except Exception:
            prefer_stake_by_fv = False

        recovery_meta = {
            "locked_ratio": float(locked_ratio),
            "cap": float(cap),
            "cap_bps": int(cap_bps),
            "target": float(target),
            "target_bps": int(target_bps),
            "converge_steps_total": int(steps_total),
            "converge_steps_done": int(steps_done),
            "converge_steps_remaining": int(steps_remaining),
            "recovery_plan": plan.as_dict() if plan is not None else None,
            "max_locked_units_trigger": int(max_locked_units_trigger),
            "max_locked_units_target": int(max_locked_units_target),
            "excess_locked_units": int(excess_locked_units),
            "unlocked_units": int(unlocked_units),
            "target_unlocked_units": int(target_unlocked_units),
            "need_unlock_units": int(need_unlock_units),
            "burn_units_target": int(burn_units_target),
            "burn_unlock_units": int(burn_unlock_units),
            "burn_probe": burn_probe,
            "stake_units_needed": int(stake_units_needed),
            "stake_units_target": int(stake_units_target),
            "vvv_fair_value": (
                float(vvv_fair_value) if vvv_fair_value is not None else None
            ),
            "vvv_discount_ratio": (
                float(vvv_discount_ratio) if vvv_discount_ratio is not None else None
            ),
            "prefer_stake_discount_mult": float(prefer_stake_discount_mult),
            "prefer_stake_by_fv": bool(prefer_stake_by_fv),
            "vvv_fv": vvv_fv_result,
        }
        rationale["capacity_recovery"] = recovery_meta

        # Rationale logging fields (stable top-level keys).
        rationale.setdefault("locked_ratio", float(locked_ratio))
        rationale.setdefault("ratio_cap", float(cap))
        rationale.setdefault("ratio_target", float(target))
        rationale["vvv_fair_value_usd"] = (
            float(vvv_fair_value) if vvv_fair_value is not None else None
        )
        if isinstance(vvv_fv_result, dict):
            rationale["vvv_fair_value_components"] = vvv_fv_result.get("components")
        else:
            rationale["vvv_fair_value_components"] = None

        # Compute hard caps from balances/risk policy.
        try:
            usdc_info = exec_ctx.balances.get("USDC") or {}
            usdc_units = int(usdc_info.get("units", 0) or 0)
            usdc_decimals = int(usdc_info.get("decimals", 6) or 6)
            available_usd = float(usdc_units) / float(10**usdc_decimals)
        except Exception:
            usdc_units = 0
            usdc_decimals = 6
            available_usd = 0.0

        min_trade_usd = float(self._recovery_min_trade_usd())
        max_trade_usd = self._recovery_max_trade_usd()
        max_steps = int(self._recovery_max_steps())

        budget_usd = float(available_usd)
        budget_units = int(usdc_units)
        if max_trade_usd is not None:
            budget_usd = min(float(budget_usd), float(max_trade_usd))
            try:
                budget_units_cap = int(float(max_trade_usd) * float(10**usdc_decimals))
            except Exception:
                budget_units_cap = int(usdc_units)
            budget_units = max(0, min(int(budget_units), int(budget_units_cap)))

        recovery_meta["recovery_min_trade_usd"] = float(min_trade_usd)
        recovery_meta["recovery_max_trade_usd"] = (
            float(max_trade_usd) if max_trade_usd else None
        )
        recovery_meta["recovery_budget_usd"] = float(budget_usd)
        recovery_meta["recovery_budget_units"] = int(budget_units)
        recovery_meta["recovery_max_steps"] = int(max_steps)

        if budget_usd < min_trade_usd:
            rationale["capacity_recovery_blocked_reason"] = "insufficient_usdc_balance"
            rationale["capacity_recovery_usdc_balance_usd"] = float(available_usd)
            rationale["capacity_recovery_budget_usd"] = float(budget_usd)
            rationale["capacity_recovery_min_trade_usd"] = float(min_trade_usd)
            rationale["capacity_recovery_max_trade_usd"] = (
                float(max_trade_usd) if max_trade_usd is not None else None
            )
            _log_recovery_campaign_attempt(
                selected_option="none",
                blocked_reason="insufficient_usdc_balance",
                trade_usd=None,
                stage="blocked",
            )
            return False

        recovery_meta["preferred_action"] = preferred_action
        recovery_meta["recovery_slippage_policy"] = {
            "base_cap_bps": (
                float(slippage_cap_bps_base_risk)
                if slippage_cap_bps_base_risk is not None
                else None
            ),
            "input_cap_bps": (
                float(slippage_cap_bps_base)
                if slippage_cap_bps_base is not None
                else None
            ),
            "max_slippage_bps": float(recovery_max_slippage_bps),
            "small_trade_usd": float(recovery_small_trade_usd),
            "price_sanity_max_rel_diff": float(recovery_price_sanity_max_rel_diff),
        }

        unlock_option: dict[str, Any] = {
            "eligible": False,
            "effective": False,
            "preview": None,
            "blocked_reason": None,
            "burn_units": 0,
            "unlock_units": 0,
            "usdc_in_units": None,
            "usdc_in_usd": None,
            "preview_slippage_bps": None,
            "preview_incoherent": None,
            "route_health": None,
            "pool_take_bps": None,
            "effective_price_usd": None,
            "price_sanity_rel_diff": None,
            "slippage_cap_bps": None,
            "slippage_applied": None,
            "slippage_applied_reason": None,
        }
        stake_option: dict[str, Any] = {
            "eligible": False,
            "effective": False,
            "preview": None,
            "blocked_reason": None,
            "stake_units_target": int(stake_units_target),
            "stake_units_expected_min_out": 0,
            "usdc_in_units": None,
            "usdc_in_usd": None,
            "route_health": None,
            "implied_price_usd": None,
            "price_sanity_rel_diff": None,
            "slippage_cap_bps": None,
            "slippage_applied": None,
            "slippage_applied_reason": None,
        }

        # Option A (unlock): Buy DIEM exact-out, burn to unlock sVVV.
        # Skip unlock evaluation if force_stake_only is True (DIEM routes are broken)
        burn_units_sized = 0
        if burn_units_target > 0 and not force_stake_only:
            burn_units_sized = int(burn_units_target)
            try:
                burn_units_sized = int(
                    self.risk.size_with_risk(
                        int(burn_units_sized),
                        float(market_price),
                        current_inventory_usd=current_inventory_usd,
                        utilization_ratio=utilization_ratio,
                        vol_bps=vol_bps,
                    )
                )
            except Exception:
                burn_units_sized = int(burn_units_sized)

            burn_fn = getattr(self.diem, "_can_burn_diem", None)
            if burn_units_sized > 0 and callable(burn_fn):
                for step in range(max(1, int(max_steps))):
                    candidate_burn_units = int(burn_units_sized) // (2**step)
                    if candidate_burn_units <= 0:
                        break

                    burn_probe_sized: dict[str, Any] | None = None
                    try:
                        burn_probe_sized = burn_fn(int(candidate_burn_units))
                    except TypeError:
                        burn_probe_sized = burn_fn(self.diem, int(candidate_burn_units))  # type: ignore[arg-type]
                    except Exception:
                        burn_probe_sized = None

                    if not (
                        isinstance(burn_probe_sized, dict)
                        and burn_probe_sized.get("can_burn", False)
                    ):
                        unlock_option["blocked_reason"] = (
                            str(
                                (burn_probe_sized or {}).get("reason")
                                or "burn_ineligible"
                            )
                            if isinstance(burn_probe_sized, dict)
                            else "burn_ineligible"
                        )
                        continue

                    try:
                        burn_unlock_units_sized = int(
                            burn_probe_sized.get("required_svvv") or 0
                        )
                    except Exception:
                        burn_unlock_units_sized = 0

                    # Preview USDC required to buy DIEM exact-out at this size.
                    preview = None
                    usdc_in_units = None
                    blocked_reason = None
                    preview_slippage_bps = None
                    preview_incoherent = None
                    preview_pool_take_bps = None
                    preview_effective_price = None
                    price_sanity_rel_diff = None

                    est_trade_usd = None
                    try:
                        if float(market_price) > 0 and int(scale) > 0:
                            est_trade_usd = (
                                float(candidate_burn_units)
                                / float(int(scale))
                                * float(market_price)
                            )
                    except Exception:
                        est_trade_usd = None
                    unlock_slip_cap_bps, unlock_slip_applied, unlock_slip_reason = (
                        _recovery_slippage_cap(estimated_trade_usd=est_trade_usd)
                    )
                    try:
                        buy_intent = ExecutionIntent(
                            side=TradeSide.BUY,
                            token_in="USDC",
                            token_out="DIEM",
                            amount_base_units=int(candidate_burn_units),
                            slippage_bps=int(unlock_slip_cap_bps),
                            pool_take_bps=pool_take_bps,
                            metadata={
                                "correlation_id": corr_id,
                                "decision": "capacity_recovery_buy_burn",
                                "recovery_step": int(step),
                                "diem_market_price_usd": float(market_price),
                            },
                        )
                        preview_res = self.diem.preview_trade(buy_intent)
                        preview = preview_res.as_dict()
                        try:
                            if preview_res.slippage_bps is not None:
                                preview_slippage_bps = float(preview_res.slippage_bps)
                        except Exception:
                            preview_slippage_bps = None
                        try:
                            if preview_res.pool_take_bps is not None:
                                preview_pool_take_bps = float(preview_res.pool_take_bps)
                        except Exception:
                            preview_pool_take_bps = None
                        try:
                            if preview_res.effective_price is not None:
                                preview_effective_price = float(
                                    preview_res.effective_price
                                )
                        except Exception:
                            preview_effective_price = None
                        try:
                            diag = getattr(preview_res, "diagnostics", None)
                            if isinstance(diag, dict):
                                preview_incoherent = bool(
                                    diag.get("coherence_incoherent_preview", False)
                                )
                        except Exception:
                            preview_incoherent = None
                        if preview_res.status == ExecutionStatus.SIMULATED:
                            if isinstance(preview_res.amount_in, int):
                                usdc_in_units = int(preview_res.amount_in)
                    except Exception as exc:
                        preview = {"status": "error", "error": str(exc)}
                        usdc_in_units = None

                    unlock_option["preview_slippage_bps"] = (
                        float(preview_slippage_bps)
                        if preview_slippage_bps is not None
                        else None
                    )
                    unlock_option["preview_incoherent"] = (
                        bool(preview_incoherent)
                        if preview_incoherent is not None
                        else None
                    )
                    unlock_option["pool_take_bps"] = (
                        float(preview_pool_take_bps)
                        if preview_pool_take_bps is not None
                        else None
                    )
                    unlock_option["effective_price_usd"] = (
                        float(preview_effective_price)
                        if preview_effective_price is not None
                        else None
                    )
                    unlock_option["slippage_cap_bps"] = float(unlock_slip_cap_bps)
                    unlock_option["slippage_applied"] = bool(unlock_slip_applied)
                    unlock_option["slippage_applied_reason"] = unlock_slip_reason

                    def _classify_unlock_preview_block_reason(
                        preview_payload: dict[str, Any] | None,
                    ) -> str:
                        if not isinstance(preview_payload, dict):
                            return "no_unlock_preview"
                        diag = preview_payload.get("diagnostics")
                        if isinstance(diag, dict):
                            if (
                                diag.get("failure_classification")
                                == "no_executable_quotes"
                            ):
                                return "no_executable_quotes"
                            agg_diag = diag.get("aggregator_diagnostics")
                            if isinstance(agg_diag, list):
                                saw_mode_unsupported = False
                                saw_revert = False
                                saw_no_pool = False
                                saw_zero = False
                                for entry in agg_diag:
                                    if not isinstance(entry, dict):
                                        continue
                                    status = (
                                        str(entry.get("status") or "").strip().lower()
                                    )
                                    reason = (
                                        str(entry.get("reason") or "").strip().lower()
                                    )
                                    if reason == "mode_unsupported":
                                        saw_mode_unsupported = True
                                    if status == "no_pool":
                                        saw_no_pool = True
                                    if status in {"zero_liquidity", "zero"}:
                                        saw_zero = True
                                    if status == "error" and entry.get("revert_reason"):
                                        saw_revert = True
                                if saw_revert:
                                    return "execution_revert"
                                if saw_no_pool:
                                    return "no_pool"
                                if saw_zero:
                                    return "zero_liquidity"
                                if saw_mode_unsupported:
                                    return "mode_unsupported"
                        return "no_unlock_preview"

                    if usdc_in_units is None or usdc_in_units <= 0:
                        blocked_reason = _classify_unlock_preview_block_reason(preview)
                        if blocked_reason in {
                            "no_pool",
                            "zero_liquidity",
                            "execution_revert",
                        }:
                            unlock_option["route_health"] = (
                                "revert"
                                if blocked_reason == "execution_revert"
                                else blocked_reason
                            )
                    else:
                        # Route health guardrail: classify from aggregator diagnostics and block
                        # structural failures even when a preview returns numbers.
                        try:
                            health = None
                            diag = (
                                preview.get("diagnostics")
                                if isinstance(preview, dict)
                                else None
                            )
                            agg_diag = (
                                diag.get("aggregator_diagnostics")
                                if isinstance(diag, dict)
                                else None
                            )
                            if isinstance(agg_diag, list):
                                saw_ok = False
                                saw_no_pool = False
                                saw_zero = False
                                saw_revert = False
                                for entry in agg_diag:
                                    if not isinstance(entry, dict):
                                        continue
                                    status = (
                                        str(entry.get("status") or "").strip().lower()
                                    )
                                    if status == "ok":
                                        saw_ok = True
                                    if status == "no_pool":
                                        saw_no_pool = True
                                    if status in {"zero_liquidity", "zero"}:
                                        saw_zero = True
                                    if status == "error" and entry.get("revert_reason"):
                                        saw_revert = True
                                if saw_revert:
                                    health = "revert"
                                elif saw_no_pool:
                                    health = "no_pool"
                                elif saw_zero:
                                    health = "zero_liquidity"
                                elif saw_ok:
                                    health = "healthy"
                            unlock_option["route_health"] = (
                                str(health) if health else None
                            )
                            if blocked_reason is None and health in {
                                "no_pool",
                                "zero_liquidity",
                                "revert",
                            }:
                                blocked_reason = f"unlock_route_{health}"
                        except Exception:
                            pass

                        # Coherence and slippage guardrails: treat pathological previews as blocked
                        # so capacity recovery can fall back to staking deterministically.
                        if bool(preview_incoherent):
                            blocked_reason = "incoherent_preview_mute"
                        else:
                            EXTREME_SLIPPAGE_THRESHOLD_BPS = 1000.0
                            try:
                                if preview_slippage_bps is not None and float(
                                    preview_slippage_bps
                                ) >= float(EXTREME_SLIPPAGE_THRESHOLD_BPS):
                                    blocked_reason = "extreme_slippage"
                                elif preview_slippage_bps is not None and float(
                                    preview_slippage_bps
                                ) > float(unlock_slip_cap_bps):
                                    blocked_reason = "slippage_exceeded_policy"
                            except Exception:
                                pass

                        # Pool-take guardrail: block when the preview indicates excessive pool take.
                        if blocked_reason is None and pool_take_bps is not None:
                            try:
                                if preview_pool_take_bps is not None and float(
                                    preview_pool_take_bps
                                ) > float(pool_take_bps):
                                    blocked_reason = "pool_take_exceeded_cap"
                            except Exception:
                                pass

                        # Price sanity: block when implied execution price is too far from reference.
                        if (
                            blocked_reason is None
                            and preview_effective_price is not None
                            and float(market_price) > 0
                        ):
                            try:
                                price_sanity_rel_diff = abs(
                                    float(preview_effective_price) / float(market_price)
                                    - 1.0
                                )
                                if float(price_sanity_rel_diff) > float(
                                    recovery_price_sanity_max_rel_diff
                                ):
                                    blocked_reason = "recovery_price_sanity_exceeded"
                            except Exception:
                                pass
                        unlock_option["price_sanity_rel_diff"] = (
                            float(price_sanity_rel_diff)
                            if price_sanity_rel_diff is not None
                            else None
                        )

                        # Ensure provider is enabled for execution (avoid preview/execution mismatch).
                        if blocked_reason is None:
                            try:
                                agg = getattr(self.diem, "aggregator", None)
                                allowed = getattr(
                                    agg, "_execution_provider_names", None
                                )
                                best_provider = None
                                if isinstance(preview, dict):
                                    best_provider = (
                                        (preview.get("diagnostics") or {}).get(
                                            "best_provider"
                                        )
                                        if isinstance(preview.get("diagnostics"), dict)
                                        else None
                                    )
                                if (
                                    best_provider
                                    and isinstance(allowed, list)
                                    and all(isinstance(p, str) for p in allowed)
                                ):
                                    allowed_l = {
                                        p.strip().lower() for p in allowed if p.strip()
                                    }
                                    if (
                                        str(best_provider).strip().lower()
                                        not in allowed_l
                                    ):
                                        blocked_reason = "unlock_provider_not_enabled"
                            except Exception:
                                pass

                    usdc_in_usd = None
                    if blocked_reason is None:
                        try:
                            usdc_in_usd = float(usdc_in_units) / float(
                                10**usdc_decimals
                            )
                        except Exception:
                            usdc_in_usd = None

                        if usdc_in_usd is not None and usdc_in_usd < min_trade_usd:
                            blocked_reason = "unlock_below_min_trade"
                        elif usdc_in_units > budget_units:
                            blocked_reason = "unlock_over_max_trade"

                    if blocked_reason is not None:
                        unlock_option["blocked_reason"] = blocked_reason
                        # If we're already below the min trade floor, smaller candidates will be too.
                        if blocked_reason == "unlock_below_min_trade":
                            break
                        continue

                    unlock_option.update(
                        {
                            "burn_units": int(candidate_burn_units),
                            "unlock_units": int(burn_unlock_units_sized),
                            "eligible": True,
                            "effective": bool(
                                max(0, int(locked_units) - int(burn_unlock_units_sized))
                                * 10_000
                                <= int(total_units) * int(target_bps)
                            ),
                            "preview": preview,
                            "burn_probe": burn_probe_sized,
                            "blocked_reason": None,
                            "usdc_in_units": int(usdc_in_units),
                            "usdc_in_usd": float(usdc_in_usd)
                            if usdc_in_usd is not None
                            else None,
                            "recovery_step": int(step),
                        }
                    )
                    break

        # Option B (stake): Buy VVV and stake to grow denominator.
        if stake_units_target > 0 and float(vvv_price_usd) > 0:
            try:
                vvv_decimals = int(os.getenv("VVV_DECIMALS", "18"))
            except Exception:
                vvv_decimals = 18

            # Risk cap by max stake budget.
            max_stake_units = 0
            try:
                max_stake_units = self.risk.max_stake_from_prices(
                    {"VVV": float(vvv_price_usd)},
                    current_staked_units=int(total_units),
                    vvv_decimals=int(vvv_decimals),
                )
            except Exception:
                max_stake_units = int(stake_units_target)
            desired_stake_units = max(
                0, min(int(stake_units_target), int(max_stake_units))
            )

            stake_est_trade_usd = None
            try:
                if float(vvv_price_usd) > 0 and int(vvv_decimals) >= 0:
                    stake_est_trade_usd = (
                        float(desired_stake_units)
                        / float(10 ** int(vvv_decimals))
                        * float(vvv_price_usd)
                    )
            except Exception:
                stake_est_trade_usd = None
            stake_slip_cap_bps, stake_slip_applied, stake_slip_reason = (
                _recovery_slippage_cap(estimated_trade_usd=stake_est_trade_usd)
            )
            stake_option["slippage_cap_bps"] = float(stake_slip_cap_bps)
            stake_option["slippage_applied"] = bool(stake_slip_applied)
            stake_option["slippage_applied_reason"] = stake_slip_reason

            try:
                slip_bps_int = int(stake_slip_cap_bps)
                if 0 <= slip_bps_int < 10_000 and desired_stake_units > 0:
                    desired_stake_units = self._ceil_div(
                        int(desired_stake_units) * 10_000, 10_000 - int(slip_bps_int)
                    )
            except Exception:
                pass

            stake_budget_units = None
            stake_budget_mode = None
            stake_route_meta: dict[str, Any] | None = None
            aggregator = getattr(self.diem, "aggregator", None)
            quote_token = os.getenv("QUOTE_TOKEN_ADDRESS") or os.getenv(
                "USDC_TOKEN_ADDRESS"
            )
            vvv_token = os.getenv("VVV_TOKEN_ADDRESS")
            stake_route_plan = None

            if aggregator is None or not quote_token or not vvv_token:
                stake_option["blocked_reason"] = "stake_missing_aggregator_or_tokens"
            else:
                try:
                    from libs.dex.routes import make_route

                    route = make_route([quote_token, vvv_token])
                except Exception:
                    route = None
                if route is None:
                    stake_option["blocked_reason"] = "stake_route_unavailable"
                elif desired_stake_units > 0:
                    stake_route_plan = route
                    allowed_exec = getattr(
                        aggregator, "_execution_provider_names", None
                    )
                    for step in range(max(1, int(max_steps))):
                        candidate_stake_units = int(desired_stake_units) // (2**step)
                        if candidate_stake_units <= 0:
                            break

                        stake_budget_units = None
                        stake_budget_mode = None
                        stake_route_meta = None

                        try:
                            est_usd = (
                                float(candidate_stake_units)
                                / float(10 ** int(vvv_decimals))
                            ) * float(vvv_price_usd)
                            stake_budget_units = int(est_usd * float(10**usdc_decimals))
                            stake_budget_mode = "price_estimate"
                        except Exception:
                            stake_budget_units = None
                            stake_budget_mode = None

                        if (
                            not isinstance(stake_budget_units, int)
                            or stake_budget_units <= 0
                        ):
                            stake_option["blocked_reason"] = "stake_budget_unavailable"
                            continue

                        if stake_budget_units > budget_units:
                            stake_option["blocked_reason"] = "stake_over_max_trade"
                            continue

                        try:
                            stake_budget_usd = float(stake_budget_units) / float(
                                10**usdc_decimals
                            )
                        except Exception:
                            stake_budget_usd = None

                        if stake_budget_usd is None or stake_budget_usd < min_trade_usd:
                            stake_option["blocked_reason"] = "stake_below_min_trade"
                            break

                        quote_in = None
                        try:
                            quote_in = aggregator.best_quote(
                                int(stake_budget_units),
                                route,
                                allowed_providers=allowed_exec,
                            )
                        except Exception:
                            quote_in = None

                        if quote_in is None or not getattr(
                            quote_in, "executable", True
                        ):
                            stake_option["blocked_reason"] = "stake_no_quote"
                            continue

                        try:
                            vvv_out_units = int(getattr(quote_in, "amount_out", 0) or 0)
                        except Exception:
                            vvv_out_units = 0
                        min_out_units = 0
                        try:
                            min_out_units = int(
                                int(vvv_out_units)
                                * (10_000 - int(stake_slip_cap_bps))
                                // 10_000
                            )
                        except Exception:
                            min_out_units = 0

                        # Price sanity: ensure implied execution price is not wildly off reference.
                        implied_price_usd = None
                        price_rel_diff = None
                        try:
                            vvv_tokens = float(vvv_out_units) / float(
                                10 ** int(vvv_decimals)
                            )
                            if stake_budget_usd is not None and vvv_tokens > 0:
                                implied_price_usd = float(stake_budget_usd) / float(
                                    vvv_tokens
                                )
                                if float(vvv_price_usd) > 0:
                                    price_rel_diff = abs(
                                        float(implied_price_usd) / float(vvv_price_usd)
                                        - 1.0
                                    )
                        except Exception:
                            implied_price_usd = None
                            price_rel_diff = None

                        stake_option["implied_price_usd"] = (
                            float(implied_price_usd)
                            if implied_price_usd is not None
                            else None
                        )
                        stake_option["price_sanity_rel_diff"] = (
                            float(price_rel_diff)
                            if price_rel_diff is not None
                            else None
                        )
                        if price_rel_diff is not None and float(price_rel_diff) > float(
                            recovery_price_sanity_max_rel_diff
                        ):
                            stake_option["blocked_reason"] = (
                                "recovery_price_sanity_exceeded"
                            )
                            continue

                        stake_option.update(
                            {
                                "preview": {
                                    "mode": stake_budget_mode,
                                    "budget_route": stake_route_meta,
                                    "provider": getattr(quote_in, "provider", None),
                                    "path": list(getattr(quote_in, "path", []) or []),
                                    "vvv_out_units": int(vvv_out_units),
                                    "vvv_min_out_units": int(min_out_units),
                                    "requested_stake_units": int(candidate_stake_units),
                                },
                                "usdc_in_units": int(stake_budget_units),
                                "usdc_in_usd": float(stake_budget_usd)
                                if stake_budget_usd is not None
                                else None,
                                "stake_units_expected_min_out": int(min_out_units),
                                "eligible": True,
                                "effective": bool(
                                    int(locked_units) * 10_000
                                    <= int(total_units + min_out_units)
                                    * int(target_bps)
                                )
                                if int(total_units + min_out_units) > 0
                                else False,
                                "blocked_reason": None,
                                "recovery_step": int(step),
                            }
                        )

                        # Route health guard (based on most recent aggregator diagnostics).
                        try:
                            diag_list = getattr(
                                aggregator, "_last_quote_diagnostics", None
                            )
                            route_diags = []
                            if (
                                isinstance(diag_list, list)
                                and stake_route_plan is not None
                            ):
                                tokens = (
                                    list(getattr(stake_route_plan, "tokens", []) or [])
                                    if stake_route_plan is not None
                                    else []
                                )
                                for entry in diag_list:
                                    if not isinstance(entry, dict):
                                        continue
                                    if entry.get("route") == tokens:
                                        route_diags.append(entry)
                            if stake_route_plan is not None:
                                stake_health = _classify_route_health(
                                    stake_route_plan, None, diagnostics=route_diags
                                )
                                stake_option["route_health"] = str(stake_health)
                                if stake_option.get("blocked_reason") is None and str(
                                    stake_health
                                ).strip().lower() in {
                                    "no_pool",
                                    "zero_liquidity",
                                    "revert",
                                }:
                                    stake_option["blocked_reason"] = (
                                        f"stake_route_{str(stake_health).strip().lower()}"
                                    )
                        except Exception:
                            pass
                        break

        recovery_meta["unlock_option"] = unlock_option
        recovery_meta["stake_option"] = stake_option

        blocked_reason = unlock_option.get("blocked_reason")
        route_health = unlock_option.get("route_health")
        fallback_threshold = max(
            0, int(self._recovery_unlock_revert_fallback_threshold())
        )
        route_health_norm = (
            str(route_health).strip().lower() if route_health is not None else ""
        )
        blocked_reason_norm = (
            str(blocked_reason).strip().lower() if blocked_reason is not None else ""
        )
        if fallback_threshold > 0:
            revert_detected = (
                blocked_reason_norm in _UNLOCK_REVERT_BLOCKED_REASONS
                or route_health_norm == "revert"
            )
            if revert_detected:
                self._capacity_recovery_unlock_revert_streak = min(
                    int(self._capacity_recovery_unlock_revert_streak) + 1,
                    fallback_threshold,
                )
            else:
                self._capacity_recovery_unlock_revert_streak = 0
        else:
            self._capacity_recovery_unlock_revert_streak = 0
        recovery_meta["unlock_fallback_streak"] = int(
            self._capacity_recovery_unlock_revert_streak
        )
        recovery_meta["unlock_fallback_threshold"] = (
            int(fallback_threshold) if fallback_threshold > 0 else None
        )

        stake_converges_within_steps = False
        stake_projected_locked_ratio_bps: int | None = None
        try:
            if (
                stake_option.get("eligible")
                and stake_option.get("blocked_reason") is None
                and int(steps_remaining) > 0
            ):
                min_out_units = int(
                    stake_option.get("stake_units_expected_min_out") or 0
                )
                projected_total_units = int(total_units) + int(min_out_units) * int(
                    steps_remaining
                )
                if projected_total_units > 0 and min_out_units > 0:
                    stake_converges_within_steps = bool(
                        int(locked_units) * 10_000
                        <= int(projected_total_units) * int(target_bps)
                    )
                    stake_projected_locked_ratio_bps = round(
                        (float(locked_units) * 10_000.0) / float(projected_total_units)
                    )
        except Exception:
            stake_converges_within_steps = False
            stake_projected_locked_ratio_bps = None

        recovery_meta["stake_converges_within_steps"] = bool(
            stake_converges_within_steps
        )
        recovery_meta["stake_projected_locked_ratio_bps"] = (
            int(stake_projected_locked_ratio_bps)
            if stake_projected_locked_ratio_bps is not None
            else None
        )

        burn_premium_mult = float(self._recovery_burn_premium_mult())
        econ_diem_price_usd: float | None = None
        econ_vvv_price_usd: float | None = None
        econ_mint_rate: float | None = None
        econ_usd_per_svvv_unlocked = None
        econ_usd_per_svvv_added = None
        econ_prefer: str | None = None
        econ_reason: str | None = None

        try:
            econ_diem_price_usd = float(market_price)
            econ_vvv_price_usd = float(vvv_price_usd)
            econ_mint_rate = float(mint_rate)

            if (
                econ_diem_price_usd is not None
                and math.isfinite(econ_diem_price_usd)
                and econ_diem_price_usd > 0
                and econ_vvv_price_usd is not None
                and math.isfinite(econ_vvv_price_usd)
                and econ_vvv_price_usd > 0
                and econ_mint_rate is not None
                and math.isfinite(econ_mint_rate)
                and econ_mint_rate > 0
            ):
                econ_usd_per_svvv_unlocked = float(econ_diem_price_usd) / float(
                    econ_mint_rate
                )
                econ_usd_per_svvv_added = float(econ_vvv_price_usd)
                burn_threshold = float(econ_usd_per_svvv_added) * float(
                    burn_premium_mult
                )
                econ_prefer = (
                    "stake"
                    if float(econ_usd_per_svvv_unlocked) > float(burn_threshold)
                    else "unlock"
                )
            else:
                econ_reason = "missing_price_or_mint_rate"
        except Exception:
            econ_reason = "economics_compute_error"

        recovery_meta["recovery_economics"] = {
            "burn_premium_mult": float(burn_premium_mult),
            "diem_price_usd": (
                float(econ_diem_price_usd)
                if econ_diem_price_usd is not None
                and math.isfinite(econ_diem_price_usd)
                else None
            ),
            "vvv_price_usd": (
                float(econ_vvv_price_usd)
                if econ_vvv_price_usd is not None and math.isfinite(econ_vvv_price_usd)
                else None
            ),
            "mint_rate_svvv_per_diem": (
                float(econ_mint_rate)
                if econ_mint_rate is not None and math.isfinite(econ_mint_rate)
                else None
            ),
            "usd_per_svvv_unlocked": (
                float(econ_usd_per_svvv_unlocked)
                if econ_usd_per_svvv_unlocked is not None
                else None
            ),
            "usd_per_svvv_added": (
                float(econ_usd_per_svvv_added)
                if econ_usd_per_svvv_added is not None
                else None
            ),
            "prefer": econ_prefer,
            "reason": econ_reason,
        }

        choose_stake = False
        choose_unlock = False

        def _log_capacity_recovery_blocked(
            *,
            unlock: dict[str, Any],
            stake: dict[str, Any],
        ) -> None:
            payload = {
                "event": "capacity_recovery_blocked",
                "locked_ratio": float(locked_ratio_f)
                if locked_ratio_f is not None
                else None,
                "ratio_cap": float(cap),
                "ratio_target": float(target),
                "simulate": bool(simulate),
                "correlation_id": corr_id,
                "unlock": {
                    "eligible": bool(unlock.get("eligible")),
                    "blocked_reason": unlock.get("blocked_reason"),
                    "usdc_in_usd": unlock.get("usdc_in_usd"),
                    "preview_slippage_bps": unlock.get("preview_slippage_bps"),
                    "preview_incoherent": unlock.get("preview_incoherent"),
                    "route_health": unlock.get("route_health"),
                },
                "stake": {
                    "eligible": bool(stake.get("eligible")),
                    "blocked_reason": stake.get("blocked_reason"),
                    "usdc_in_usd": stake.get("usdc_in_usd"),
                    "route_health": stake.get("route_health"),
                },
            }
            try:
                logger.info("capacity_recovery_blocked", extra=payload)
            except Exception:
                pass

        stake_ready = bool(
            stake_option.get("eligible") and stake_option.get("blocked_reason") is None
        )
        unlock_ready = bool(
            unlock_option.get("eligible")
            and unlock_option.get("blocked_reason") is None
        )

        # Preferred action override (applies in both simulate and live).
        if preferred_action in {"stake", "burn"}:
            if preferred_action == "stake":
                if stake_ready and (stake_converges_within_steps or not unlock_ready):
                    choose_stake = True
                elif unlock_ready:
                    choose_unlock = True
                elif stake_ready:
                    choose_stake = True
            elif preferred_action == "burn":
                if unlock_ready:
                    choose_unlock = True
                elif stake_ready:
                    choose_stake = True
        elif not simulate:
            # Deterministic selection in live mode:
            # - When a recovery campaign is active, keep the action choice simple so
            #   fair-value preferences cannot prevent convergence to the target.
            if stake_ready:
                choose_stake = True
            elif unlock_ready:
                choose_unlock = True
            else:
                rationale["decision"] = "hold"
                rationale["reason"] = "capacity_recovery_blocked"
                rationale["capacity_recovery_blocked_reason"] = (
                    "capacity_recovery_blocked"
                )
                rationale["capacity_recovery_blocked"] = {
                    "unlock": {
                        "eligible": bool(unlock_option.get("eligible")),
                        "blocked_reason": unlock_option.get("blocked_reason"),
                    },
                    "stake": {
                        "eligible": bool(stake_option.get("eligible")),
                        "blocked_reason": stake_option.get("blocked_reason"),
                        "route_health": stake_option.get("route_health"),
                    },
                }
                _log_capacity_recovery_blocked(unlock=unlock_option, stake=stake_option)
                return False
        else:
            # Selection rule:
            # - Prefer stake when VVV is discounted to intrinsic FV by configured margin.
            # - Prefer stake when buy+burn is too expensive per sVVV unlocked vs buy+stake.
            # - Prefer stake when unlock path is blocked by liquidity/min-trade.
            # - Otherwise prefer the cheaper effective option.
            unlock_blocked_liquidity = unlock_option.get("blocked_reason") in {
                "no_unlock_preview",
                "unlock_provider_not_enabled",
                "unlock_below_min_trade",
                "unlock_over_max_trade",
            }

            if (stake_option.get("eligible") and prefer_stake_by_fv) or (
                econ_prefer == "stake" and stake_option.get("eligible")
            ):
                choose_stake = True
            elif econ_prefer == "unlock" and unlock_option.get("eligible"):
                choose_unlock = True
            elif econ_prefer is None:
                # Missing economics inputs: avoid buy+burn in live mode and default to staking
                # when possible.
                if stake_option.get("eligible"):
                    choose_stake = True
                elif not simulate and unlock_option.get("eligible"):
                    rationale["capacity_recovery_blocked_reason"] = (
                        "missing_capacity_recovery_economics"
                    )
                    _log_recovery_campaign_attempt(
                        selected_option="unlock",
                        blocked_reason="missing_capacity_recovery_economics",
                        trade_usd=(
                            float(unlock_option.get("usdc_in_usd"))
                            if unlock_option.get("usdc_in_usd") is not None
                            else None
                        ),
                        stage="blocked",
                    )
                    return False

            if not choose_stake and not choose_unlock:
                if stake_option.get("eligible") and (
                    (not unlock_option.get("eligible")) and unlock_blocked_liquidity
                ):
                    choose_stake = True
                else:
                    unlock_ok = bool(unlock_option.get("eligible"))
                    stake_ok = bool(stake_option.get("eligible"))

                    candidates: list[tuple[str, float, bool]] = []
                    if unlock_ok and unlock_option.get("usdc_in_usd") is not None:
                        candidates.append(
                            (
                                "unlock",
                                float(unlock_option["usdc_in_usd"]),
                                bool(unlock_option.get("effective")),
                            )
                        )
                    if stake_ok and stake_option.get("usdc_in_usd") is not None:
                        candidates.append(
                            (
                                "stake",
                                float(stake_option["usdc_in_usd"]),
                                bool(stake_option.get("effective")),
                            )
                        )

                    if candidates:
                        effective = [c for c in candidates if c[2]]
                        if effective:
                            candidates = effective
                        candidates.sort(key=lambda c: c[1])
                        chosen = candidates[0][0]
                        choose_unlock = chosen == "unlock"
                        choose_stake = chosen == "stake"

        fallback_reason: str | None = None
        if blocked_reason_norm in _UNLOCK_FALLBACK_BLOCKED_REASONS:
            fallback_reason = blocked_reason_norm
        elif (
            fallback_threshold > 0
            and self._capacity_recovery_unlock_revert_streak >= fallback_threshold
        ):
            fallback_reason = "unlock_route_revert_streak"
        if fallback_reason:
            rationale["capacity_recovery_unlock_fallback_reason"] = fallback_reason
            rationale["capacity_recovery_unlock_fallback_blocked_reason"] = (
                blocked_reason_norm if blocked_reason_norm else None
            )
            rationale["capacity_recovery_unlock_route_health"] = (
                route_health_norm if route_health_norm else None
            )
            rationale["capacity_recovery_unlock_revert_streak"] = int(
                self._capacity_recovery_unlock_revert_streak
            )
            if stake_option.get("eligible"):
                choose_stake = True
                choose_unlock = False
            else:
                choose_unlock = False
            try:
                logger.info(
                    "capacity_recovery_unlock_fallback",
                    extra={
                        "event": "capacity_recovery_unlock_fallback",
                        "fallback_reason": fallback_reason,
                        "blocked_reason": (
                            blocked_reason_norm if blocked_reason_norm else None
                        ),
                        "route_health": (
                            route_health_norm if route_health_norm else None
                        ),
                        "revert_streak": int(
                            self._capacity_recovery_unlock_revert_streak
                        ),
                        "stake_eligible": bool(stake_option.get("eligible")),
                        "simulate": bool(simulate),
                        "correlation_id": corr_id,
                    },
                )
            except Exception:
                pass

        if preferred_action == "stake" and choose_stake and stake_ready:
            if not bool(stake_converges_within_steps) and unlock_ready:
                choose_stake = False
                choose_unlock = True

        if not simulate and not choose_stake and not choose_unlock:
            rationale["decision"] = "hold"
            rationale["reason"] = "capacity_recovery_blocked"
            rationale["capacity_recovery_blocked_reason"] = "capacity_recovery_blocked"
            picked = stake_option if preferred_action == "stake" else unlock_option
            if picked.get("slippage_cap_bps") is None:
                picked = (
                    unlock_option
                    if unlock_option.get("slippage_cap_bps") is not None
                    else stake_option
                )
            try:
                rationale["recovery_slippage_cap_bps"] = float(
                    picked.get("slippage_cap_bps") or slippage_cap_bps
                )
            except Exception:
                rationale["recovery_slippage_cap_bps"] = None
            rationale["recovery_slippage_applied"] = bool(
                picked.get("slippage_applied")
            )
            rationale["recovery_slippage_applied_reason"] = (
                str(picked.get("slippage_applied_reason"))
                if picked.get("slippage_applied_reason") is not None
                else None
            )
            rationale["capacity_recovery_blocked"] = {
                "unlock": {
                    "eligible": bool(unlock_option.get("eligible")),
                    "blocked_reason": unlock_option.get("blocked_reason"),
                },
                "stake": {
                    "eligible": bool(stake_option.get("eligible")),
                    "blocked_reason": stake_option.get("blocked_reason"),
                    "route_health": stake_option.get("route_health"),
                },
            }
            _log_capacity_recovery_blocked(unlock=unlock_option, stake=stake_option)
            return False

        if choose_stake and stake_option.get("eligible"):
            bypass_interval = bool(self._recovery_bypass_interval())
            pacing_check = self._pacing_check(
                action="capacity_recovery",
                bypass_interval=bypass_interval,
            )
            rationale["capacity_recovery_pacing"] = pacing_check
            if not bool(pacing_check.get("ok", False)) and not simulate:
                blocked_reason = f"pacing_{pacing_check.get('reason') or 'blocked'}"
                rationale["capacity_recovery_blocked_reason"] = blocked_reason
                rationale["recovery_slippage_cap_bps"] = float(
                    stake_option.get("slippage_cap_bps") or slippage_cap_bps
                )
                rationale["recovery_slippage_applied"] = bool(
                    stake_option.get("slippage_applied")
                )
                rationale["recovery_slippage_applied_reason"] = (
                    str(stake_option.get("slippage_applied_reason"))
                    if stake_option.get("slippage_applied_reason") is not None
                    else None
                )
                _log_recovery_campaign_attempt(
                    selected_option="stake",
                    blocked_reason=str(blocked_reason),
                    trade_usd=(
                        float(stake_option.get("usdc_in_usd"))
                        if stake_option.get("usdc_in_usd") is not None
                        else None
                    ),
                    stage="blocked",
                )
                return False

            rationale.update(
                {
                    "decision": "capacity_recovery_stake",
                    "reason": "locked_ratio_exceeds_cap",
                    "recovery_action": "buy_vvv_and_stake",
                    "recovery_usdc_in_units": int(
                        stake_option.get("usdc_in_units") or 0
                    ),
                    "recovery_stake_units_target": int(stake_units_target),
                    "recovery_units": {
                        "usdc_in_units": int(stake_option.get("usdc_in_units") or 0),
                        "vvv_min_out_units": int(
                            stake_option.get("stake_units_expected_min_out") or 0
                        ),
                    },
                }
            )
            rationale["recovery_slippage_cap_bps"] = float(
                stake_option.get("slippage_cap_bps") or slippage_cap_bps
            )
            rationale["recovery_slippage_applied"] = bool(
                stake_option.get("slippage_applied")
            )
            rationale["recovery_slippage_applied_reason"] = (
                str(stake_option.get("slippage_applied_reason"))
                if stake_option.get("slippage_applied_reason") is not None
                else None
            )
            if not simulate:
                try:
                    now_ts = (pacing_check.get("pacing") or {}).get("now_ts")
                    self._pacing_record_action(
                        action="capacity_recovery",
                        now_ts=float(now_ts) if now_ts is not None else None,
                    )
                except Exception:
                    self._pacing_record_action(action="capacity_recovery")
            _log_recovery_campaign_attempt(
                selected_option="stake",
                blocked_reason=None,
                trade_usd=(
                    float(stake_option.get("usdc_in_usd"))
                    if stake_option.get("usdc_in_usd") is not None
                    else None
                ),
                stage="execute",
            )
            usdc_amount_units = int(stake_option.get("usdc_in_units") or 0)
            stake_slippage_bps = int(
                float(stake_option.get("slippage_cap_bps") or slippage_cap_bps)
            )
            if simulate:
                pending = {
                    "kind": "stake",
                    "decision": "capacity_recovery_stake",
                    "usdc_amount_units": int(usdc_amount_units),
                    "slippage_bps": int(stake_slippage_bps),
                    "corr_id": corr_id,
                    "ts": float(time.time()),
                }
                self._pending_recovery_action = pending
                rationale["pending_recovery_action"] = dict(pending)
                rationale["execution"] = {
                    "status": "planned",
                    "action": "buy_vvv_and_stake",
                    "simulate": True,
                    "usdc_in_units": int(usdc_amount_units),
                    "slippage_bps": int(stake_slippage_bps),
                    "correlation_id": corr_id,
                }
                return True

            exec_res = self._execute_capacity_recovery_buy_vvv_and_stake(
                usdc_amount_units=int(usdc_amount_units),
                slippage_bps=int(stake_slippage_bps),
                corr_id=corr_id,
                simulate=False,
            )
            rationale["execution"] = exec_res
            if isinstance(exec_res, dict) and exec_res.get("status") in {
                "submitted",
                "simulated",
            }:
                if not simulate:
                    try:
                        plan = self._recovery_plan
                        if plan is not None:
                            plan.steps_done = min(
                                int(plan.steps_total), int(plan.steps_done) + 1
                            )
                            plan.last_action_ts = float(time.time())
                    except Exception:
                        pass
                return True
            rationale["capacity_recovery_failed"] = True
            try:
                status = (
                    str(exec_res.get("status") or "unknown")
                    if isinstance(exec_res, dict)
                    else "unknown"
                )
                error = (
                    str(exec_res.get("error") or "")
                    if isinstance(exec_res, dict)
                    else str(exec_res)
                )
                _metrics_inc(
                    "arbi_diem_capacity_recovery_failures_total",
                    labels={
                        "decision": "capacity_recovery_stake",
                        "status": status,
                        "simulate": "true" if simulate else "false",
                    },
                )
                logger.error(
                    "Capacity recovery execution failed: decision=%s status=%s error=%s corr_id=%s",
                    "capacity_recovery_stake",
                    status,
                    error,
                    corr_id,
                    extra={
                        "agent": "arbi_diem",
                        "action": "capacity_recovery",
                        "decision": "capacity_recovery_stake",
                        "status": status,
                        "error": error,
                        "simulate": bool(simulate),
                        "correlation_id": corr_id,
                        "locked_ratio": float(locked_ratio),
                        "ratio_cap": float(cap),
                        "ratio_target": float(target),
                    },
                )
            except Exception:
                pass
            return False

        if choose_unlock and unlock_option.get("eligible"):
            bypass_interval = bool(self._recovery_bypass_interval())
            pacing_check = self._pacing_check(
                action="capacity_recovery",
                bypass_interval=bypass_interval,
            )
            rationale["capacity_recovery_pacing"] = pacing_check
            if not bool(pacing_check.get("ok", False)) and not simulate:
                blocked_reason = f"pacing_{pacing_check.get('reason') or 'blocked'}"
                rationale["capacity_recovery_blocked_reason"] = blocked_reason
                rationale["recovery_slippage_cap_bps"] = float(
                    unlock_option.get("slippage_cap_bps") or slippage_cap_bps
                )
                rationale["recovery_slippage_applied"] = bool(
                    unlock_option.get("slippage_applied")
                )
                rationale["recovery_slippage_applied_reason"] = (
                    str(unlock_option.get("slippage_applied_reason"))
                    if unlock_option.get("slippage_applied_reason") is not None
                    else None
                )
                _log_recovery_campaign_attempt(
                    selected_option="unlock",
                    blocked_reason=str(blocked_reason),
                    trade_usd=(
                        float(unlock_option.get("usdc_in_usd"))
                        if unlock_option.get("usdc_in_usd") is not None
                        else None
                    ),
                    stage="blocked",
                )
                return False

            rationale.update(
                {
                    "decision": "capacity_recovery_buy_burn",
                    "reason": "locked_ratio_exceeds_cap",
                    "recovery_action": "buy_diem_then_burn_unlock",
                    "recovery_burn_units_target": int(
                        unlock_option.get("burn_units") or 0
                    ),
                    "recovery_mint_rate_units_per_diem_unit": mint_rate_units,
                    "recovery_units": {
                        "diem_burn_units": int(unlock_option.get("burn_units") or 0),
                        "unlock_svvv_units": int(
                            unlock_option.get("unlock_units") or 0
                        ),
                        "usdc_in_units": int(unlock_option.get("usdc_in_units") or 0),
                    },
                }
            )
            rationale["recovery_slippage_cap_bps"] = float(
                unlock_option.get("slippage_cap_bps") or slippage_cap_bps
            )
            rationale["recovery_slippage_applied"] = bool(
                unlock_option.get("slippage_applied")
            )
            rationale["recovery_slippage_applied_reason"] = (
                str(unlock_option.get("slippage_applied_reason"))
                if unlock_option.get("slippage_applied_reason") is not None
                else None
            )
            if not simulate:
                try:
                    now_ts = (pacing_check.get("pacing") or {}).get("now_ts")
                    self._pacing_record_action(
                        action="capacity_recovery",
                        now_ts=float(now_ts) if now_ts is not None else None,
                    )
                except Exception:
                    self._pacing_record_action(action="capacity_recovery")
            _log_recovery_campaign_attempt(
                selected_option="unlock",
                blocked_reason=None,
                trade_usd=(
                    float(unlock_option.get("usdc_in_usd"))
                    if unlock_option.get("usdc_in_usd") is not None
                    else None
                ),
                stage="execute",
            )
            burn_units = int(unlock_option.get("burn_units") or 0)
            unlock_slippage_bps = int(
                float(unlock_option.get("slippage_cap_bps") or slippage_cap_bps)
            )
            if simulate:
                pending = {
                    "kind": "unlock",
                    "decision": "capacity_recovery_buy_burn",
                    "diem_amount_units": int(burn_units),
                    "slippage_bps": int(unlock_slippage_bps),
                    "pool_take_bps": pool_take_bps,
                    "portfolio_snapshot": exec_ctx.snapshot,
                    "corr_id": corr_id,
                    "ts": float(time.time()),
                }
                self._pending_recovery_action = pending
                try:
                    rationale["pending_recovery_action"] = {
                        k: v for k, v in pending.items() if k != "portfolio_snapshot"
                    }
                except Exception:
                    rationale["pending_recovery_action"] = {
                        "kind": "unlock",
                        "diem_amount_units": int(burn_units),
                    }
                rationale["execution"] = {
                    "status": "planned",
                    "action": "buy_diem_then_burn_unlock",
                    "simulate": True,
                    "diem_burn_units": int(burn_units),
                    "slippage_bps": int(unlock_slippage_bps),
                    "pool_take_bps": pool_take_bps,
                    "correlation_id": corr_id,
                }
                return True

            exec_res = self._execute_capacity_recovery_buy_burn(
                diem_amount_units=int(burn_units),
                slippage_bps=int(unlock_slippage_bps),
                pool_take_bps=pool_take_bps,
                corr_id=corr_id,
                simulate=False,
                portfolio_snapshot=exec_ctx.snapshot,
            )
            rationale["execution"] = exec_res
            if isinstance(exec_res, dict) and exec_res.get("status") in {
                ExecutionStatus.SIMULATED.value,
                ExecutionStatus.SUBMITTED.value,
                ExecutionStatus.CONFIRMED.value,
                "simulated",
                "submitted",
                "confirmed",
            }:
                if not simulate:
                    try:
                        plan = self._recovery_plan
                        if plan is not None:
                            plan.steps_done = min(
                                int(plan.steps_total), int(plan.steps_done) + 1
                            )
                            plan.last_action_ts = float(time.time())
                    except Exception:
                        pass
                return True
            rationale["capacity_recovery_failed"] = True
            try:
                status = (
                    str(exec_res.get("status") or "unknown")
                    if isinstance(exec_res, dict)
                    else "unknown"
                )
                error = (
                    str(exec_res.get("error") or "")
                    if isinstance(exec_res, dict)
                    else str(exec_res)
                )
                _metrics_inc(
                    "arbi_diem_capacity_recovery_failures_total",
                    labels={
                        "decision": "capacity_recovery_buy_burn",
                        "status": status,
                        "simulate": "true" if simulate else "false",
                    },
                )
                logger.error(
                    "Capacity recovery execution failed: decision=%s status=%s error=%s corr_id=%s",
                    "capacity_recovery_buy_burn",
                    status,
                    error,
                    corr_id,
                    extra={
                        "agent": "arbi_diem",
                        "action": "capacity_recovery",
                        "decision": "capacity_recovery_buy_burn",
                        "status": status,
                        "error": error,
                        "simulate": bool(simulate),
                        "correlation_id": corr_id,
                        "locked_ratio": float(locked_ratio),
                        "ratio_cap": float(cap),
                        "ratio_target": float(target),
                    },
                )
            except Exception:
                pass
            return False

        if not unlock_option.get("eligible") and not stake_option.get("eligible"):
            rationale["capacity_recovery_blocked_reason"] = "no_recovery_option"
            _log_recovery_campaign_attempt(
                selected_option="none",
                blocked_reason="no_recovery_option",
                trade_usd=None,
                stage="blocked",
            )
        return False

    def _execute_pending_recovery(
        self,
        *,
        corr_id: str | None,
        simulate: bool,
    ) -> bool:
        pending = getattr(self, "_pending_recovery_action", None)
        if not isinstance(pending, dict) or not pending:
            return False

        pending_kind = str(pending.get("kind") or "").strip().lower()
        pending_decision = str(pending.get("decision") or "").strip()

        rationale = getattr(self, "_last_rationale", None)
        if not isinstance(rationale, dict):
            rationale = {"decision": pending_decision or "capacity_recovery_pending"}
        if pending_decision:
            rationale["decision"] = pending_decision

        bypass_interval = bool(self._recovery_bypass_interval())
        pacing_check = self._pacing_check(
            action="capacity_recovery",
            bypass_interval=bypass_interval,
        )
        rationale["capacity_recovery_pacing"] = pacing_check
        if not bool(pacing_check.get("ok", False)) and not simulate:
            rationale["capacity_recovery_blocked_reason"] = (
                f"pacing_{pacing_check.get('reason') or 'blocked'}"
            )
            self._last_rationale = rationale
            return False

        if not simulate:
            try:
                now_ts = (pacing_check.get("pacing") or {}).get("now_ts")
                self._pacing_record_action(
                    action="capacity_recovery",
                    now_ts=float(now_ts) if now_ts is not None else None,
                )
            except Exception:
                self._pacing_record_action(action="capacity_recovery")

        exec_corr_id = corr_id if corr_id is not None else pending.get("corr_id")
        exec_res: dict[str, Any]

        if pending_kind == "stake":
            exec_res = self._execute_capacity_recovery_buy_vvv_and_stake(
                usdc_amount_units=int(pending.get("usdc_amount_units") or 0),
                slippage_bps=int(pending.get("slippage_bps") or 0),
                corr_id=str(exec_corr_id) if exec_corr_id is not None else None,
                simulate=bool(simulate),
            )
        elif pending_kind == "unlock":
            pool_take_bps = pending.get("pool_take_bps")
            try:
                pool_take_bps = (
                    int(pool_take_bps) if pool_take_bps is not None else None
                )
            except Exception:
                pool_take_bps = None
            exec_res = self._execute_capacity_recovery_buy_burn(
                diem_amount_units=int(pending.get("diem_amount_units") or 0),
                slippage_bps=int(pending.get("slippage_bps") or 0),
                pool_take_bps=pool_take_bps,
                corr_id=str(exec_corr_id) if exec_corr_id is not None else None,
                simulate=bool(simulate),
                portfolio_snapshot=pending.get("portfolio_snapshot"),
            )
        else:
            exec_res = {
                "status": "error",
                "error": f"unknown_pending_recovery_kind:{pending_kind}",
            }

        rationale["execution"] = exec_res
        rationale["pending_recovery_executed"] = True
        try:
            rationale["pending_recovery_action"] = {
                k: v for k, v in pending.items() if k != "portfolio_snapshot"
            }
        except Exception:
            rationale["pending_recovery_action"] = {"kind": pending_kind}
        self._last_rationale = rationale

        status = exec_res.get("status") if isinstance(exec_res, dict) else None
        ok_statuses = {
            ExecutionStatus.SIMULATED.value,
            ExecutionStatus.SUBMITTED.value,
            ExecutionStatus.CONFIRMED.value,
            "simulated",
            "submitted",
            "confirmed",
        }
        executed = isinstance(status, str) and status in ok_statuses
        if executed and not simulate:
            try:
                plan = self._recovery_plan
                if plan is not None:
                    plan.steps_done = min(
                        int(plan.steps_total), int(plan.steps_done) + 1
                    )
                    plan.last_action_ts = float(time.time())
            except Exception:
                pass
        return bool(executed)

    def _maybe_liquidate_unlocked_diem(
        self,
        *,
        wallet_diem_units: int,
        market_price: float,
        fair_value: float,
        corr_id: str | None,
        simulate: bool,
    ) -> dict | None:
        """Attempt to sell non-staked DIEM inventory when burning is impossible.

        Returns a rationale dict when liquidation logic runs, with `executed` flag.
        Returns None when liquidation is disabled or not applicable.
        """

        flag = os.getenv("ARBI_DIEM_INVENTORY_LIQUIDATE_ENABLE", "0").strip().lower()
        if flag not in {"1", "true", "yes", "on"}:
            return None

        if wallet_diem_units <= 0:
            return None

        if fair_value <= 0:
            rationale = {
                "decision": "hold",
                "reason": "invalid_fair_value",
                "wallet_diem_units": int(wallet_diem_units),
                "executed": False,
                "action": "inventory_liquidate",
            }
            self._last_rationale = rationale
            return rationale

        try:
            floor_mult = float(
                os.getenv("ARBI_DIEM_INVENTORY_LIQUIDATE_FLOOR_MULT", "0.95") or 0.95
            )
        except Exception:
            floor_mult = 0.95

        price_floor = float(fair_value) * max(0.0, floor_mult)

        rationale = {
            "decision": "hold",
            "reason": None,
            "wallet_diem_units": int(wallet_diem_units),
            "price_floor": price_floor,
            "market_price": float(market_price),
            "fair_value": float(fair_value),
            "executed": False,
            "action": "inventory_liquidate",
        }

        if market_price <= 0:
            rationale.update({"reason": "missing_market_price"})
            self._last_rationale = rationale
            return rationale

        if market_price < price_floor:
            rationale.update({"reason": "below_price_floor"})
            self._last_rationale = rationale
            return rationale

        try:
            slippage_cap_bps = float(self.risk.slippage_bps_cap)
        except Exception:
            slippage_cap_bps = 100.0
        pool_take_bps: int | None = None
        try:
            pt_raw = getattr(self.risk, "pool_take_bps_cap", None)
            if pt_raw is not None:
                pool_take_bps = int(pt_raw)
                if pool_take_bps <= 0:
                    pool_take_bps = None
        except Exception:
            pool_take_bps = None

        try:
            sell_intent = ExecutionIntent(
                side=TradeSide.SELL,
                token_in="DIEM",
                token_out="USDC",
                amount_base_units=int(wallet_diem_units),
                slippage_bps=int(slippage_cap_bps),
                pool_take_bps=pool_take_bps,
                metadata={
                    "correlation_id": corr_id,
                    "decision": "inventory_liquidate",
                    "diem_market_price_usd": float(market_price),
                },
            )
        except Exception as exc:
            rationale.update({"reason": "intent_error", "error": str(exc)})
            self._last_rationale = rationale
            return rationale

        preview_result = None
        try:
            preview_result = self.diem.preview_trade(sell_intent)
            rationale["execution_preview"] = preview_result.as_dict()
            if preview_result.slippage_bps is not None:
                rationale["slippage_bps"] = float(preview_result.slippage_bps)
            if preview_result.effective_price is not None:
                rationale["exec_price_preview"] = float(preview_result.effective_price)
        except Exception as exc:
            rationale.update({"reason": "preview_failed", "error": str(exc)})
            self._last_rationale = rationale
            return rationale

        exec_price = None
        try:
            exec_price = float(preview_result.effective_price)
        except Exception:
            exec_price = None
        slip_check = self._check_slippage_buy(
            exec_price if exec_price is not None else float(market_price),
            float(market_price),
            precomputed_slippage_bps=getattr(preview_result, "slippage_bps", None),
            slippage_cap_bps=slippage_cap_bps,
        )

        rationale.update(
            {
                "slippage_check": slip_check,
            }
        )

        if slip_check.get("quote_failure", False):
            rationale.update({"reason": "quote_failure"})
            self._last_rationale = rationale
            return rationale

        if not bool(slip_check.get("ok", False)):
            rationale.update({"reason": "slippage_exceeded"})
            self._last_rationale = rationale
            return rationale

        if simulate:
            rationale.update(
                {
                    "decision": "inventory_liquidate",
                    "reason": "simulate",
                    "executed": True,
                    "execution_status": ExecutionStatus.SIMULATED.value,
                }
            )
            self._last_rationale = rationale
            return rationale

        try:
            execution_result = self.diem.execute_trade(sell_intent, simulate=False)
            exec_dict = execution_result.as_dict()
            rationale["execution"] = exec_dict
            status_val = (
                execution_result.status.value
                if isinstance(execution_result.status, ExecutionStatus)
                else str(execution_result.status)
            )
            success = execution_result.status in {
                ExecutionStatus.SUBMITTED,
                ExecutionStatus.CONFIRMED,
            }
            rationale.update(
                {
                    "execution_status": status_val,
                    "tx_hash": exec_dict.get("tx_hash"),
                    "executed": bool(success),
                }
            )
            if success:
                rationale.update(
                    {
                        "decision": "inventory_liquidate",
                        "reason": "sold_unlocked_inventory",
                    }
                )
                _metrics_inc(
                    "agent_decisions_total",
                    labels={"agent": "arbi_diem", "action": "inventory_liquidate"},
                )
            else:
                rationale.update({"decision": "hold", "reason": "execution_failed"})
        except Exception as exc:
            rationale.update({"reason": "execution_exception", "error": str(exc)})

        self._last_rationale = rationale
        return rationale

    def _slippage_soft_cap_bps(self) -> float | None:
        """Optional soft slippage cap (bps) for telemetry/classification only.

        If set, trades slightly over the hard cap but under this soft cap may be
        classified differently in logs/metrics, but execution still requires
        hard cap compliance.

        Returns None if not configured (soft cap disabled).
        """
        try:
            raw = os.getenv("ARBI_DIEM_SLIPPAGE_SOFT_BPS")
            if raw is None or raw.strip() == "":
                return None
            val = float(raw)
            if val <= 0:
                return None
            return float(val)
        except Exception:
            return None

    def on_run_mode(self, *, dry_run: bool, transitioned_to_live: bool = False) -> None:
        """Update internal state for the current execution mode."""
        new_mode = "dry" if dry_run else "live"
        was_live = self._last_run_mode == "live"
        transition_detected = transitioned_to_live or (
            self._last_run_mode == "dry" and new_mode == "live"
        )

        if transition_detected:
            if self._ratio_history or self._util_history:
                logger.info(
                    "Resetting trend history after live transition",
                    extra={
                        "ratio_history_len": len(self._ratio_history),
                        "util_history_len": len(self._util_history),
                    },
                )
            self._ratio_history.clear()
            self._util_history.clear()

        if not dry_run and not was_live and new_mode == "live":
            logger.debug("ArbiDiem live mode engaged")

        self._run_mode = new_mode
        self._last_run_mode = new_mode

    def _check_factory_registration(self) -> bool:
        """Verify bridge pools are registered with their factories."""

        if self._factory_registration_cache is True:
            return True

        try:
            w3 = get_web3()
            addresses = load_addresses()
            aerodrome_status = check_aerodrome_registration(w3, addresses)
            uniswap_status = check_uniswap_v3_registration(w3, addresses)
            registered = bool(aerodrome_status.registered and uniswap_status.registered)
            if registered:
                self._factory_registration_cache = True
                return True

            logger.info(
                "Execution blocked: bridge pools not registered with factories. "
                "Run `market:bridge-factory-check` for details.",
                extra={
                    "aerodrome_registered": bool(aerodrome_status.registered),
                    "uniswap_registered": bool(uniswap_status.registered),
                },
            )
            self._factory_registration_cache = False
            return False
        except Exception as exc:
            logger.info(
                "Execution blocked: bridge pool registration check failed",
                extra={"error": str(exc)},
            )
            self._factory_registration_cache = False
            return False

    def _market_provider(self) -> Any:
        if self.market is not None:
            return self.market
        if self._market_cached is None:
            from services.marketdata.provider import MarketDataProvider  # lazy import

            self._market_cached = MarketDataProvider()
        return self._market_cached

    def _current_vvv_price(self) -> float:
        """Return the current VVV price with deterministic fallbacks for tests."""
        override = os.getenv("VVV_FAKE_PRICE") or os.getenv("TEST_VVV_PRICE")
        if override not in (None, ""):
            try:
                value = float(override)
                if value > 0:
                    return value
            except Exception:
                pass
        if os.getenv("PYTEST_CURRENT_TEST"):
            return 1.0
        try:
            md = self._market_provider()
            price = md.prices(["VVV"]).get("VVV")
            if isinstance(price, (int, float)) and price > 0:
                return float(price)
        except Exception:
            pass
        return 1.0

    def _target_supply(self) -> int:
        raw = os.getenv("DIEM_TARGET_SUPPLY")
        if raw is not None:
            try:
                candidate = int(raw)
                if candidate > 0:
                    return candidate
            except Exception:
                pass
        return 38_000

    def _trade_routes(self):
        try:
            # Prefer an explicit _trade_routes hook when present.
            # This allows tests and callers to override the dynamic path logic.
            routes_attr = getattr(self.diem, "_trade_routes", None)
            if callable(routes_attr):
                try:
                    routes = routes_attr()
                except TypeError:
                    # Allow unbound functions that still expect the service instance.
                    routes = routes_attr(self.diem)
                if routes:
                    try:
                        muted_fn = getattr(self.diem, "_is_route_muted", None)
                        if callable(muted_fn):
                            routes = [r for r in routes if not bool(muted_fn(r))]
                    except Exception:
                        pass
                    return routes
        except Exception:
            pass
        try:
            routes_attr = getattr(self.diem, "trade_routes", None)
            if callable(routes_attr):
                try:
                    routes = routes_attr()
                except TypeError:
                    # Allow unbound functions that still expect the service instance
                    routes = routes_attr(self.diem)
                if routes:
                    try:
                        muted_fn = getattr(self.diem, "_is_route_muted", None)
                        if callable(muted_fn):
                            routes = [r for r in routes if not bool(muted_fn(r))]
                    except Exception:
                        pass
                    return routes
        except Exception:
            pass
        try:
            env_route = getattr(self.diem, "_route_from_env", None)
            if callable(env_route):
                route_obj = env_route()
                if route_obj:
                    return [route_obj]
        except Exception:
            pass
        try:
            tokens = self.diem._path_from_env()
            if tokens:
                from libs.dex.routes import make_route

                return [make_route(tokens)]
        except Exception:
            pass
        return []

    def _get_direct_buy_route_if_enabled(self) -> RoutePlan | None:
        """Return a direct USDC→DIEM route if DIEM_BUY_DIRECT_ONLY=1.

        When direct-only mode is enabled, this avoids selecting a bridge route
        (DIEM→VVV→USDC) and reversing it, which would create a failing
        USDC→VVV→DIEM bridge buy path.
        """
        buy_direct_only = os.getenv("DIEM_BUY_DIRECT_ONLY", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not buy_direct_only:
            return None

        try:
            diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
            quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
            if not (diem_addr and quote_addr):
                return None

            routes = self._trade_routes()
            for route in routes:
                tokens = list(route.tokens) if hasattr(route, "tokens") else []
                # Look for direct 2-hop route: USDC→DIEM (buy direction)
                if (
                    len(tokens) == 2
                    and tokens[0].lower() == quote_addr
                    and tokens[-1].lower() == diem_addr
                ):
                    logger.debug(
                        "DIEM_BUY_DIRECT_ONLY: Using direct USDC→DIEM route for buy preview"
                    )
                    return route
        except Exception as e:
            logger.debug(f"_get_direct_buy_route_if_enabled failed: {e}")
        return None

    def _filter_routes_by_health(
        self, routes: list[RoutePlan]
    ) -> tuple[list[RoutePlan], list[RoutePlan]]:
        """
        Filter routes by health, separating healthy routes from unhealthy ones.

        Returns:
            Tuple of (healthy_routes, unhealthy_routes)
        """
        healthy: list[RoutePlan] = []
        unhealthy: list[RoutePlan] = []

        if not routes:
            return (healthy, unhealthy)

        aggregator = self.diem.aggregator
        if aggregator is None:
            return (routes, unhealthy)

        for route in routes:
            try:
                route_tokens = list(route.tokens) if hasattr(route, "tokens") else []
                diagnostics = getattr(aggregator, "_last_quote_diagnostics", [])
                route_diagnostics = []

                for diag in diagnostics:
                    diag_route = diag.get("route", [])
                    if (
                        isinstance(diag_route, list)
                        and len(diag_route) == len(route_tokens)
                        and all(
                            str(diag_route[i]).lower() == str(route_tokens[i]).lower()
                            for i in range(len(route_tokens))
                        )
                    ):
                        route_diagnostics.append(diag)

                if not route_diagnostics:
                    try:
                        # For exact_out probes, the output token is DIEM (18 decimals)
                        # The dust threshold requires ~4.58e9 base units minimum for USDC input
                        # Use 10**15 (0.001 DIEM ≈ $0.22) to be safely above dust threshold
                        diem_decimals = 18
                        try:
                            diem_decimals = int(self.risk._diem_decimals())
                        except Exception:
                            pass
                        # For 18-decimal tokens, use 10**15 (0.001 tokens)
                        # For 6-decimal tokens, use 10**6 (1 token)
                        probe_amount = 10 ** max(6, diem_decimals - 3)
                        probe_route = (
                            route.reversed() if hasattr(route, "reversed") else route
                        )
                        if hasattr(aggregator, "best_quote_exact_out"):
                            aggregator.best_quote_exact_out(probe_amount, probe_route)
                            diagnostics = getattr(
                                aggregator, "_last_quote_diagnostics", []
                            )
                            for diag in diagnostics:
                                diag_route = diag.get("route", [])
                                if (
                                    isinstance(diag_route, list)
                                    and len(diag_route) == len(route_tokens)
                                    and all(
                                        str(diag_route[i]).lower()
                                        == str(route_tokens[i]).lower()
                                        for i in range(len(route_tokens))
                                    )
                                ):
                                    route_diagnostics.append(diag)
                        else:
                            probe_quote = aggregator.best_quote(probe_amount, route)
                            if probe_quote:
                                diagnostics = getattr(
                                    aggregator, "_last_quote_diagnostics", []
                                )
                                for diag in diagnostics:
                                    diag_route = diag.get("route", [])
                                    if (
                                        isinstance(diag_route, list)
                                        and len(diag_route) == len(route_tokens)
                                        and all(
                                            str(diag_route[i]).lower()
                                            == str(route_tokens[i]).lower()
                                            for i in range(len(route_tokens))
                                        )
                                    ):
                                        route_diagnostics.append(diag)
                    except Exception as probe_exc:
                        logger.debug(
                            "Route health probe quote failed: %s",
                            probe_exc,
                            extra={
                                "agent": "arbi_diem",
                                "action": "route_health_probe_failed",
                            },
                        )
            except Exception as exc:
                logger.debug(
                    "Route health check failed, assuming healthy: %s",
                    exc,
                    extra={
                        "agent": "arbi_diem",
                        "action": "route_health_check_failed",
                    },
                )
                healthy.append(route)
                continue

            health = self.diem._classify_route_health(route, diagnostics)
            if health in ("healthy", "unknown"):
                healthy.append(route)
            else:
                # Check if this is a direct DIEM/USDC route that works via slot0
                # The slot0 fallback bypasses standard provider diagnostics, so we
                # should treat direct 2-token routes with a known pool as healthy
                is_direct_route = len(route_tokens) == 2
                has_slot0_pool = False
                if is_direct_route:
                    try:
                        diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").lower()
                        usdc_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").lower()
                        route_lower = [t.lower() for t in route_tokens]
                        is_diem_usdc = (
                            diem_addr in route_lower and usdc_addr in route_lower
                        )
                        if is_diem_usdc:
                            # Check if slot0 pool is configured
                            slot0_pool = os.getenv("DIEM_USDC_SLOT0_POOL", "")
                            has_slot0_pool = bool(slot0_pool.strip())
                    except Exception:
                        pass

                if is_direct_route and has_slot0_pool:
                    # Direct route with slot0 pool - trust it as healthy despite mismatched diagnostics
                    logger.debug(
                        "Direct DIEM/USDC route treated as healthy (slot0 pool available)",
                        extra={
                            "agent": "arbi_diem",
                            "route": route_tokens,
                            "original_health": health,
                        },
                    )
                    healthy.append(route)
                else:
                    unhealthy.append(route)
                    logger.info(
                        "Route filtered out due to health: %s",
                        health,
                        extra={
                            "agent": "arbi_diem",
                            "route": getattr(route, "path", []),
                            "health": health,
                        },
                    )

        return (healthy, unhealthy)

    def _get_bridge_route_for_buy(self) -> Any | None:
        """Construct a bridge-based RoutePlan for DIEM buy operations.

        Returns a RoutePlan with composite metadata for DIEM→VVV→USDC path,
        or None if bridge path is unavailable.
        """
        try:
            from libs.dex.composite import attach_composite_metadata
            from libs.dex.routes import RouteHop, RoutePlan
            from services.marketdata.pathing.env import load_env_config
            from services.marketdata.pathing.fallbacks import (
                get_bridge_trade_path_with_metadata,
            )

            config = load_env_config()
            bridge_metadata = get_bridge_trade_path_with_metadata(config)
            if not bridge_metadata:
                return None

            path = bridge_metadata.get("path")
            legs = bridge_metadata.get("legs", [])
            if not path or len(path) < 3 or not legs:
                return None

            # Construct RoutePlan from bridge path
            # For buy direction: USDC → VVV → DIEM (reversed from sell path)
            # But we'll construct it in sell direction first, then reverse when using
            hops = []
            for i in range(len(path) - 1):
                token_in = path[i]
                token_out = path[i + 1]
                # Find matching leg for fee tier
                leg = next(
                    (
                        leg_item
                        for leg_item in legs
                        if leg_item.get("token_in", "").lower() == token_in.lower()
                        and leg_item.get("token_out", "").lower() == token_out.lower()
                    ),
                    None,
                )
                fee = leg.get("fee") if leg else None
                # If VVV/USDC leg and no fee specified, default to 3000 for V3
                if (
                    i == len(path) - 2
                    and fee is None
                    and leg
                    and leg.get("provider") == "uniswap_v3"
                ):
                    try:
                        fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
                        fee = int(fee_str)
                    except Exception:
                        fee = 3000
                hops.append(RouteHop(token_in=token_in, token_out=token_out, fee=fee))

            route = RoutePlan(tuple(hops))

            # Attach composite metadata
            attach_composite_metadata(route, bridge_legs=legs, is_composite=True)

            return route
        except Exception as e:
            logger.debug(f"Failed to construct bridge route: {e}")
            return None

    def _desired_units(self, price_usd_per_diem: float | None = None) -> int:
        """Return initial desired DIEM units before risk/liquidity adjustments.

        Prefers an explicit USD notional (`ARBI_DIEM_TRADE_USD`, default 20)
        so we size in dollars first, then convert to units using the live price.
        Falls back to the legacy token-based config when price is missing.
        """
        os_mod = __import__("os")
        env = os_mod.getenv

        # USD-first sizing when we have a market price.
        if price_usd_per_diem is not None and price_usd_per_diem > 0:
            usd_raw = env("ARBI_DIEM_TRADE_USD") or env("ARBI_DIEM_TARGET_TRADE_USD")
            default_usd = "20"
            try:
                trade_usd = (
                    float(usd_raw) if usd_raw not in (None, "") else float(default_usd)
                )
            except Exception:
                trade_usd = float(default_usd)

            # Clamp by configured max_trade_usd to respect risk limits.
            try:
                cap_usd = float(getattr(self.risk, "max_trade_usd", trade_usd))
                if cap_usd > 0:
                    trade_usd = min(trade_usd, cap_usd)
            except Exception:
                pass

            # Optional floor to avoid zeroing out tiny trades if requested.
            try:
                floor_raw = env("ARBI_DIEM_MIN_TRADE_USD") or env(
                    "ARBI_DIEM_MIN_NOTIONAL_USD"
                )
                if floor_raw not in (None, ""):
                    trade_usd = max(trade_usd, float(floor_raw))
            except Exception:
                pass

            try:
                units_from_usd = self.risk.units_from_usd(trade_usd, price_usd_per_diem)
                if units_from_usd > 0:
                    return int(units_from_usd)
            except Exception:
                # Fall back to legacy path if conversion fails.
                pass

        # Legacy token-based sizing (kept for backward compatibility)
        raw = str(env("ARBI_DIEM_MINT_UNITS") or "1000").strip()
        if not raw:
            raw = "1000"
        base_flag_raw = (
            str(env("ARBI_DIEM_MINT_UNITS_BASE_UNITS") or "").strip().lower()
        )
        base_flag = base_flag_raw in {"1", "true", "yes", "on"}
        if base_flag:
            try:
                value = int(raw, 0)
            except Exception:
                try:
                    value = int(Decimal(raw))
                except Exception:
                    return 0
            return max(0, int(value))
        try:
            tokens = Decimal(raw)
        except (InvalidOperation, ValueError):
            tokens = Decimal("1000")
        try:
            decimals = int(self.risk._diem_decimals())
        except Exception:
            decimals = 18
        scale = Decimal(10) ** decimals
        units = tokens * scale
        if units <= 0:
            return 0
        try:
            return int(units)
        except Exception:
            return 0

    def _decimals_out(self) -> int:
        try:
            from web3 import Web3  # type: ignore

            from libs.agentkit_ext.web3_utils import get_contract, get_web3

            routes = self._trade_routes()
            path = routes[0].tokens if routes else []
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
        if self.diem.aggregator is None:
            return 0.0
        routes = self._trade_routes()
        if not routes:
            return 0.0
        for route in routes:
            try:
                quote = self.diem.aggregator.best_quote(units_in, route)
            except Exception:
                continue
            if quote is None:
                continue
            try:
                dec_in = self.risk._diem_decimals()
                dec_out = self._decimals_out()
                amt_in = quote.amount_in / float(10**dec_in)
                amt_out = quote.amount_out / float(10**dec_out)
                if amt_in <= 0:
                    continue
                return float(amt_out / amt_in)
            except Exception:
                continue
        return 0.0

    def _erc20_decimals(self, address: str) -> int:
        try:
            from web3 import Web3  # type: ignore

            from libs.agentkit_ext.web3_utils import get_contract, get_web3

            w3 = get_web3()
            erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
            return int(erc20.functions.decimals().call())
        except Exception:
            return 18

    def _preview_exec_price_buy(
        self, units_out: int
    ) -> float | tuple[float, float | None]:
        """Quote execution price (USD per DIEM) for DIEM buy previews.

        Uses configured DIEM buy mode (DIEM_BUY_EXECUTION_MODE). When exact_in is enabled,
        uses diem.preview_trade() to avoid multi-hop exact-out reverts.

        When exact_out is enabled, uses aggregator.best_quote_exact_out on the reversed
        TRADE_PATH (QUOTE->...->DIEM).
        Supports composite routes (multi-venue bridge paths).
        Prioritizes bridge-based DIEM→VVV→USDC route when available.
        Returns 0.0 when unavailable. When composite quotes provide precomputed slippage,
        returns a tuple of (exec_price, precomputed_slippage_bps).

        In live mode, bridge_vvv fallback is gated behind DIEM_BRIDGE_LIVE_FALLBACK_ENABLE
        to prevent execution without valid DEX quotes.
        """
        if self.diem.aggregator is None:
            return 0.0

        buy_execution_mode = (
            os.getenv("DIEM_BUY_EXECUTION_MODE", "exact_in").strip().lower()
        )
        if buy_execution_mode == "exact_in":
            try:
                # When DIEM_BUY_DIRECT_ONLY=1, prefer direct USDC→DIEM route
                direct_buy = self._get_direct_buy_route_if_enabled()
                if direct_buy is not None:
                    preferred = direct_buy
                else:
                    routes = self._trade_routes()
                    preferred = routes[0].reversed() if routes and routes[0] else None
                slippage_bps = int(getattr(self.risk, "slippage_bps_cap", 50) or 50)
                intent = ExecutionIntent(
                    side=TradeSide.BUY,
                    token_in="USDC",
                    token_out="DIEM",
                    amount_base_units=int(units_out),
                    slippage_bps=slippage_bps,
                    preferred_route=preferred,
                    metadata={"agent": "arbi_diem", "preview": True},
                )
                preview = self.diem.preview_trade(intent)
                if preview.effective_price is not None:
                    if preview.slippage_bps is not None:
                        return float(preview.effective_price), float(
                            preview.slippage_bps
                        )
                    return float(preview.effective_price)
            except Exception:
                return 0.0

        has_exact_out = hasattr(self.diem.aggregator, "best_quote_exact_out")
        if not has_exact_out:
            # Prefer preview_trade for exact-in previews when exact-out is unavailable.
            try:
                # When DIEM_BUY_DIRECT_ONLY=1, prefer direct USDC→DIEM route
                direct_buy = self._get_direct_buy_route_if_enabled()
                if direct_buy is not None:
                    preferred = direct_buy
                else:
                    routes = self._trade_routes()
                    preferred = routes[0].reversed() if routes and routes[0] else None
                slippage_bps = int(getattr(self.risk, "slippage_bps_cap", 50) or 50)
                intent = ExecutionIntent(
                    side=TradeSide.BUY,
                    token_in="USDC",
                    token_out="DIEM",
                    amount_base_units=int(units_out),
                    slippage_bps=slippage_bps,
                    preferred_route=preferred,
                    metadata={"agent": "arbi_diem", "preview": True},
                )
                preview = self.diem.preview_trade(intent)
                if preview.effective_price is not None:
                    if preview.slippage_bps is not None:
                        return float(preview.effective_price), float(
                            preview.slippage_bps
                        )
                    return float(preview.effective_price)
            except Exception:
                pass

        # Try bridge-based route first (ID-001: enforce bridge path for DIEM buys)
        bridge_route = self._get_bridge_route_for_buy()
        if bridge_route is not None and has_exact_out:
            try:
                rev_route = bridge_route.reversed()
                quote = self.diem.aggregator.best_quote_exact_out(units_out, rev_route)  # type: ignore[attr-defined]
                if quote is not None:
                    try:
                        dec_in = self._erc20_decimals(rev_route.tokens[0])
                        dec_out = self.risk._diem_decimals()
                        amt_in = quote.amount_in / float(10**dec_in)
                        amt_out = quote.amount_out / float(10**dec_out)
                        if amt_out > 0:
                            precomputed_slip = getattr(
                                quote, "total_slippage_bps", None
                            )
                            logger.debug(
                                f"Bridge route quote successful: {amt_in:.6f} -> {amt_out:.6f} DIEM"
                            )
                            exec_price = float(amt_in / amt_out)
                            if precomputed_slip is not None:
                                return exec_price, float(precomputed_slip)
                            return exec_price
                    except Exception:
                        pass  # Fall through to generic routes
            except Exception:
                pass  # Fall through to generic routes

        routes = self._trade_routes()
        if not routes:
            return 0.0

        has_non_composite_route = False
        for route in routes:
            try:
                rev_route = route.reversed()
                # Preserve composite route metadata when reversing
                bridge_legs = get_composite_bridge_legs(route)
                is_composite = getattr(route, "_is_composite", False)
                if not is_composite:
                    has_non_composite_route = True
                if bridge_legs and is_composite:
                    # Reverse the leg metadata for the reversed route
                    try:
                        reversed_legs = []
                        for leg in reversed(bridge_legs):
                            reversed_legs.append(
                                {
                                    "token_in": leg.get("token_out", ""),
                                    "token_out": leg.get("token_in", ""),
                                    "provider": leg.get("provider", "uniswap_v2"),
                                    "pool_address": leg.get("pool_address"),
                                    "fee": leg.get("fee"),
                                }
                            )
                        attach_composite_metadata(
                            rev_route,
                            bridge_legs=reversed_legs,
                            is_composite=True,
                        )
                    except Exception:
                        pass  # Metadata preservation is best-effort

                quote = self.diem.aggregator.best_quote_exact_out(units_out, rev_route)  # type: ignore[attr-defined]
            except Exception:
                continue
            if quote is None:
                continue
            try:
                dec_in = self._erc20_decimals(rev_route.tokens[0])
                dec_out = (
                    self.risk._diem_decimals()
                    if has_exact_out
                    else dec_in  # when using exact-in fallback preview, assume same decimals
                )
                amt_in = quote.amount_in / float(10**dec_in)
                amt_out = quote.amount_out / float(10**dec_out)
                if amt_out <= 0:
                    continue

                precomputed_slip = getattr(quote, "total_slippage_bps", None)

                # Log composite route usage for telemetry
                if quote.provider == "composite":
                    logger.debug(
                        f"Composite route quote successful: {amt_in:.6f} -> {amt_out:.6f} DIEM"
                    )

                exec_price = float(amt_in / amt_out)
                if precomputed_slip is not None:
                    return exec_price, float(precomputed_slip)
                return exec_price
            except Exception:
                continue

        # When all available routes are composite and no exact-out quote succeeded,
        # treat this as a hard preview failure so the caller can decide whether to
        # use exact-in fallback or skip the trade.
        if not has_non_composite_route:
            return 0.0

        # Fallback: Use bridge_vvv price as execution proxy when router quotes fail
        # for non-composite routes only.
        # In live mode, this fallback is gated behind DIEM_BRIDGE_LIVE_FALLBACK_ENABLE
        # to prevent execution without valid DEX quotes.
        is_live_mode = self._run_mode == "live"
        bridge_live_fallback_enabled = os.getenv(
            "DIEM_BRIDGE_LIVE_FALLBACK_ENABLE", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

        # In live mode, only use bridge fallback if explicitly enabled
        if is_live_mode and not bridge_live_fallback_enabled:
            logger.debug(
                "Bridge_vvv fallback skipped in live mode (DIEM_BRIDGE_LIVE_FALLBACK_ENABLE=0)"
            )
            return 0.0

        try:
            md = self._market_provider()
            health = md.price_health("DIEM")
            source = health.get("source", "")
            price = health.get("price")
            valid = health.get("valid", False)
            if source == "bridge_vvv" and price is not None and valid:
                try:
                    bridge_price = float(price)
                    if bridge_price > 0:
                        mode_label = (
                            "simulation" if self._run_mode == "dry" else "execution"
                        )
                        logger.debug(
                            f"Using bridge_vvv price as {mode_label} preview fallback: ${bridge_price:.2f}"
                        )
                        return bridge_price
                except Exception:
                    pass
        except Exception:
            pass

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

    def _adjust_for_liquidity(
        self,
        units_in: int,
        market_price: float,
        *,
        preview_price: float | None = None,
        slippage_cap_bps: float | None = None,
        include_initial_bps: bool = False,
    ) -> tuple[int, float | None] | tuple[int, float | None, float | None]:
        """Iteratively reduce trade size until slippage is within hard cap, respecting minimum trade notional.

        This method shrinks the trade size by halving on each iteration until either:
        - Slippage falls within the hard cap (returns compliant trade size)
        - Minimum trade USD threshold is reached (stops shrinking)
        - Maximum adjustment steps are exhausted (stops shrinking)

        Returns (adjusted_units, final_slippage_bps) by default.

        When include_initial_bps=True, returns (adjusted_units, final_slippage_bps, initial_slippage_bps).
        The hard slippage cap is enforced for execution decisions; soft cap (if configured)
        is used only for telemetry/classification.
        """
        if units_in <= 0 or market_price <= 0:
            if include_initial_bps:
                return 0, None, None
            return 0, None

        # Compute minimum units based on minimum trade USD
        min_trade_usd = self._liquidity_min_trade_usd()
        min_units = 0
        if min_trade_usd > 0:
            try:
                min_units = self.risk.units_from_usd(min_trade_usd, market_price)
            except Exception:
                min_units = 0

        # Get initial preview price
        exec_px = (
            float(preview_price)
            if (preview_price is not None and preview_price > 0)
            else self._preview_exec_price(units_in)
        )
        if exec_px <= 0:
            if include_initial_bps:
                return units_in, None, None
            return units_in, None

        # Check initial slippage
        slip = self.risk.check_slippage(exec_px, market_price, cap_bps=slippage_cap_bps)
        bps = float(slip.get("slippage_bps", 0.0)) if isinstance(slip, dict) else 0.0
        epsilon = 1e-6
        initial_bps: float | None = bps

        # Determine hard cap (execution requirement)
        try:
            hard_cap = float(
                slippage_cap_bps
                if slippage_cap_bps is not None
                else getattr(self.risk, "slippage_bps_cap", 0.0)
            )
        except Exception:
            hard_cap = 0.0
        threshold = hard_cap - epsilon if hard_cap > epsilon else hard_cap

        def _within_hard_cap(bps_val: float, slip_ok: bool) -> bool:
            """Check if slippage is within the hard cap (required for execution)."""
            if not slip_ok:
                return False
            try:
                val = float(bps_val)
            except Exception:
                return False
            if not math.isfinite(val):
                return False
            if hard_cap <= epsilon:
                return val <= hard_cap
            return val <= threshold

        # If initial size already complies, return early
        if _within_hard_cap(bps, bool(slip.get("ok", False))):
            try:
                _metrics_inc(
                    "risk_liquidity_checks_total", labels={"adjusted": "false"}
                )
                _metrics_inc(
                    "risk_liquidity_slippage_bucket_total",
                    labels={"bucket": self._slippage_bucket(bps)},
                )
            except Exception:
                pass
            if include_initial_bps:
                return int(units_in), bps, initial_bps
            return int(units_in), bps

        # Iteratively shrink until compliant or limits reached
        adjusted = int(units_in)
        last_bps: float | None = bps
        max_steps = self._liquidity_max_adjust_steps()

        for step in range(max_steps):
            # Stop if we've hit minimum trade size
            if min_units > 0 and adjusted <= min_units:
                logger.debug(
                    f"Liquidity adjustment stopped at minimum trade size: {adjusted} units (min_usd=${min_trade_usd:.2f})"
                )
                break

            # Halve the size for next iteration
            adjusted = max(0, adjusted // 2)
            if adjusted <= 0:
                break

            # Get preview for adjusted size
            px = self._preview_exec_price(adjusted)
            if px <= 0:
                break

            # Check slippage for adjusted size
            slip2 = self.risk.check_slippage(px, market_price, cap_bps=slippage_cap_bps)
            bps2 = (
                float(slip2.get("slippage_bps", float("inf")))
                if isinstance(slip2, dict)
                else float("inf")
            )
            prev_bps = last_bps
            last_bps = bps2

            # If compliant with hard cap, return this size
            if _within_hard_cap(bps2, bool(slip2.get("ok", False))):
                try:
                    _metrics_inc(
                        "risk_liquidity_checks_total", labels={"adjusted": "true"}
                    )
                    _metrics_inc(
                        "risk_liquidity_slippage_bucket_total",
                        labels={"bucket": self._slippage_bucket(bps2)},
                    )
                except Exception:
                    pass
                logger.debug(
                    f"Liquidity adjustment succeeded: {adjusted} units (slippage_bps={bps2:.2f}, steps={step + 1})"
                )
                if include_initial_bps:
                    return int(adjusted), bps2, initial_bps
                return int(adjusted), bps2

            # Early exit if slippage stopped improving (converged)
            try:
                if prev_bps is not None and abs(bps2 - prev_bps) < 1e-6:
                    if adjusted <= 1:
                        break
                    continue
            except Exception:
                pass

        # Log final state (may still exceed cap if min size reached)
        try:
            _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "true"})
            _metrics_inc(
                "risk_liquidity_slippage_bucket_total",
                labels={
                    "bucket": self._slippage_bucket(
                        last_bps if last_bps is not None else float("inf")
                    )
                },
            )
        except Exception:
            pass

        # Return final adjusted size (caller will check if slippage is acceptable)
        if include_initial_bps:
            return int(max(0, adjusted)), last_bps, initial_bps
        return int(max(0, adjusted)), last_bps

    def _check_slippage_buy(
        self,
        exec_price: float,
        ref_price: float,
        *,
        precomputed_slippage_bps: float | None = None,
        slippage_cap_bps: float | None = None,
    ) -> dict:
        """Check slippage for buy operations with sanity bounds.

        Returns dict with:
        - ok: True if slippage is within acceptable cap
        - slippage_bps: Calculated slippage in basis points
        - quote_failure: True when pricing is unusable (non-finite, zero, etc.)
        """
        try:
            if ref_price <= 0 or exec_price <= 0:
                return {
                    "ok": False,
                    "slippage_bps": float("inf"),
                    "quote_failure": True,
                }

            if precomputed_slippage_bps is not None:
                slip = max(0.0, float(precomputed_slippage_bps))
            else:
                slip = max(0.0, (exec_price - ref_price) / ref_price * 10_000.0)

            # Treat only non-finite values as quote failures; large but finite
            # slippage is handled by the usual policy caps and adjustment logic.
            if not math.isfinite(slip):
                return {
                    "ok": False,
                    "slippage_bps": float("inf"),
                    "quote_failure": True,
                }

            # ID-002: Add slippage sanity cap to treat astronomical values as quote failures
            # Configurable via env, default 50,000 bps (500%)
            try:
                sanity_max_raw = os.getenv("RISK_SLIPPAGE_SANITY_MAX_BPS", "50000")
                sanity_max_bps = float(sanity_max_raw)
            except Exception:
                sanity_max_bps = 50000.0

            if slip > sanity_max_bps:
                logger.debug(
                    f"Slippage sanity cap exceeded: {slip:.2f} bps > {sanity_max_bps:.2f} bps, treating as quote failure"
                )
                return {
                    "ok": False,
                    "slippage_bps": float("inf"),
                    "quote_failure": True,
                }

            try:
                cap_val = (
                    float(slippage_cap_bps)
                    if slippage_cap_bps is not None
                    else float(self.risk.slippage_bps_cap)
                )
            except Exception:
                cap_val = float(self.risk.slippage_bps_cap)

            return {
                "ok": slip <= cap_val,
                "slippage_bps": float(slip),
                "quote_failure": False,
            }
        except Exception:
            return {"ok": False, "slippage_bps": float("inf"), "quote_failure": True}

    def _calculate_adaptive_slippage_multiplier(self, route: Any | None) -> float:
        """Calculate adaptive slippage multiplier based on route characteristics.

        Args:
            route: RoutePlan or None

        Returns:
            Multiplier to apply to slippage cap (e.g., 1.2 for 20% increase)
        """
        # Check if adaptive slippage is enabled
        adaptive_enabled = os.getenv(
            "DIEM_ADAPTIVE_SLIPPAGE_ENABLE", "1"
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not adaptive_enabled or route is None:
            return 1.0

        multiplier = 1.0

        # Get route characteristics
        tokens = list(route.tokens) if hasattr(route, "tokens") else []
        hop_count = len(tokens) - 1 if len(tokens) > 1 else 1
        route_is_v3 = (
            route.is_uniswap_v3() if hasattr(route, "is_uniswap_v3") else False
        )

        # Increase multiplier for multi-hop routes (more hops = more slippage risk)
        # Add ~5% per hop beyond 2 hops
        if hop_count > 2:
            multiplier += (hop_count - 2) * 0.05

        # Increase multiplier for V3 routes (concentrated liquidity can have higher slippage)
        # Add ~10% for V3 routes
        if route_is_v3:
            multiplier += 0.10

        # Cap maximum multiplier at 1.5x (50% increase)
        multiplier = min(multiplier, 1.5)

        return multiplier

    def _adjust_for_liquidity_buy(
        self,
        units_out: int,
        market_price: float,
        *,
        slippage_cap_bps: float | None = None,
    ) -> tuple[int, float | None, float | None]:
        """Iteratively reduce DIEM units to buy until slippage is within hard cap, respecting minimum trade notional.

        Uses the configured DIEM buy preview mode to estimate execution price. Shrinks trade size by halving on each
        iteration until either:
        - Slippage falls within the hard cap (returns compliant trade size)
        - Minimum trade USD threshold is reached (stops shrinking)
        - Maximum adjustment steps are exhausted (stops shrinking)
        - Quote failure detected (returns None to trigger fallback)

        Returns (adjusted_units_out, final_slippage_bps, initial_slippage_bps).
        The hard slippage cap is enforced for execution decisions; soft cap (if configured)
        is used only for telemetry/classification.
        """
        if units_out <= 0 or market_price <= 0:
            return 0, None, None

        # Compute minimum units based on minimum trade USD
        min_trade_usd = self._liquidity_min_trade_usd()
        min_units = 0
        if min_trade_usd > 0:
            try:
                min_units = self.risk.units_from_usd(min_trade_usd, market_price)
            except Exception:
                min_units = 0

        # Get initial preview price (exact-out)
        preview = self._preview_exec_price_buy(units_out)
        precomputed_slip: float | None = None
        exec_px = preview
        if isinstance(preview, tuple):
            try:
                exec_px, precomputed_slip = preview
            except Exception:
                exec_px = preview[0] if preview else 0.0  # type: ignore[index]
        try:
            exec_px_val = float(exec_px)
        except Exception:
            exec_px_val = 0.0
        if exec_px_val <= 0:
            return units_out, None, None

        # Check initial slippage
        slip = self._check_slippage_buy(
            exec_px_val,
            market_price,
            precomputed_slippage_bps=precomputed_slip,
            slippage_cap_bps=slippage_cap_bps,
        )

        # If slippage indicates quote failure, return None to trigger fallback
        if slip.get("quote_failure", False):
            logger.debug(
                f"Quote failure detected in slippage check: slippage_bps={slip.get('slippage_bps')}"
            )
            return units_out, None, None

        bps = float(slip.get("slippage_bps", 0.0)) if isinstance(slip, dict) else 0.0
        initial_bps: float | None = bps

        # If initial size already complies, return early
        if bool(slip.get("ok", False)):
            try:
                _metrics_inc(
                    "risk_liquidity_checks_total", labels={"adjusted": "false"}
                )
                _metrics_inc(
                    "risk_liquidity_slippage_bucket_total",
                    labels={"bucket": self._slippage_bucket(bps)},
                )
            except Exception:
                pass
            return int(units_out), bps, initial_bps

        # Iteratively shrink until compliant or limits reached
        adjusted = int(units_out)
        last_bps = bps
        max_steps = self._liquidity_max_adjust_steps()

        for step in range(max_steps):
            # Stop if we've hit minimum trade size
            if min_units > 0 and adjusted <= min_units:
                logger.debug(
                    f"Buy liquidity adjustment stopped at minimum trade size: {adjusted} units (min_usd=${min_trade_usd:.2f})"
                )
                break

            # Halve the size for next iteration
            adjusted = max(0, adjusted // 2)
            if adjusted <= 0:
                break

            # Get preview for adjusted size (exact-out)
            preview_px = self._preview_exec_price_buy(adjusted)
            precomputed_slip = None
            px = preview_px
            if isinstance(preview_px, tuple):
                try:
                    px, precomputed_slip = preview_px
                except Exception:
                    px = preview_px[0] if preview_px else 0.0  # type: ignore[index]
            try:
                px_val = float(px)
            except Exception:
                px_val = 0.0
            if px_val <= 0:
                break

            # Check slippage for adjusted size
            slip2 = self._check_slippage_buy(
                px_val,
                market_price,
                precomputed_slippage_bps=precomputed_slip,
                slippage_cap_bps=slippage_cap_bps,
            )

            # If slippage indicates quote failure, return None
            if slip2.get("quote_failure", False):
                logger.debug(
                    f"Quote failure detected during adjustment: slippage_bps={slip2.get('slippage_bps')}"
                )
                return adjusted, None, initial_bps

            bps2 = (
                float(slip2.get("slippage_bps", 0.0))
                if isinstance(slip2, dict)
                else float("inf")
            )
            last_bps = bps2

            # If compliant with hard cap, return this size
            if bool(slip2.get("ok", False)):
                try:
                    _metrics_inc(
                        "risk_liquidity_checks_total", labels={"adjusted": "true"}
                    )
                    _metrics_inc(
                        "risk_liquidity_slippage_bucket_total",
                        labels={"bucket": self._slippage_bucket(bps2)},
                    )
                except Exception:
                    pass
                logger.debug(
                    f"Buy liquidity adjustment succeeded: {adjusted} units (slippage_bps={bps2:.2f}, steps={step + 1})"
                )
                return int(adjusted), bps2, initial_bps

        # Log final state (may still exceed cap if min size reached)
        try:
            _metrics_inc("risk_liquidity_checks_total", labels={"adjusted": "true"})
            _metrics_inc(
                "risk_liquidity_slippage_bucket_total",
                labels={
                    "bucket": self._slippage_bucket(
                        last_bps if last_bps is not None else float("inf")
                    )
                },
            )
        except Exception:
            pass

        # Return final adjusted size (caller will check if slippage is acceptable)
        return int(max(0, adjusted)), last_bps, initial_bps

    def _try_exact_in_fallback_buy(
        self,
        desired_units_out: int,
        market_price: float,
        current_inventory_usd: float | None = None,
        *,
        simulate: bool = False,
    ) -> tuple[int, float, Any] | None:
        """Attempt exact-in fallback for buy/burn when exact-out preview fails.

        This is a controlled fallback path that:
        - Only works for small trade sizes (capped by DIEM_EXACT_IN_FALLBACK_MAX_USD)
        - Respects slippage and pool-take limits
        - Returns None if fallback cannot provide a valid quote

        Returns:
            Tuple of (adjusted_units_out, slippage_bps, quote) or None if fallback fails
        """
        self._last_exact_in_fallback_reason = None

        # Check for valid cached quote (avoid re-quoting between simulate and execute)
        if not simulate and self._cached_fallback_quote:
            cached = self._cached_fallback_quote
            cache_age = time.time() - cached.get("ts", 0)
            cached_price = cached.get("market_price", 0)
            # Validate cache: fresh enough and price hasn't drifted too much
            price_drift_pct = (
                abs(market_price - cached_price) / cached_price * 100
                if cached_price > 0
                else 100
            )
            if (
                cache_age <= self._cached_fallback_quote_ttl_seconds
                and price_drift_pct < 2.0  # Less than 2% price drift
            ):
                logger.info(
                    f"Exact-in fallback: reusing cached quote (age={cache_age:.1f}s, "
                    f"units={cached.get('units')}, bps={cached.get('bps'):.2f}, "
                    f"provider={cached.get('provider')}, price_drift={price_drift_pct:.2f}%)"
                )
                return (
                    cached.get("units"),
                    cached.get("bps"),
                    cached.get("quote"),
                )
            logger.debug(
                f"Exact-in fallback: cache expired/invalid (age={cache_age:.1f}s, "
                f"ttl={self._cached_fallback_quote_ttl_seconds}s, drift={price_drift_pct:.2f}%)"
            )
            self._cached_fallback_quote = None

        if self.diem.aggregator is None or not hasattr(
            self.diem.aggregator, "best_quote"
        ):
            self._last_exact_in_fallback_reason = "no_aggregator"
            return None

        # Get fallback configuration
        max_usd = float(os.getenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "10.0") or 10.0)
        max_slippage_bps = float(
            os.getenv("DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS", "0") or 0
        )
        # Use global slippage cap if fallback-specific cap not set
        if max_slippage_bps <= 0:
            max_slippage_bps = float(self.risk.slippage_bps_cap)

        # Require valid DIEM price health when available.
        # If price health is missing, proceed conservatively instead of blocking.
        # Allowed sources must match orchestrator validation (see orchestrator.py).
        price_health_valid = True
        try:
            md = self._market_provider()
            health = md.price_health("DIEM")
            source = health.get("source", "")
            # Accept: bridge_vvv, direct_pool (SlipStream), diem_canonical,
            # path_engine* prefixes, and aggregator* prefixes.
            source_ok = source.startswith(("path_engine", "aggregator")) or source in {
                "bridge_vvv",
                "diem_canonical",
                "direct_pool",
            }
            if not source_ok:
                price_health_valid = False
            valid_flag = health.get("valid")
            if valid_flag is False:
                price_health_valid = False
        except Exception:
            pass

        if not price_health_valid:
            self._last_exact_in_fallback_reason = "price_health_invalid"
            return None

        routes = self._trade_routes()
        if not routes:
            self._last_exact_in_fallback_reason = "no_routes"
            return None

        # When DIEM_BUY_DIRECT_ONLY=1, prioritize direct USDC→DIEM route for exact-in fallback
        # This ensures we try the 2-token route first, which uses slot0 quoting
        direct_buy = self._get_direct_buy_route_if_enabled()
        if direct_buy is not None:
            # Move direct route to front of list (dedup if already present)
            filtered = [r for r in routes if list(r.tokens) != list(direct_buy.tokens)]
            routes = [direct_buy, *filtered]
            logger.info(
                f"Exact-in fallback: prioritizing direct USDC→DIEM route (DIEM_BUY_DIRECT_ONLY=1), "
                f"total_routes={len(routes)}"
            )

        diem_decimals = self.risk._diem_decimals()
        diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
        last_failure_reason: str | None = None

        # Try exact-in quote on a QUOTE->...->DIEM route
        for route in routes:
            try:
                try:
                    tokens = list(route.tokens)
                except Exception:
                    tokens = []
                lower_tokens = [str(tok).lower() for tok in tokens] if tokens else []
                route_for_quote = route
                if tokens:
                    if diem_addr and lower_tokens and lower_tokens[-1] == diem_addr:
                        route_for_quote = route
                    elif diem_addr and lower_tokens and lower_tokens[0] == diem_addr:
                        route_for_quote = route.reversed()
                    else:
                        # Fallback: prefer a reversed route if it ends with DIEM
                        try:
                            rev = route.reversed()
                            rev_tokens = [str(tok).lower() for tok in rev.tokens]
                            if rev_tokens and (
                                not diem_addr or rev_tokens[-1] == diem_addr
                            ):
                                route_for_quote = rev
                        except Exception:
                            route_for_quote = route
                rev_route = route_for_quote

                try:
                    rev_tokens = list(rev_route.tokens)
                except Exception:
                    rev_tokens = []
                if not rev_tokens:
                    continue
                target_diem = str(rev_tokens[-1]).lower()
                if diem_addr and target_diem != diem_addr:
                    continue

                quote_dec_in = self._erc20_decimals(rev_tokens[0])
                # Allow explicit override for quote token decimals when chain reads fail
                override_quote_dec = os.getenv("QUOTE_TOKEN_DECIMALS")
                if override_quote_dec:
                    try:
                        quote_dec_in = int(override_quote_dec)
                    except Exception:
                        pass
                if quote_dec_in == 18:
                    token_lower = str(rev_tokens[0]).lower()
                    stable_addr = (
                        (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                    )
                    usdc_addr = (os.getenv("USDC_TOKEN_ADDRESS") or "").strip().lower()
                    if token_lower in {stable_addr, usdc_addr}:
                        quote_dec_in = 6
                if quote_dec_in <= 0:
                    quote_dec_in = 6
                quote_price = 1.0  # Assume USD stable quote token

                # Bound input by USD cap and desired output
                desired_diem_tokens = max(
                    0.0, float(desired_units_out) / float(10**diem_decimals)
                )
                max_in_from_cap = max(
                    0.0, max_usd * float(10**quote_dec_in) / quote_price
                )
                max_in_for_desired = (
                    desired_diem_tokens
                    * market_price
                    * float(10**quote_dec_in)
                    / quote_price
                    if desired_diem_tokens > 0
                    else max_in_from_cap
                )
                max_quote_in = int(min(max_in_from_cap, max_in_for_desired))

                if max_quote_in <= 0:
                    logger.debug("Exact-in fallback: max_quote_in <= 0, skipping")
                    last_failure_reason = "insufficient_size"
                    continue

                # Start with max_units, reduce if needed
                test_units = max_quote_in
                quote = None

                route_tokens = (
                    list(rev_route.tokens) if hasattr(rev_route, "tokens") else []
                )
                logger.debug(
                    f"Exact-in fallback: trying route {route_tokens}, max_quote_in={max_quote_in}"
                )

                # Try progressively smaller amounts if first attempt fails
                for attempt in range(3):
                    if test_units <= 0:
                        break
                    try:
                        quote = self.diem.aggregator.best_quote(test_units, rev_route)  # type: ignore[attr-defined]
                    except Exception as e:
                        logger.debug(f"Exact-in fallback: best_quote exception: {e}")
                        quote = None
                    if quote is not None and quote.amount_out > 0:
                        logger.debug(
                            f"Exact-in fallback: quote SUCCESS route={route_tokens}, "
                            f"in={quote.amount_in}, out={quote.amount_out}"
                        )
                        break
                    # Reduce by half for next attempt
                    test_units = max(0, test_units // 2)

                if quote is None or quote.amount_out <= 0:
                    logger.debug(
                        f"Exact-in fallback: quote unavailable for route {route_tokens}"
                    )
                    last_failure_reason = "quote_unavailable"
                    continue

                # Calculate effective execution price and slippage
                try:
                    if quote.amount_in <= 0:
                        last_failure_reason = "quote_zero_input"
                        continue

                    raw_ratio = quote.amount_out / float(quote.amount_in)
                    if raw_ratio <= 0:
                        last_failure_reason = "quote_zero_output"
                        continue
                    decimal_factor = float(10 ** max(0, diem_decimals - quote_dec_in))
                    adjusted_factor = decimal_factor
                    if raw_ratio <= 1.0 and decimal_factor > 1.0:
                        adjusted_factor = min(decimal_factor, 25.0)

                    exec_price = (adjusted_factor / raw_ratio) * quote_price

                    composite_slip = None
                    try:
                        composite_slip = getattr(quote, "_composite_slippage_bps", None)
                    except Exception:
                        composite_slip = None

                    slip_check = self._check_slippage_buy(
                        exec_price,
                        market_price,
                        precomputed_slippage_bps=composite_slip,
                    )

                    # Skip if slippage check indicates quote failure
                    if slip_check.get("quote_failure", False):
                        logger.debug(
                            "Exact-in fallback: quote failure detected for route"
                        )
                        last_failure_reason = "quote_failure"
                        continue

                    slippage_bps = float(
                        slip_check.get("slippage_bps", float("inf"))
                        if isinstance(slip_check, dict)
                        else float("inf")
                    )

                    # Check if slippage is acceptable (composite routes now respect caps).
                    is_composite_route_flag = False
                    try:
                        from libs.dex.composite import is_composite_route as _is_comp

                        is_composite_route_flag = bool(_is_comp(route))
                    except Exception:
                        is_composite_route_flag = False

                    composite_slippage_cap = max_slippage_bps
                    try:
                        override = os.getenv("DIEM_COMPOSITE_MAX_SLIPPAGE_BPS")
                        if override:
                            composite_slippage_cap = float(override)
                    except Exception:
                        composite_slippage_cap = max_slippage_bps

                    applied_slippage_cap = (
                        composite_slippage_cap
                        if is_composite_route_flag
                        else max_slippage_bps
                    )

                    if applied_slippage_cap > 0 and slippage_bps > applied_slippage_cap:
                        logger.debug(
                            f"Exact-in fallback: slippage {slippage_bps} bps exceeds cap {applied_slippage_cap} "
                            f"(composite={is_composite_route_flag}, simulate={simulate})"
                        )
                        if not (simulate and is_composite_route_flag):
                            last_failure_reason = "slippage_exceeded"
                            continue

                    # Check pool-take limits if available
                    try:
                        md = self._market_provider()
                        pool_take_bps = int(getattr(self.risk, "pool_take_bps_cap", 25))
                        reserve_cap = md.reserve_cap_units(
                            rev_route, take_bps=pool_take_bps
                        )
                        if reserve_cap is not None and reserve_cap > 0:
                            if quote.amount_out > reserve_cap:
                                logger.debug(
                                    f"Exact-in fallback: quote amount {quote.amount_out} exceeds reserve cap {reserve_cap}"
                                )
                                last_failure_reason = "pool_take_limit"
                                continue
                    except Exception:
                        # Pool-take check is optional, continue if it fails
                        pass

                    # Success - return adjusted units and slippage
                    adjusted_units = int(quote.amount_out)
                    logger.info(
                        f"Exact-in fallback succeeded: units={adjusted_units}, slippage_bps={slippage_bps:.2f}, provider={quote.provider}"
                    )
                    self._last_exact_in_fallback_reason = None
                    # Cache the successful quote for reuse in simulate=False execution
                    self._cached_fallback_quote = {
                        "quote": quote,
                        "ts": time.time(),
                        "units": adjusted_units,
                        "bps": slippage_bps,
                        "provider": quote.provider,
                        "market_price": market_price,
                    }
                    return (adjusted_units, slippage_bps, quote)

                except Exception as exc:
                    logger.debug(f"Exact-in fallback: error processing quote: {exc}")
                    last_failure_reason = "quote_error"
                    continue

            except Exception:
                continue

        self._last_exact_in_fallback_reason = last_failure_reason
        return None

    def evaluate_and_maybe_mint(
        self,
        market_price: float,
        mint_rate: float | None = None,
        mint_rate_source: str | None = None,
        desired_units: int | None = None,
        current_inventory_usd: float | None = None,
        utilization_ratio: float | None = None,
        vol_bps: float | None = None,
        corr_id: str | None = None,
        simulate: bool = False,
        portfolio_snapshot: dict | InventorySnapshot | None = None,
        recovery_only: bool = False,
    ) -> bool:
        # Track consecutive slippage-based holds for adaptive cap widening.
        self._update_slippage_hold_streak()
        if simulate:
            # Pending recovery actions are scoped to the most recent simulation pass.
            self._pending_recovery_action = None
        # Import lazily so tests can monkeypatch libs.pricing.diem
        pricing_module = import_module("libs.pricing.diem")
        fair_value_per_diem = pricing_module.fair_value_per_diem

        vvv_price = self._current_vvv_price()
        mint_rate_info = self._resolve_mint_rate_for_decision(
            mint_rate_arg=mint_rate,
            mint_rate_source_arg=mint_rate_source,
            simulate=bool(simulate),
        )
        mint_rate_svvv_per_diem = mint_rate_info.get("mint_rate_svvv_per_diem")
        if mint_rate_svvv_per_diem is None or not math.isfinite(
            float(mint_rate_svvv_per_diem)
        ):
            mint_rate_svvv_per_diem = 1.0
            mint_rate_info = dict(mint_rate_info)
            mint_rate_info.update(
                {
                    "mint_rate_svvv_per_diem": 1.0,
                    "mint_rate_source": "env_override",
                    "mint_rate_source_detail": "fallback_default",
                }
            )
        mint_rate_svvv_per_diem = float(mint_rate_svvv_per_diem)

        # Capture wallet inventory once for this decision and reuse everywhere
        inv: InventorySnapshot | None = None
        if isinstance(portfolio_snapshot, InventorySnapshot):
            inv = portfolio_snapshot
        elif isinstance(portfolio_snapshot, dict):
            inv = InventorySnapshot(raw=portfolio_snapshot)
        else:
            try:
                inv = InventorySnapshot.capture(include_eth=False)
            except Exception:
                inv = None
        exec_ctx = ExecutionContext(inventory=inv)
        wallet_balances = exec_ctx.balances

        supply_snapshot = {}
        try:
            supply_snapshot = self.diem.get_circulating_supply(ttl_s=600)
        except Exception:
            supply_snapshot = {}
        circulating_supply = None
        if isinstance(supply_snapshot, dict):
            supply_val = supply_snapshot.get("supply")
            if supply_val is not None:
                try:
                    circulating_supply = float(supply_val)
                except Exception:
                    circulating_supply = None
            else:
                raw_val = supply_snapshot.get("raw")
                decimals_val = supply_snapshot.get("decimals")
                try:
                    if raw_val is not None and decimals_val is not None:
                        circulating_supply = float(raw_val) / float(
                            10 ** int(decimals_val)
                        )
                except Exception:
                    circulating_supply = None

        util_value = None
        if utilization_ratio is not None:
            try:
                util_value = float(utilization_ratio)
                self._util_history.append(util_value)
                if len(self._util_history) > 6:
                    self._util_history.pop(0)
            except Exception:
                util_value = None
        util_trend = None
        if self._util_history:
            util_trend = sum(self._util_history) / len(self._util_history)

        ratio_vs_history = None
        history_len = len(self._ratio_history)
        min_history_required = 10
        if vvv_price > 0 and self._run_mode != "dry":
            try:
                current_ratio = float(market_price) / float(vvv_price)
                if math.isfinite(current_ratio) and current_ratio > 0:
                    self._ratio_history.append(current_ratio)
                    if len(self._ratio_history) > 20:
                        self._ratio_history.pop(0)
                    if len(self._ratio_history) - 1 >= min_history_required:
                        history_values = self._ratio_history[:-1]
                        history_len = len(history_values)
                        history_avg = sum(self._ratio_history[:-1]) / (
                            len(self._ratio_history) - 1
                        )
                        if history_avg > 0:
                            raw_ratio = current_ratio / history_avg
                            bounded_ratio = max(0.5, min(2.0, raw_ratio))
                            if not math.isclose(raw_ratio, bounded_ratio, rel_tol=1e-6):
                                logger.debug(
                                    "Clamped ratio_vs_history",
                                    extra={
                                        "raw_ratio": raw_ratio,
                                        "bounded_ratio": bounded_ratio,
                                        "history_len": history_len,
                                    },
                                )
                            ratio_vs_history = bounded_ratio
                    else:
                        logger.debug(
                            "Insufficient ratio history for sentiment adjustment",
                            extra={
                                "history_len": max(len(self._ratio_history) - 1, 0),
                                "min_required": min_history_required,
                            },
                        )
            except Exception:
                ratio_vs_history = None
        elif self._run_mode == "dry" and self._ratio_history:
            # Ensure dry-run cycles do not contaminate live history
            logger.debug(
                "Skipping ratio history update while in dry-run mode",
                extra={"history_len": len(self._ratio_history)},
            )

        # Determine if on-chain liquidity exists for illiquidity discount
        has_onchain_liquidity = True
        md = None
        try:
            md = self._market_provider()
        except Exception:
            md = None

        if md is not None:
            try:
                price_health = md.price_health("DIEM")
                source = price_health.get("source", "")
                # On-chain sources: aggregator, pools
                # Off-chain sources: bridge_vvv, external_reference
                if source in ("bridge_vvv", "external_reference"):
                    has_onchain_liquidity = False
            except Exception:
                pass

        fair_value_result = fair_value_per_diem(
            vvv_price=vvv_price,
            mint_rate=mint_rate_svvv_per_diem,
            emissions_penalty=0.20,
            utilization_current=util_value,
            utilization_trend=util_trend,
            circulating_supply=circulating_supply,
            target_supply=self._target_supply(),
            discount_rate_apy=self.discount_rate_apy,
            growth_rate_apy=0.05,
            historical_ratio=ratio_vs_history,
            has_onchain_liquidity=has_onchain_liquidity,
            market_price=market_price,
        )

        if isinstance(fair_value_result, dict):
            fair_value = float(fair_value_result.get("fair_value", 0.0))
            fv_components = fair_value_result.get("components", {}) or {}
            fv_confidence = fair_value_result.get("confidence")
        else:
            fair_value = float(fair_value_result)
            fv_components = {}
            fv_confidence = None

        logger.info(
            "Market px=%.4f, fair=%.4f (vvv=%.4f, mint_rate=%.2f, util=%s, conf=%.0f%%)",
            market_price,
            fair_value,
            vvv_price,
            mint_rate_svvv_per_diem,
            _utilization_log_value(utilization_ratio),
            (float(fv_confidence) * 100.0) if fv_confidence is not None else 0.0,
        )
        if fv_components:
            logger.debug("Fair value components: %s", fv_components)
        # Premium/discount thresholds are policy defaults (RiskPolicy) unless explicitly overridden via env.
        # DIEM_PREMIUM_THRESHOLD and DIEM_DISCOUNT_THRESHOLD are read by RiskPolicy.from_env().
        try:
            threshold_mult = float(self.risk.premium_trigger())
        except Exception:
            threshold_mult = 1.05
        try:
            discount_mult = float(self.risk.discount_trigger())
        except Exception:
            discount_mult = threshold_mult
        # Initialize rationale holder
        # Extract has_onchain_liquidity from fair_value_components if available
        has_onchain_liquidity_init = True
        if isinstance(fv_components, dict):
            has_onchain_liquidity_init = fv_components.get(
                "has_onchain_liquidity", True
            )

        rationale = {
            "market_price": float(market_price),
            "fair_value": float(fair_value),
            "threshold_mult": float(threshold_mult),
            "discount_mult": float(discount_mult),
            "premium": (float(market_price / fair_value) if fair_value > 0 else None),
            "mint_rate": float(mint_rate_svvv_per_diem),
            # Explicit diagnostic aliases for auditability (used by orchestrator `why` block).
            "mint_rate_svvv_per_diem": float(mint_rate_svvv_per_diem),
            "mint_rate_source": str(
                mint_rate_info.get("mint_rate_source") or "unknown"
            ),
            "mint_rate_source_detail": str(
                mint_rate_info.get("mint_rate_source_detail") or "unknown"
            ),
            "mint_rate_units_per_diem_unit": mint_rate_info.get(
                "mint_rate_units_per_diem_unit"
            ),
            "mint_rate_env_override_present": bool(
                mint_rate_info.get("mint_rate_env_override_present", False)
            ),
            "desired_units": None,
            "suggested_units": None,
            "exec_price_preview": None,
            "slippage_bps": None,
            "slippage_ok": None,
            "slippage_hold_streak": int(self._slippage_hold_streak),
            "slippage_override_enabled": bool(self._slippage_override_enabled()),
            "decision": "hold",
            "reason": None,
            "has_onchain_liquidity": has_onchain_liquidity_init,
            "fair_value_components": (
                fv_components if isinstance(fv_components, dict) else {}
            ),
            "fair_value_confidence": (
                float(fv_confidence) if fv_confidence is not None else None
            ),
            "circulating_supply": (
                float(circulating_supply) if circulating_supply is not None else None
            ),
            "price_ratio_vs_history": (
                float(ratio_vs_history) if ratio_vs_history is not None else None
            ),
            "input_token_balance": self._get_input_token_info(),
            "wallet_inventory": wallet_balances,
        }
        price_ratio = rationale.get("premium")
        dynamic_cap = float(
            self.risk._compute_dynamic_slippage_cap(
                price_ratio=price_ratio, liquidity_slippage_bps=None
            )
        )
        # Clamp dynamic cap to risk policy's max_slippage_bps to ensure orchestrator-level limits are respected
        risk_max_bps = float(self.risk.slippage_bps_cap)
        slippage_cap_bps = min(dynamic_cap, risk_max_bps)
        rationale.update(
            {
                "slippage_cap_bps": float(slippage_cap_bps),
                "slippage_cap_bps_base": float(slippage_cap_bps),
            }
        )
        if fair_value <= 0:
            rationale.update({"decision": "hold", "reason": "invalid_fair"})
            self._last_rationale = rationale
            return False

        if recovery_only:
            cap = float(self._locked_svvv_ratio_cap())
            svvv_lock_state: dict[str, Any] | None = None
            if cap < 1.0:
                min_total_units = int(self._locked_svvv_ratio_min_total_units())
                svvv_lock_state = self._svvv_lock_state(wallet_balances)
                locked_ratio = (
                    svvv_lock_state.get("locked_ratio")
                    if isinstance(svvv_lock_state, dict)
                    else None
                )
                total_units = (
                    int((svvv_lock_state.get("total_units") or 0) or 0)
                    if isinstance(svvv_lock_state, dict)
                    else 0
                )
                if locked_ratio is not None and total_units >= min_total_units:
                    recovered = self._maybe_capacity_recovery(
                        market_price=float(market_price),
                        fair_value=float(fair_value),
                        threshold_mult=float(threshold_mult),
                        mint_rate=float(mint_rate_svvv_per_diem),
                        slippage_cap_bps=float(slippage_cap_bps),
                        pool_take_bps=None,
                        corr_id=corr_id,
                        simulate=bool(simulate),
                        exec_ctx=exec_ctx,
                        svvv_lock_state=svvv_lock_state,
                        rationale=rationale,
                        utilization_ratio=utilization_ratio,
                        vol_bps=vol_bps,
                        current_inventory_usd=current_inventory_usd,
                        vvv_price_usd=float(vvv_price),
                    )
                    self._last_rationale = rationale
                    return bool(recovered)
            rationale.update({"decision": "hold", "reason": "recovery_only_no_action"})
            self._last_rationale = rationale
            return False

        if market_price > fair_value * threshold_mult:  # premium over threshold
            cap = float(self._locked_svvv_ratio_cap())
            svvv_lock_state: dict[str, Any] | None = None
            if cap < 1.0:
                min_total_units = int(self._locked_svvv_ratio_min_total_units())
                svvv_lock_state = self._svvv_lock_state(wallet_balances)
                locked_ratio = (
                    svvv_lock_state.get("locked_ratio")
                    if isinstance(svvv_lock_state, dict)
                    else None
                )
                total_units = int((svvv_lock_state.get("total_units") or 0) or 0)
                locked_ratio_f = None
                try:
                    locked_ratio_f = (
                        float(locked_ratio) if locked_ratio is not None else None
                    )
                except Exception:
                    locked_ratio_f = None

                if (
                    locked_ratio_f is not None
                    and locked_ratio_f > cap
                    and total_units >= min_total_units
                ):
                    recovered = self._maybe_capacity_recovery(
                        market_price=float(market_price),
                        fair_value=float(fair_value),
                        threshold_mult=float(threshold_mult),
                        mint_rate=float(mint_rate_svvv_per_diem),
                        slippage_cap_bps=float(slippage_cap_bps),
                        pool_take_bps=None,
                        corr_id=corr_id,
                        simulate=bool(simulate),
                        exec_ctx=exec_ctx,
                        svvv_lock_state=svvv_lock_state,
                        rationale=rationale,
                        utilization_ratio=utilization_ratio,
                        vol_bps=vol_bps,
                        current_inventory_usd=current_inventory_usd,
                        vvv_price_usd=float(vvv_price),
                    )
                    if recovered:
                        self._last_rationale = rationale
                        return True

            # Risk-gated sizing (utilization/vol-aware if available)
            want = (
                int(desired_units)
                if desired_units is not None
                else self._desired_units(market_price)
            )
            # For mint/sell, we're selling DIEM, so balance constraint doesn't apply
            # (we're minting DIEM from locked sVVV, not buying with USDC)
            # Optional pool reserve cap (best-effort)
            reserve_cap_units: int | None = None
            pool_take_bps: int | None = None
            try:
                md = self._market_provider()
                routes = self._trade_routes()
                path = routes[0] if routes else None
                try:
                    pool_take_bps = int(getattr(self.risk, "pool_take_bps_cap", 25))
                except Exception:
                    pool_take_bps = 25
                allow_reserve_cap = True
                try:
                    # Unit tests should not depend on networked reserve discovery unless an
                    # explicit market stub is provided.
                    if os.getenv("PYTEST_CURRENT_TEST") and self.market is None:
                        allow_reserve_cap = False
                except Exception:
                    allow_reserve_cap = True
                if path and allow_reserve_cap:
                    cap_raw = md.reserve_cap_units(path, take_bps=pool_take_bps)
                    if cap_raw is not None:
                        try:
                            reserve_cap_units = max(0, int(cap_raw))
                        except Exception:
                            reserve_cap_units = None
            except Exception:
                reserve_cap_units = None
            try:
                suggested = self.risk.size_with_risk(
                    want,
                    market_price,
                    current_inventory_usd=current_inventory_usd,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                )
            except Exception:
                suggested = self.risk.suggest_trade_units(
                    want, market_price, current_inventory_usd
                )
            base_suggested = max(0, int(suggested))
            final_suggested = base_suggested
            if reserve_cap_units is not None:
                final_suggested = min(base_suggested, reserve_cap_units)
            rationale.update(
                {
                    "desired_units": int(want),
                    "suggested_units": int(base_suggested),
                    "reserve_cap_units": (
                        int(reserve_cap_units)
                        if reserve_cap_units is not None
                        else None
                    ),
                    "pool_take_bps": (
                        int(pool_take_bps) if pool_take_bps is not None else None
                    ),
                    "utilization_ratio": (
                        float(utilization_ratio)
                        if utilization_ratio is not None
                        else None
                    ),
                    "utilization_trend": (
                        float(util_trend) if util_trend is not None else None
                    ),
                    "vol_bps": (float(vol_bps) if vol_bps is not None else None),
                    "current_inventory_usd": (
                        float(current_inventory_usd)
                        if current_inventory_usd is not None
                        else None
                    ),
                }
            )
            if final_suggested != base_suggested:
                rationale.update({"reserve_capped_units": int(final_suggested)})
            suggested = int(final_suggested)
            execution_target_units = int(suggested)
            rationale.update({"portfolioAdjustedUnits": int(suggested)})
            if suggested <= 0:
                logger.info("Risk rejected mint/trade (suggested=0)")
                rationale.update({"decision": "hold", "reason": "risk_rejected"})
                self._last_rationale = rationale
                return False
            wallet_diem_units = int((wallet_balances.get("DIEM") or {}).get("units", 0))
            wallet_sell_units = min(wallet_diem_units, suggested)
            minted_shortfall = max(0, suggested - wallet_sell_units)
            rationale.update(
                {
                    "wallet_diem_units": wallet_diem_units,
                    "wallet_sell_units": int(wallet_sell_units),
                    "mint_shortfall_units": int(minted_shortfall),
                }
            )

            # If minting is unavailable (latched by DIEMService), switch to a spot-only
            # inventory sell path. We only sell wallet DIEM and never attempt mint+sell.
            mint_unavailable = False
            try:
                if hasattr(self.diem, "mint_unavailable_latched"):
                    mint_unavailable = bool(self.diem.mint_unavailable_latched())
                else:
                    mint_unavailable = bool(
                        getattr(self.diem, "_mint_unavailable", False)
                    )
            except Exception:
                mint_unavailable = False
            if mint_unavailable:
                rationale["mint_unavailable_latched"] = True
                spot_units = int(wallet_sell_units)
                if spot_units <= 0:
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "mint_unavailable_no_wallet_inventory",
                        }
                    )
                    self._last_rationale = rationale
                    return False
                try:
                    sell_intent = ExecutionIntent(
                        side=TradeSide.SELL,
                        token_in="DIEM",
                        token_out="USDC",
                        amount_base_units=int(spot_units),
                        slippage_bps=int(slippage_cap_bps),
                        pool_take_bps=pool_take_bps,
                        preferred_route=routes[0] if routes else None,
                        metadata={
                            "correlation_id": corr_id,
                            "decision": "spot_sell",
                            "mint_unavailable": True,
                            "diem_market_price_usd": float(market_price),
                        },
                    )
                except Exception as exc:
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "intent_error",
                            "error": str(exc),
                        }
                    )
                    self._last_rationale = rationale
                    return False

                try:
                    preview_result = self.diem.preview_trade(sell_intent)
                    rationale["execution_preview"] = preview_result.as_dict()
                    if preview_result.slippage_bps is not None:
                        rationale["slippage_bps"] = float(preview_result.slippage_bps)
                    if preview_result.effective_price is not None:
                        rationale["exec_price_preview"] = float(
                            preview_result.effective_price
                        )
                except Exception as exc:
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "preview_failed",
                            "error": str(exc),
                        }
                    )
                    self._last_rationale = rationale
                    return False

                logger.info(
                    f"Signal: Spot-sell DIEM inventory (units={spot_units}) simulate={simulate} mint_unavailable=True"
                )
                rationale.update(
                    {
                        "decision": "spot_sell",
                        "units": int(spot_units),
                        "want_units": int(want),
                    }
                )
                _metrics_inc(
                    "agent_decisions_total",
                    labels={"agent": "arbi_diem", "action": "spot_sell"},
                )
                if not simulate:
                    try:
                        exec_result = self.diem.execute_trade(
                            sell_intent, simulate=False
                        )
                        rationale["execution"] = {
                            "status": exec_result.status.value,
                            "sell": exec_result.as_dict(),
                        }
                        if exec_result.status in (
                            ExecutionStatus.REJECTED,
                            ExecutionStatus.FAILED,
                        ):
                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": "execution_failed",
                                    "execution_error": exec_result.error,
                                    "execution_diagnostics": exec_result.diagnostics,
                                }
                            )
                            self._last_rationale = rationale
                            return False
                    except Exception as exc:
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "execution_exception",
                                "execution_error": str(exc),
                            }
                        )
                        self._last_rationale = rationale
                        return False
                else:
                    rationale["execution"] = {
                        "status": ExecutionStatus.SIMULATED.value,
                        "sell": rationale.get("execution_preview"),
                    }
                self._last_rationale = rationale
                return True

            preview_px = self._preview_exec_price(suggested)
            guard_liquidity = (self.diem.aggregator is not None) and not simulate
            # Check if we have a valid price source even without on-chain liquidity
            price_source_valid = False
            try:
                md = self._market_provider()
                health = md.price_health("DIEM")
                source = health.get("source", "")
                # Accept bridge_vvv pricing when no DEX liquidity exists
                if source in ("bridge_vvv", "aggregator"):
                    price_source_valid = True
            except Exception:
                pass
            if guard_liquidity and preview_px <= 0 and not price_source_valid:
                logger.info("Mint and sell skipped: no liquidity preview available")
                rationale.update({"decision": "hold", "reason": "no_liquidity_preview"})
                self._last_rationale = rationale
                return False

            # Liquidity-aware adaptive slippage cap (opt-in via DIEM_SLIPPAGE_OVERRIDE_ENABLE).
            try:
                base_cap = float(slippage_cap_bps)
            except Exception:
                base_cap = float(rationale.get("slippage_cap_bps") or 0.0)
            trade_usd = None
            try:
                if suggested > 0:
                    trade_usd = float(
                        self.risk.usd_from_units(int(suggested), float(market_price))
                    )
            except Exception:
                trade_usd = None
            depth_usd = None
            try:
                if reserve_cap_units is not None and int(reserve_cap_units) > 0:
                    depth_usd = float(
                        self.risk.usd_from_units(
                            int(reserve_cap_units), float(market_price)
                        )
                    )
            except Exception:
                depth_usd = None
            route_health = None
            try:
                route_health = self._route_health(routes[0] if routes else None)
            except Exception:
                route_health = None
            adaptive_cap, adaptive_meta = self._adaptive_slippage_cap_bps(
                float(base_cap),
                trade_usd=trade_usd,
                liquidity_depth_usd=depth_usd,
                route_health=route_health,
            )
            if adaptive_meta.get("applied"):
                slippage_cap_bps = float(adaptive_cap)
                rationale.update(
                    {
                        "slippage_cap_bps": float(slippage_cap_bps),
                        "slippage_cap_adaptive": adaptive_meta,
                    }
                )

            adjusted, last_bps, initial_bps = self._adjust_for_liquidity(
                suggested,
                market_price,
                preview_price=preview_px if preview_px > 0 else None,
                slippage_cap_bps=slippage_cap_bps,
                include_initial_bps=True,
            )
            rationale.update(
                {
                    "price_deviation_bps_initial": (
                        float(initial_bps) if initial_bps is not None else None
                    ),
                    "liquidity_downsized": bool(int(adjusted) != int(suggested)),
                }
            )
            final_preview = (
                self._preview_exec_price(adjusted) if adjusted > 0 else preview_px
            )
            if final_preview and final_preview > 0:
                rationale["exec_price_preview"] = float(final_preview)
            else:
                rationale["exec_price_preview"] = (
                    float(preview_px) if preview_px and preview_px > 0 else None
                )
            if last_bps is not None:
                soft_cap = self._slippage_soft_cap_bps()
                rationale.update(
                    {
                        "slippage_bps": float(last_bps),
                        "slippage_ok": bool(last_bps <= float(slippage_cap_bps)),
                        "slippage_soft_cap_bps": (
                            float(soft_cap) if soft_cap is not None else None
                        ),
                        "liquidity_adjustment_config": {
                            "max_steps": self._liquidity_max_adjust_steps(),
                            "min_trade_usd": self._liquidity_min_trade_usd(),
                        },
                    }
                )
            # If adjusted dropped to zero due to slippage, hold
            if adjusted <= 0:
                logger.info("Rejected due to liquidity/slippage after adjustment")
                rationale.update({"decision": "hold", "reason": "slippage_exceeded"})
                self._last_rationale = rationale
                return False
            # Track trade route for telemetry
            trade_route_str = None
            if routes and routes[0]:
                try:
                    route_tokens = (
                        routes[0].tokens if hasattr(routes[0], "tokens") else []
                    )
                    trade_route_str = (
                        "->".join(str(t) for t in route_tokens)
                        if route_tokens
                        else None
                    )
                except Exception:
                    pass

            # Record the post-liquidity size for observability even when unchanged
            rationale.update({"liquidity_adjusted_units": int(adjusted)})
            if adjusted != suggested:
                rationale.update(
                    {"portfolioAdjustedUnits": int(execution_target_units)}
                )
            else:
                rationale.update(
                    {"portfolioAdjustedUnits": int(execution_target_units)}
                )

            if trade_route_str:
                rationale.update({"tradeRoute": trade_route_str})
            if routes and routes[0] is not None:
                route_meta = getattr(routes[0], "_metadata", None)
                if isinstance(route_meta, dict) and route_meta:
                    rationale.update({"tradeRouteMeta": route_meta})

            suggested = adjusted
            mint_needed = max(0, int(suggested) - int(wallet_diem_units))
            rationale["mint_needed_units"] = int(mint_needed)
            mint_check: dict | None = None
            if mint_needed > 0:
                try:
                    mint_check = self.diem._can_mint(int(mint_needed))
                except Exception as exc:
                    logger.debug(f"_can_mint check failed: {exc}")
            if mint_check is not None:
                rationale["mint_check"] = mint_check
                rationale["mint_needed_units"] = int(mint_needed)
                rationale["mint_rate"] = float(mint_rate_svvv_per_diem)
                rationale["mint_rate_svvv_per_diem"] = float(mint_rate_svvv_per_diem)
                rationale["mint_rate_source"] = str(
                    mint_rate_info.get("mint_rate_source") or "unknown"
                )
                rationale["mint_rate_source_detail"] = str(
                    mint_rate_info.get("mint_rate_source_detail") or "unknown"
                )
                rationale["mint_rate_units_per_diem_unit"] = mint_rate_info.get(
                    "mint_rate_units_per_diem_unit"
                )
                available_svvv = mint_check.get("available_svvv")
                required_svvv = mint_check.get("required_svvv")
                if available_svvv is not None and mint_check.get("can_mint") is False:
                    mint_reason = str(mint_check.get("reason") or "").strip().lower()
                    if mint_reason and mint_reason != "insufficient_svvv":
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": mint_reason,
                            }
                        )
                        self._last_rationale = rationale
                        return False
                    stake_rec = self._build_stake_recommendation(
                        mint_needed_units=mint_needed,
                        mint_check=mint_check,
                        mint_rate=float(mint_rate_svvv_per_diem),
                        corr_id=corr_id,
                    )
                    deficit = None
                    try:
                        deficit = max(0, int(required_svvv) - int(available_svvv))
                    except Exception:
                        deficit = None
                    rec_payload: dict[str, Any] = (
                        dict(stake_rec) if isinstance(stake_rec, dict) else {}
                    )
                    if deficit is not None:
                        rec_payload.setdefault("shortfall_units", deficit)
                        rec_payload["requested_svvv_units"] = deficit
                    rec_payload.setdefault("reason", "insufficient_svvv")
                    rec_payload.setdefault("mint_needed_units", int(mint_needed))
                    rec_payload.setdefault("ts", time.time())
                    rationale["stake_recommendation"] = rec_payload
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "insufficient_svvv",
                        }
                    )
                    recovered = self._maybe_capacity_recovery(
                        market_price=float(market_price),
                        fair_value=float(fair_value),
                        threshold_mult=float(threshold_mult),
                        mint_rate=float(mint_rate_svvv_per_diem),
                        slippage_cap_bps=float(slippage_cap_bps),
                        pool_take_bps=pool_take_bps,
                        corr_id=corr_id,
                        simulate=bool(simulate),
                        exec_ctx=exec_ctx,
                        svvv_lock_state=svvv_lock_state,
                        rationale=rationale,
                        utilization_ratio=utilization_ratio,
                        vol_bps=vol_bps,
                        current_inventory_usd=current_inventory_usd,
                        vvv_price_usd=float(vvv_price),
                    )
                    self._last_rationale = rationale
                    return bool(recovered)
            # Call preview_trade for execution preview (both simulate and live modes)
            execution_preview = None
            preview_status_rejected = False
            if adjusted > 0:
                try:
                    sell_intent = ExecutionIntent(
                        side=TradeSide.SELL,
                        token_in="DIEM",
                        token_out="USDC",
                        amount_base_units=adjusted,
                        slippage_bps=int(slippage_cap_bps),
                        pool_take_bps=pool_take_bps,
                        preferred_route=routes[0] if routes else None,
                        metadata={
                            "correlation_id": corr_id,
                            "decision": "mint_sell",
                            "diem_market_price_usd": float(market_price),
                        },
                    )
                    preview_result = self.diem.preview_trade(sell_intent)
                    execution_preview = preview_result.as_dict()
                    preview_status_rejected = (
                        preview_result.status == ExecutionStatus.REJECTED
                    )
                    quote_summary = None
                    try:
                        diag = getattr(preview_result, "diagnostics", {}) or {}
                        if isinstance(diag, dict):
                            quote_summary = diag.get("quote_summary")
                    except Exception:
                        quote_summary = None
                    if quote_summary:
                        rationale["quote_summary"] = quote_summary
                        try:
                            rationale["executable_quote_count"] = quote_summary.get(
                                "executable_quote_count"
                            )
                            rationale["provider_errors"] = quote_summary.get(
                                "provider_errors"
                            )
                        except Exception:
                            pass
                    if (
                        not simulate
                        and quote_summary
                        and int(quote_summary.get("executable_quote_count") or 0) <= 0
                    ):
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "no_executable_quotes_pretrade",
                                "has_onchain_liquidity": False,
                            }
                        )
                        self._last_rationale = rationale
                        return False
                    # Update exec_price_preview from preview result if available
                    if preview_result.effective_price is not None:
                        rationale["exec_price_preview"] = float(
                            preview_result.effective_price
                        )
                    if preview_result.slippage_bps is not None:
                        rationale["slippage_bps"] = float(preview_result.slippage_bps)
                except Exception as exc:
                    logger.debug(f"preview_trade failed: {exc}")
            rationale["execution_preview"] = execution_preview
            # Check slippage policy before execution
            slippage_ok = last_bps is None or last_bps <= float(slippage_cap_bps)
            # Block execution if slippage exceeds cap or is extremely high (near 10,000 bps = effectively no depth)
            EXTREME_SLIPPAGE_THRESHOLD_BPS = 1000.0
            extreme_slippage = (
                last_bps is not None and last_bps >= EXTREME_SLIPPAGE_THRESHOLD_BPS
            )
            # Check if we have valid execution price preview (quotes available)
            # Also check execution_preview status - if preview_trade returned REJECTED, block execution
            has_valid_preview = (
                final_preview is not None
                and final_preview > 0
                and preview_px is not None
                and preview_px > 0
                and not preview_status_rejected
            )
            # Block execution if no valid quotes/preview available (unless in simulate mode)
            if not simulate and not has_valid_preview:
                logger.info(
                    "Mint and sell blocked: no valid execution price preview available"
                )
                # Check diagnostics for specific failure reasons
                reason = "no_execution_preview"
                if execution_preview:
                    diag = execution_preview.get("diagnostics", {})
                    failure_class = diag.get("failure_classification")
                    if failure_class == "diem_bridge_fallback_disabled":
                        reason = "no_executable_bridge_route_fallback_disabled"
                    elif failure_class == "diem_bridge_leg_failure":
                        reason = "no_executable_bridge_route_leg_failure"
                    elif failure_class == "diem_bridge_no_executable_route" or (
                        diag.get("bridge_route_available")
                        and not diag.get("bridge_quotes_found")
                    ):
                        reason = "no_executable_bridge_route"

                rationale.update(
                    {
                        "decision": "hold",
                        "reason": reason,
                        "slippage_bps": (
                            float(last_bps) if last_bps is not None else None
                        ),
                        "policy_checks": {"slippage_ok": slippage_ok},
                        "has_onchain_liquidity": False,
                    }
                )
                self._last_rationale = rationale
                return False
            # Block execution if slippage exceeds policy cap or is extreme
            if not simulate and (not slippage_ok or extreme_slippage):
                reason = (
                    "extreme_slippage"
                    if extreme_slippage
                    else "slippage_exceeded_policy"
                )
                # Distinguish DEX quote failures from legitimate market depth issues
                slippage_source = "unknown"
                if last_bps is not None:
                    if last_bps >= 9000.0:  # Near 10000 bps = likely quote failure
                        slippage_source = "quote_failure"
                    elif last_bps >= 1000.0:
                        slippage_source = "extreme_market_depth"
                    else:
                        slippage_source = "market_depth"
                logger.info(
                    f"Mint and sell blocked: {reason} (slippage_bps={last_bps}, cap={slippage_cap_bps}, source={slippage_source})"
                )
                rationale.update(
                    {
                        "decision": "hold",
                        "reason": reason,
                        "slippage_bps": (
                            float(last_bps) if last_bps is not None else None
                        ),
                        "slippage_source": slippage_source,
                        "policy_checks": {"slippage_ok": slippage_ok},
                    }
                )
                self._last_rationale = rationale
                return False
            # Structured decision log for mint/sell
            slippage_source = "unknown"
            if last_bps is not None:
                if last_bps >= 9000.0:
                    slippage_source = "quote_failure"
                elif last_bps >= 1000.0:
                    slippage_source = "extreme_market_depth"
                else:
                    slippage_source = "market_depth"
            execution_slippage_tolerance_bps = None
            try:
                execution_slippage_tolerance_bps = int(slippage_cap_bps)
            except Exception:
                execution_slippage_tolerance_bps = None
            decision_log = {
                "decision": "mint_sell",
                "units": int(suggested),
                "want_units": int(want),
                "price_deviation_bps_initial": rationale.get(
                    "price_deviation_bps_initial"
                ),
                "price_deviation_bps_final": float(last_bps)
                if last_bps is not None
                else None,
                "slippage_source": slippage_source,
                "slippage_cap_bps": float(slippage_cap_bps),
                "execution_slippage_tolerance_bps": execution_slippage_tolerance_bps,
                "liquidity_downsized": rationale.get("liquidity_downsized"),
                "policy_checks": {
                    "slippage_ok": slippage_ok,
                },
            }
            logger.info(
                f"Signal: Mint and sell DIEM (units={suggested}, want={want}) simulate={simulate} "
                f"price_deviation_bps_initial={rationale.get('price_deviation_bps_initial')} "
                f"price_deviation_bps_final={last_bps} "
                f"execution_slippage_tolerance_bps={execution_slippage_tolerance_bps} "
                f"downsized={rationale.get('liquidity_downsized')} "
                f"source={slippage_source}"
            )
            rationale.update(decision_log)
            _metrics_inc(
                "agent_decisions_total",
                labels={"agent": "arbi_diem", "action": "mint_sell"},
            )
            if not simulate:
                try:
                    gas_budget = self.diem.estimate_gas_budget_wei(include_swap=True)
                    eth_balance = self.diem._eth_balance_wei()
                    if (
                        gas_budget
                        and eth_balance is not None
                        and eth_balance < int(gas_budget.get("required_wei", 0))
                    ):
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "insufficient_gas_funds",
                                "execution_error": "insufficient_gas_funds",
                                "eth_balance_wei": int(eth_balance),
                                "gas_required_wei": int(
                                    gas_budget.get("required_wei", 0)
                                ),
                                "gas_price_wei": gas_budget.get("effective_gas_price"),
                            }
                        )
                        self._last_rationale = rationale
                        return False
                except Exception:
                    pass
                if not self._check_factory_registration():
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "factory_registration_missing",
                        }
                    )
                    self._last_rationale = rationale
                    return False
                pacing_check = self._pacing_check(action="mint_sell")
                rationale["mint_sell_pacing"] = pacing_check
                if not bool(pacing_check.get("ok", False)):
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": f"pacing_{pacing_check.get('reason') or 'blocked'}",
                        }
                    )
                    self._last_rationale = rationale
                    return False
                try:
                    now_ts = (pacing_check.get("pacing") or {}).get("now_ts")
                    self._pacing_record_action(
                        action="mint_sell",
                        now_ts=float(now_ts) if now_ts is not None else None,
                    )
                except Exception:
                    self._pacing_record_action(action="mint_sell")
                # Use wallet-first helper (falls back to DEX-first when disabled)
                try:
                    execution_result = self.diem.wallet_first_mint_and_sell(
                        diem_amount=suggested,
                        slippage_bps=int(slippage_cap_bps),
                        pool_take_bps=pool_take_bps,
                        simulate=False,
                        portfolio_snapshot=exec_ctx.snapshot,
                    )
                    # Record execution results in rationale
                    rationale["execution"] = execution_result
                    # Check if execution was successful
                    sell_result = (
                        execution_result.get("sell", {})
                        if isinstance(execution_result, dict)
                        else {}
                    )
                    overall_status = (
                        execution_result.get("status")
                        if isinstance(execution_result, dict)
                        else None
                    )
                    sell_status = "unknown"
                    if isinstance(sell_result, dict):
                        sell_status = sell_result.get(
                            "status", overall_status or "unknown"
                        )
                    if overall_status is None:
                        overall_status = sell_status
                    if overall_status in (
                        ExecutionStatus.REJECTED.value,
                        ExecutionStatus.FAILED.value,
                        "error",
                    ):
                        # ExecutionStatus values: simulated, submitted, confirmed, rejected, failed
                        # Also check for "error" which may come from exception handling
                        # Extract detailed error information for diagnostics
                        error_msg = sell_result.get("error", "unknown_error")
                        diagnostics = sell_result.get("diagnostics", {})

                        # Check if this is a liquidity error
                        is_liquidity_error = diagnostics.get(
                            "is_liquidity_error", False
                        )
                        # Also check error message patterns for liquidity issues
                        if not is_liquidity_error:
                            liquidity_keywords = [
                                "no executable",
                                "unhealthy",
                                "no pool",
                                "zero liquidity",
                                "revert",
                                "no quotes",
                                "all routes",
                            ]
                            is_liquidity_error = any(
                                keyword in str(error_msg).lower()
                                for keyword in liquidity_keywords
                            )

                        # Determine specific reason from diagnostics if available
                        if is_liquidity_error:
                            # Map liquidity errors to no_onchain_liquidity
                            reason = "no_onchain_liquidity"
                            # Update fair_value_components to reflect liquidity constraint
                            if "fair_value_components" in rationale:
                                fv_components = rationale.get(
                                    "fair_value_components", {}
                                )
                                if isinstance(fv_components, dict):
                                    fv_components["has_onchain_liquidity"] = False
                                    rationale["fair_value_components"] = fv_components
                            rationale["has_onchain_liquidity"] = False
                        else:
                            reason = f"execution_{overall_status}"
                            if diagnostics:
                                if diagnostics.get(
                                    "bridge_route_available"
                                ) and not diagnostics.get("bridge_quotes_found"):
                                    reason = "execution_rejected_bridge_no_quotes"
                                elif (
                                    diagnostics.get("quotes_attempted", 0) > 0
                                    and diagnostics.get("valid_quotes", 0) == 0
                                ):
                                    reason = "execution_rejected_no_valid_quotes"
                                elif "no_quotes" in str(error_msg).lower():
                                    reason = "execution_rejected_no_quotes"

                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": reason,
                                "execution_error": error_msg,
                                "execution_diagnostics": diagnostics,
                            }
                        )
                        self._last_rationale = rationale
                        return False
                except Exception as exc:
                    logger.error(f"mint_and_sell_diem failed: {exc}", exc_info=True)
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "execution_exception",
                            "execution_error": str(exc),
                        }
                    )
                    self._last_rationale = rationale
                    return False
            self._last_rationale = rationale
            return True
        # Discount branch: consider buy and burn when price is sufficiently below fair value
        if fair_value > 0 and market_price < (fair_value / discount_mult):
            mint_unavailable = False
            try:
                if hasattr(self.diem, "mint_unavailable_latched"):
                    mint_unavailable = bool(self.diem.mint_unavailable_latched())
                else:
                    mint_unavailable = bool(
                        getattr(self.diem, "_mint_unavailable", False)
                    )
            except Exception:
                mint_unavailable = False
            if mint_unavailable:
                rationale["mint_unavailable_latched"] = True
            # Require exact-out support from aggregator to enable buy/burn in v1
            try:
                aggregator = getattr(self.diem, "aggregator", None)
                use_exact_in_only = False
                use_exact_in_only = bool(aggregator) and (
                    not hasattr(aggregator, "best_quote_exact_out")
                )
                has_preview = bool(aggregator) and (
                    hasattr(aggregator, "best_quote_exact_out")
                    or hasattr(aggregator, "best_quote")
                )
                has_trade = bool(aggregator) and (
                    hasattr(aggregator, "trade_best_exact_out")
                    or hasattr(aggregator, "trade_best")
                )
            except Exception:
                has_preview = False
                has_trade = False
                use_exact_in_only = False
            if (not mint_unavailable) and (not has_preview):
                logger.info("Buy/burn skipped: exact-out preview unavailable")
                rationale.update({"decision": "hold", "reason": "no_liquidity_preview"})
                self._last_rationale = rationale
                return False
            if (not mint_unavailable) and (not simulate) and (not has_trade):
                logger.info("Buy/burn skipped: exact-out unsupported by aggregator")
                rationale.update({"decision": "hold", "reason": "no_liquidity_preview"})
                self._last_rationale = rationale
                return False
            # Check on-chain liquidity before attempting trades
            try:
                min_reserve_threshold = int(
                    os.getenv("DIEM_MIN_RESERVE_THRESHOLD", "1000000000000000000")
                )  # Default: 1 token (1e18)
                # Only enforce the on-chain liquidity guard in live mode; allow
                # simulations and dry-runs to progress so fallback logic can be tested.
                rpc_env_present = any(
                    os.getenv(var)
                    for var in (
                        "RPC_URL",
                        "BASE_RPC_URL",
                        "RPC_URLS",
                        "BASE_RPC_URLS",
                    )
                )
                # Respect exact-in/bridge fallback modes: when enabled we allow the
                # trade path logic to run even if on-chain reserves look thin so tests
                # can exercise the fallback behavior.
                try:
                    fallback_env_enabled = any(
                        os.getenv(flag, "0").strip().lower()
                        in {"1", "true", "yes", "on"}
                        for flag in (
                            "DIEM_EXACT_IN_FALLBACK_ENABLE",
                            "ARBI_DIEM_EXACT_IN_FALLBACK",
                            "DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY",
                        )
                    )
                except Exception:
                    fallback_env_enabled = False

                if not simulate and rpc_env_present and not fallback_env_enabled:
                    has_sufficient_liquidity = check_diem_vvv_liquidity_threshold(
                        min_reserve_out=min_reserve_threshold
                    )
                    if not has_sufficient_liquidity:
                        logger.info(
                            f"Buy/burn blocked: insufficient on-chain DIEM/VVV liquidity (threshold: {min_reserve_threshold})"
                        )
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "no_onchain_liquidity",
                                "liquidity_check": {
                                    "threshold": min_reserve_threshold,
                                    "has_sufficient": False,
                                },
                            }
                        )
                        # Fallback to stake-only recovery if locked ratio exceeds cap
                        cap = float(self._locked_svvv_ratio_cap())
                        if cap < 1.0:
                            try:
                                if wallet_balances.get("SVVV"):
                                    lock_state = self._svvv_lock_state(wallet_balances)
                                    locked_ratio = lock_state.get("locked_ratio")
                                    min_units = int(
                                        self._locked_svvv_ratio_min_total_units()
                                    )
                                    total_units = int(
                                        lock_state.get("total_units") or 0
                                    )
                                    if (
                                        locked_ratio is not None
                                        and float(locked_ratio) > cap
                                        and total_units >= min_units
                                    ):
                                        logger.info(
                                            f"Buy/burn blocked (no_onchain_liquidity), attempting stake-only recovery "
                                            f"(locked_ratio={float(locked_ratio) * 100:.1f}% > cap={cap * 100:.1f}%)"
                                        )
                                        threshold_mult = float(
                                            self.risk.premium_trigger()
                                        )
                                        slippage_cap_bps = float(
                                            self.risk.slippage_bps_cap
                                        )
                                        recovered = self._maybe_capacity_recovery(
                                            market_price=float(market_price),
                                            fair_value=float(fair_value),
                                            threshold_mult=float(threshold_mult),
                                            mint_rate=float(mint_rate_svvv_per_diem),
                                            slippage_cap_bps=float(slippage_cap_bps),
                                            pool_take_bps=None,
                                            corr_id=corr_id,
                                            simulate=bool(simulate),
                                            exec_ctx=exec_ctx,
                                            svvv_lock_state=lock_state,
                                            rationale=rationale,
                                            utilization_ratio=utilization_ratio,
                                            vol_bps=vol_bps,
                                            current_inventory_usd=current_inventory_usd,
                                            vvv_price_usd=float(vvv_price),
                                            force_stake_only=True,
                                        )
                                        if recovered:
                                            return True
                            except Exception as exc:
                                logger.debug(
                                    f"Stake-only recovery fallback failed: {exc}"
                                )
                        self._last_rationale = rationale
                        return False
                elif not simulate and rpc_env_present and fallback_env_enabled:
                    logger.debug(
                        "Skipping on-chain liquidity check: exact-in/bridge fallback enabled"
                    )
                elif not rpc_env_present:
                    logger.debug(
                        "Skipping on-chain liquidity check: no RPC endpoint configured"
                    )
            except Exception as exc:
                logger.debug(f"Liquidity check failed: {exc}")
                # Continue if check fails (don't block trades due to check errors)

            # Check locked sVVV eligibility to determine if burning is possible
            # If burn is blocked, we switch to spot-buy mode (accumulate DIEM at discount)
            burn_unavailable = False
            burnable_wallet_diem = 0
            try:
                locked_svvv = self.diem._locked_svvv_for_wallet()
                wallet_diem = int((exec_ctx.balances.get("DIEM") or {}).get("units", 0))
                # Use can_burn_diem to check actual burn eligibility, not just locked_svvv == 0
                # The wallet may have dust locked sVVV that's insufficient to burn any DIEM
                burn_eligibility = (
                    self.diem.can_burn_diem(wallet_diem) if wallet_diem > 0 else {}
                )
                can_burn = burn_eligibility.get("can_burn", False)
                burn_reason = burn_eligibility.get("reason", "")
                max_burnable = burn_eligibility.get("max_burnable_diem", 0)

                # Determine how much wallet DIEM is actually burnable
                if can_burn:
                    burnable_wallet_diem = wallet_diem
                elif burn_reason == "insufficient_locked_svvv" and max_burnable > 0:
                    burnable_wallet_diem = int(max_burnable)
                else:
                    burnable_wallet_diem = 0

                # Check if burn is blocked due to no/insufficient locked sVVV
                burn_blocked = (
                    wallet_diem > 0
                    and not can_burn
                    and burn_reason in ("no_locked_svvv", "insufficient_locked_svvv")
                )

                if burn_blocked:
                    burn_unavailable = True
                    rationale["burn_unavailable"] = True
                    rationale["burn_reason"] = burn_reason
                    rationale["locked_svvv"] = locked_svvv
                    rationale["burnable_wallet_diem"] = burnable_wallet_diem
                    rationale["burn_eligibility"] = burn_eligibility
                    logger.info(
                        "Burn unavailable: switching to spot-buy accumulation mode",
                        extra={
                            "wallet_diem": wallet_diem,
                            "burnable_wallet_diem": burnable_wallet_diem,
                            "locked_svvv": locked_svvv,
                            "burn_reason": burn_reason,
                        },
                    )
            except Exception as exc:
                logger.debug(f"Locked sVVV check failed: {exc}")

            # Check minimum input balance before committing to buy_burn
            min_input_balance_wei = int(
                os.getenv("DIEM_MIN_INPUT_BALANCE_WEI", "1000000")
            )  # Default: 1 USDC (6 decimals)
            input_info = self._get_input_token_info()
            input_balance = input_info.get("balance_wei", 0)
            enforce_input_balance = os.getenv(
                "DIEM_ENFORCE_INPUT_BALANCE", "0"
            ).strip().lower() in {"1", "true", "yes", "on"}
            if enforce_input_balance and input_balance < min_input_balance_wei:
                logger.info(
                    f"Buy/burn skipped: input token balance below minimum "
                    f"(balance={input_balance}, min={min_input_balance_wei})",
                    extra={
                        "agent": "arbi_diem",
                        "action": "buy_burn",
                        "balance_wei": input_balance,
                        "min_required_wei": min_input_balance_wei,
                        "token": input_info.get("token"),
                    },
                )
                rationale.update(
                    {
                        "decision": "hold",
                        "reason": "insufficient_input_balance",
                        "input_balance_wei": input_balance,
                        "min_required_wei": min_input_balance_wei,
                        "recommendation": f"Deposit at least {min_input_balance_wei / 1e6:.2f} USDC to enable buy_burn",
                    }
                )
                self._last_rationale = rationale
                return False

            available_usd = float(input_info.get("balance_usd", 0.0) or 0.0)
            min_trade_usd = None
            try:
                min_trade_raw = os.getenv("ARBI_DIEM_MIN_TRADE_USD") or os.getenv(
                    "ARBI_DIEM_MIN_NOTIONAL_USD"
                )
                if min_trade_raw not in (None, ""):
                    min_trade_usd = float(min_trade_raw)
            except Exception:
                min_trade_usd = None
            if min_trade_usd is not None and available_usd < min_trade_usd:
                logger.info(
                    "Buy/burn skipped: insufficient USDC balance for minimum trade",
                    extra={
                        "agent": "arbi_diem",
                        "action": "buy_burn",
                        "available_usd": float(available_usd),
                        "min_trade_usd": float(min_trade_usd),
                    },
                )
                rationale.update(
                    {
                        "decision": "hold",
                        "reason": "insufficient_balance",
                        "available_usd": float(available_usd),
                        "min_trade_usd": float(min_trade_usd),
                    }
                )
                self._last_rationale = rationale
                return False

            want = (
                int(desired_units)
                if desired_units is not None
                else self._desired_units(market_price)
            )
            # Constrain desired units by available USDC balance
            # Account for 10% slippage buffer applied at execution time plus ~3% price impact
            # to prevent trades that will be rejected for insufficient balance
            if available_usd > 0 and market_price > 0:
                # Reserve 15% headroom for slippage buffer (10%) + price impact (~3-5%)
                effective_usd = available_usd / 1.15
                max_units_from_balance = self.risk.units_from_usd(
                    effective_usd, market_price
                )
                if max_units_from_balance < want:
                    logger.info(
                        f"Buy trade constrained by available balance: "
                        f"desired={want} units, available=${available_usd:.2f} "
                        f"(effective=${effective_usd:.2f}, max_units={max_units_from_balance})",
                        extra={
                            "agent": "arbi_diem",
                            "action": "buy_burn",
                            "constraint": "balance",
                            "desired_units": int(want),
                            "available_usd": float(available_usd),
                            "effective_usd": float(effective_usd),
                            "max_units_from_balance": int(max_units_from_balance),
                        },
                    )
                    want = max_units_from_balance
            # Reserve cap for reversed path (QUOTE->...->DIEM)
            reserve_cap_units: int | None = None
            pool_take_bps: int | None = None
            # Fetch and filter routes by health - store filtered routes for later use
            all_routes = self._trade_routes()
            healthy_routes, unhealthy_routes = self._filter_routes_by_health(all_routes)
            if healthy_routes:
                routes = healthy_routes
                if unhealthy_routes:
                    logger.info(
                        f"Filtered out {len(unhealthy_routes)} unhealthy route(s), "
                        f"using {len(healthy_routes)} healthy route(s)",
                        extra={
                            "agent": "arbi_diem",
                            "action": "route_health_filter_applied",
                            "healthy_count": len(healthy_routes),
                            "unhealthy_count": len(unhealthy_routes),
                        },
                    )
            elif all_routes:
                # All routes unhealthy - block execution
                logger.info(
                    "Buy/burn blocked: all routes unhealthy (no_pool/zero_liquidity/revert)",
                    extra={
                        "agent": "arbi_diem",
                        "action": "route_health_block",
                        "total_routes": len(all_routes),
                        "unhealthy_count": len(unhealthy_routes),
                    },
                )
                rationale.update(
                    {
                        "decision": "hold",
                        "reason": "all_routes_unhealthy",
                        "unhealthy_routes_count": len(unhealthy_routes),
                    }
                )
                # Fallback to stake-only recovery if locked ratio exceeds cap
                cap = float(self._locked_svvv_ratio_cap())
                if cap < 1.0:
                    try:
                        if wallet_balances.get("SVVV"):
                            lock_state = self._svvv_lock_state(wallet_balances)
                            locked_ratio = lock_state.get("locked_ratio")
                            min_units = int(self._locked_svvv_ratio_min_total_units())
                            total_units = int(lock_state.get("total_units") or 0)
                            if (
                                locked_ratio is not None
                                and float(locked_ratio) > cap
                                and total_units >= min_units
                            ):
                                logger.info(
                                    f"Buy/burn blocked (all_routes_unhealthy), attempting stake-only recovery "
                                    f"(locked_ratio={float(locked_ratio) * 100:.1f}% > cap={cap * 100:.1f}%)"
                                )
                                threshold_mult = float(self.risk.premium_trigger())
                                slippage_cap_bps = float(self.risk.slippage_bps_cap)
                                recovered = self._maybe_capacity_recovery(
                                    market_price=float(market_price),
                                    fair_value=float(fair_value),
                                    threshold_mult=float(threshold_mult),
                                    mint_rate=float(mint_rate_svvv_per_diem),
                                    slippage_cap_bps=float(slippage_cap_bps),
                                    pool_take_bps=None,
                                    corr_id=corr_id,
                                    simulate=bool(simulate),
                                    exec_ctx=exec_ctx,
                                    svvv_lock_state=lock_state,
                                    rationale=rationale,
                                    utilization_ratio=utilization_ratio,
                                    vol_bps=vol_bps,
                                    current_inventory_usd=current_inventory_usd,
                                    vvv_price_usd=float(vvv_price),
                                    force_stake_only=True,
                                )
                                if recovered:
                                    return True
                    except Exception as exc:
                        logger.debug(f"Stake-only recovery fallback failed: {exc}")
                self._last_rationale = rationale
                return False
            else:
                routes = []

            try:
                md = self._market_provider()
                # When DIEM_BUY_DIRECT_ONLY=1, prefer direct USDC→DIEM route
                direct_buy = self._get_direct_buy_route_if_enabled()
                if direct_buy is not None:
                    path_buy = direct_buy
                else:
                    path_buy = routes[0].reversed() if routes else None
                try:
                    pool_take_bps = int(getattr(self.risk, "pool_take_bps_cap", 25))
                except Exception:
                    pool_take_bps = 25
                allow_reserve_cap = True
                try:
                    # Unit tests should not depend on networked reserve discovery unless an
                    # explicit market stub is provided.
                    if os.getenv("PYTEST_CURRENT_TEST") and self.market is None:
                        allow_reserve_cap = False
                except Exception:
                    allow_reserve_cap = True
                if path_buy and allow_reserve_cap:
                    cap_raw = md.reserve_cap_units(path_buy, take_bps=pool_take_bps)
                    if cap_raw is not None:
                        try:
                            reserve_cap_units = max(0, int(cap_raw))
                        except Exception:
                            reserve_cap_units = None
            except Exception:
                reserve_cap_units = None
            try:
                suggested = self.risk.size_with_risk(
                    want,
                    market_price,
                    current_inventory_usd=current_inventory_usd,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                )
            except Exception:
                suggested = self.risk.suggest_trade_units(
                    want, market_price, current_inventory_usd
                )
            base_suggested = max(0, int(suggested))
            final_suggested = base_suggested
            if reserve_cap_units is not None:
                final_suggested = min(base_suggested, reserve_cap_units)
            rationale.update(
                {
                    "desired_units": int(want),
                    "suggested_units": int(base_suggested),
                    "reserve_cap_units": (
                        int(reserve_cap_units)
                        if reserve_cap_units is not None
                        else None
                    ),
                    "pool_take_bps": (
                        int(pool_take_bps) if pool_take_bps is not None else None
                    ),
                    "utilization_ratio": (
                        float(utilization_ratio)
                        if utilization_ratio is not None
                        else None
                    ),
                    "vol_bps": (float(vol_bps) if vol_bps is not None else None),
                    "current_inventory_usd": (
                        float(current_inventory_usd)
                        if current_inventory_usd is not None
                        else None
                    ),
                }
            )
            if final_suggested != base_suggested:
                rationale.update({"reserve_capped_units": int(final_suggested)})
            suggested = int(final_suggested)
            rationale.update({"portfolioAdjustedUnits": int(suggested)})
            if suggested <= 0:
                logger.info("Risk rejected buy/burn (suggested=0)")
                rationale.update({"decision": "hold", "reason": "risk_rejected"})
                self._last_rationale = rationale
                return False
            wallet_diem_units = int((wallet_balances.get("DIEM") or {}).get("units", 0))
            # When burn is unavailable, only count actually burnable DIEM toward burn target
            # This allows spot-buying to continue accumulating DIEM at discount
            effective_burnable = (
                burnable_wallet_diem if burn_unavailable else wallet_diem_units
            )
            wallet_burn_units = min(effective_burnable, suggested)
            dex_target_units = max(0, suggested - wallet_burn_units)
            rationale.update(
                {
                    "wallet_diem_units": wallet_diem_units,
                    "wallet_burn_units": int(wallet_burn_units),
                    "dex_target_units": int(dex_target_units),
                    "effective_burnable": int(effective_burnable),
                }
            )

            # When burn is unavailable, use spot-buy path to accumulate DIEM at discount
            if mint_unavailable or burn_unavailable:
                if int(dex_target_units) <= 0:
                    # Only hold if wallet has enough burnable DIEM; otherwise keep buying
                    if (
                        burn_unavailable
                        and wallet_diem_units > 0
                        and burnable_wallet_diem == 0
                    ):
                        # Wallet has DIEM but none is burnable - strategy is to accumulate more
                        # Continue to buy more DIEM at discount (up to risk-adjusted suggested amount)
                        # Only hold if we've reached the max inventory cap
                        max_inventory_usd = float(
                            os.getenv("ARBI_DIEM_MAX_INVENTORY_USD", "10000")
                        )
                        current_inv_usd = float(current_inventory_usd or 0)
                        if current_inv_usd >= max_inventory_usd:
                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": "inventory_cap_reached",
                                    "current_inventory_usd": current_inv_usd,
                                    "max_inventory_usd": max_inventory_usd,
                                }
                            )
                            self._last_rationale = rationale
                            return False
                        # Otherwise, buy more to accumulate
                        dex_target_units = suggested  # Buy the full suggested amount
                        rationale["dex_target_units"] = int(dex_target_units)
                    else:
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "spot_inventory_sufficient",
                            }
                        )
                        self._last_rationale = rationale
                        return False
                unavailable_reason = (
                    "burn_unavailable" if burn_unavailable else "mint_unavailable"
                )
                logger.info(
                    f"Signal: Spot-buy DIEM inventory (units={int(dex_target_units)}) simulate={simulate} {unavailable_reason}=True"
                )
                rationale.update(
                    {
                        "decision": "spot_buy",
                        "units": int(dex_target_units),
                        "want_units": int(want),
                        "spot_buy_reason": unavailable_reason,
                    }
                )
                _metrics_inc(
                    "agent_decisions_total",
                    labels={"agent": "arbi_diem", "action": "spot_buy"},
                )
                if simulate:
                    rationale["execution"] = {
                        "status": ExecutionStatus.SIMULATED.value,
                        "buy": {
                            "status": ExecutionStatus.SIMULATED.value,
                            "side": "buy",
                            "token_out": "DIEM",
                            "amount_base_units": int(dex_target_units),
                            "burn_unavailable": burn_unavailable,
                            "mint_unavailable": mint_unavailable,
                        },
                    }
                    self._last_rationale = rationale
                    return True
                try:
                    buy_res = self.diem.trade(
                        "buy",
                        int(dex_target_units),
                        slippage_bps=int(slippage_cap_bps),
                        corr_id=corr_id,
                    )
                    rationale["execution"] = {
                        "status": buy_res.get("status", "submitted"),
                        "buy": buy_res,
                    }
                    buy_status = str(buy_res.get("status", "")).lower()
                    if buy_status in {"error", "failed", "rejected"}:
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "execution_failed",
                                "execution_error": buy_res.get("error")
                                or buy_res.get("reason"),
                            }
                        )
                        self._last_rationale = rationale
                        return False
                except Exception as exc:
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "execution_exception",
                            "execution_error": str(exc),
                        }
                    )
                    self._last_rationale = rationale
                    return False
                self._last_rationale = rationale
                return True

            # If wallet covers the full target, skip DEX path entirely
            if dex_target_units <= 0:
                logger.info(
                    "Buy/burn satisfied from wallet inventory; skipping DEX acquisition"
                )
                rationale.update(
                    {
                        "tradeRoute": "wallet_inventory",
                        "execution_preview": {
                            "status": "wallet_only",
                            "wallet_burn_units": int(wallet_burn_units),
                        },
                    }
                )
                if not simulate:
                    try:
                        gas_budget = self.diem.estimate_gas_budget_wei(
                            include_swap=False
                        )
                        eth_balance = self.diem._eth_balance_wei()
                        if (
                            gas_budget
                            and eth_balance is not None
                            and eth_balance < int(gas_budget.get("required_wei", 0))
                        ):
                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": "insufficient_gas_funds",
                                    "execution_error": "insufficient_gas_funds",
                                    "eth_balance_wei": int(eth_balance),
                                    "gas_required_wei": int(
                                        gas_budget.get("required_wei", 0)
                                    ),
                                    "gas_price_wei": gas_budget.get(
                                        "effective_gas_price"
                                    ),
                                }
                            )
                            self._last_rationale = rationale
                            return False
                    except Exception:
                        pass
                    try:
                        execution_result = self.diem.wallet_first_buy_and_burn(
                            diem_amount=int(wallet_burn_units),
                            slippage_bps=int(slippage_cap_bps),
                            pool_take_bps=pool_take_bps,
                            simulate=False,
                            portfolio_snapshot=exec_ctx.snapshot,
                        )
                        rationale["execution"] = execution_result
                        burn_status = execution_result.get("burn", {}).get(
                            "status", execution_result.get("status")
                        )
                        if burn_status in (
                            ExecutionStatus.REJECTED.value,
                            ExecutionStatus.FAILED.value,
                            "error",
                        ):
                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": "burn_failed",
                                    "execution_error": execution_result.get("burn", {}),
                                }
                            )
                            self._last_rationale = rationale
                            return False
                    except Exception as exc:
                        logger.error(
                            f"wallet-only buy/burn execution failed: {exc}",
                            exc_info=True,
                        )
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "execution_exception",
                                "execution_error": str(exc),
                            }
                        )
                        self._last_rationale = rationale
                        return False
                self._last_rationale = rationale
                return True

            # Apply route-specific adaptive slippage to slippage cap
            adaptive_multiplier = 1.0
            try:
                if routes and len(routes) > 0:
                    # Use the first route for adaptive slippage calculation
                    route_for_slippage = routes[0]
                    adaptive_multiplier = self._calculate_adaptive_slippage_multiplier(
                        route_for_slippage
                    )
                    if adaptive_multiplier != 1.0:
                        # Apply multiplier to slippage cap
                        original_cap = slippage_cap_bps
                        slippage_cap_bps = slippage_cap_bps * adaptive_multiplier
                        # Still respect the risk max cap
                        risk_max_bps = float(self.risk.slippage_bps_cap)
                        slippage_cap_bps = min(slippage_cap_bps, risk_max_bps)
                        logger.info(
                            f"ArbiDiem adaptive slippage: route multiplier={adaptive_multiplier:.3f}, "
                            f"cap={original_cap:.1f}bps -> {slippage_cap_bps:.1f}bps",
                            extra={
                                "agent": "arbi_diem",
                                "action": "adaptive_slippage",
                                "original_cap_bps": original_cap,
                                "adaptive_multiplier": adaptive_multiplier,
                                "adjusted_cap_bps": slippage_cap_bps,
                                "route_tokens": (
                                    list(routes[0].tokens)
                                    if hasattr(routes[0], "tokens")
                                    else []
                                ),
                                "route_is_v3": (
                                    routes[0].is_uniswap_v3()
                                    if hasattr(routes[0], "is_uniswap_v3")
                                    else False
                                ),
                            },
                        )
                        rationale.update(
                            {
                                "adaptive_slippage_multiplier": float(
                                    adaptive_multiplier
                                ),
                                "slippage_cap_bps": float(slippage_cap_bps),
                            }
                        )
            except Exception as exc:
                logger.debug(
                    f"Adaptive slippage calculation failed: {exc}, using base cap"
                )

            # Liquidity-aware adaptive slippage cap (opt-in via DIEM_SLIPPAGE_OVERRIDE_ENABLE).
            try:
                base_cap = float(slippage_cap_bps)
            except Exception:
                base_cap = float(rationale.get("slippage_cap_bps") or 0.0)
            trade_usd = None
            try:
                if int(dex_target_units) > 0:
                    trade_usd = float(
                        self.risk.usd_from_units(
                            int(dex_target_units), float(market_price)
                        )
                    )
            except Exception:
                trade_usd = None
            route_health = None
            try:
                route_health = self._route_health(routes[0] if routes else None)
            except Exception:
                route_health = None
            adaptive_cap, adaptive_meta = self._adaptive_slippage_cap_bps(
                float(base_cap),
                trade_usd=trade_usd,
                liquidity_depth_usd=None,
                route_health=route_health,
            )
            if adaptive_meta.get("applied"):
                slippage_cap_bps = float(adaptive_cap)
                rationale.update(
                    {
                        "slippage_cap_bps": float(slippage_cap_bps),
                        "slippage_cap_adaptive": adaptive_meta,
                    }
                )

            dex_suggested = int(dex_target_units)
            adjusted, last_bps, initial_bps = self._adjust_for_liquidity_buy(
                dex_suggested, market_price, slippage_cap_bps=slippage_cap_bps
            )
            rationale.update(
                {
                    "liquidity_adjusted_units": int(adjusted),
                    "price_deviation_bps_initial": (
                        float(initial_bps) if initial_bps is not None else None
                    ),
                    "liquidity_downsized": bool(int(adjusted) != int(dex_suggested)),
                }
            )
            execution_target_units = int(wallet_burn_units + max(0, adjusted))
            # Initialize price_health_info early so it's available for bridge fallback checks
            price_health_info = {}
            if last_bps is None:
                # Cannot preview exact-out; gather context for rationale
                trade_route_meta = None
                is_composite_active = False
                try:
                    md = self._market_provider()
                    health = md.price_health("DIEM")
                    price_health_info = {
                        "source": health.get("source", "unknown"),
                        "valid": health.get("valid", False),
                        "price": health.get("price"),
                    }
                except Exception:
                    pass

                # Check if composite routing was active (use already-filtered routes)
                try:
                    if routes and routes[0]:
                        from libs.dex.composite import is_composite_route

                        is_composite_active = is_composite_route(routes[0])
                        route_meta = getattr(routes[0], "_metadata", None)
                        if isinstance(route_meta, dict):
                            trade_route_meta = route_meta
                except Exception:
                    pass

                # Determine whether exact-in fallback is enabled (including legacy and auto modes)
                exact_in_fallback_enabled = os.getenv(
                    "DIEM_EXACT_IN_FALLBACK_ENABLE", "1"
                ).strip().lower() in {"1", "true", "yes", "on"}
                if not exact_in_fallback_enabled:
                    # Also check legacy env var for backward compatibility
                    exact_in_fallback_enabled = os.getenv(
                        "ARBI_DIEM_EXACT_IN_FALLBACK", "1"
                    ).strip().lower() in {"1", "true", "yes", "on"}

                auto_fallback_enabled = os.getenv(
                    "DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY", "0"
                ).strip().lower() in {"1", "true", "yes", "on"}
                if auto_fallback_enabled:
                    # Auto-enable fallback if price source is healthy and DIEM/VVV reserves are sane
                    source = price_health_info.get("source", "")
                    if source in {"bridge_vvv", "path_engine"}:
                        reserves_sane = False
                        try:
                            md = self._market_provider()
                            # Use already-filtered routes
                            if routes:
                                diem_addr = (
                                    (os.getenv("DIEM_TOKEN_ADDRESS") or "")
                                    .strip()
                                    .lower()
                                )
                                vvv_addr = (
                                    (os.getenv("VVV_TOKEN_ADDRESS") or "")
                                    .strip()
                                    .lower()
                                )
                                if diem_addr and vvv_addr:
                                    from services.marketdata import (
                                        etherscan_verify as es,
                                    )

                                    discovery = es.verify_trade_path(
                                        [vvv_addr, diem_addr]
                                    )
                                    hops = discovery.get("hops") or []
                                    if hops:
                                        uni = (hops[0] or {}).get("uniswap_v2") or {}
                                        reserves = uni.get("reserves")
                                        if (
                                            isinstance(reserves, (tuple, list))
                                            and len(reserves) >= 2
                                        ):
                                            if (
                                                int(reserves[0] or 0) > 0
                                                and int(reserves[1] or 0) > 0
                                            ):
                                                reserves_sane = True
                        except Exception:
                            pass

                        if reserves_sane:
                            exact_in_fallback_enabled = True
                            logger.debug(
                                "Auto-enabled exact-in fallback: bridge healthy and reserves sane"
                            )

                # For composite routes with no exact-out preview and no fallback,
                # skip buy/burn and record detailed rationale instead of relying on bridge price.
                if is_composite_active and not exact_in_fallback_enabled:
                    logger.info(
                        "Buy/burn skipped: no exact-out preview available for composite route"
                    )
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "no_exact_out_preview",
                            "exact_in_fallback": False,
                            "price_health": price_health_info,
                            "is_composite": is_composite_active,
                            "trade_route_meta": trade_route_meta,
                        }
                    )
                    self._last_rationale = rationale
                    return False

                # In dry-run mode, use bridge_vvv price as simulation proxy when available,
                # but only when exact-in fallback is not enabled for composite routes.
                bridge_fallback_used = False
                # Allow bridge fallback if source is bridge_vvv OR if composite uses bridge path
                sim_price_source = price_health_info.get("source", "")
                sim_price_provider = price_health_info.get("provider", "")
                sim_price_path = price_health_info.get("path", [])
                sim_vvv_in_path = any(
                    "acfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf" in str(p).lower()
                    for p in sim_price_path
                )
                sim_is_bridge_compatible = (
                    sim_price_source == "bridge_vvv"
                    or (sim_price_provider == "composite" and sim_vvv_in_path)
                    or (sim_price_source == "diem_canonical" and sim_vvv_in_path)
                )
                if simulate and sim_is_bridge_compatible:
                    if not (is_composite_active and exact_in_fallback_enabled):
                        bridge_price = price_health_info.get("price") or market_price
                        if bridge_price is not None:
                            try:
                                bridge_price_float = float(bridge_price)
                                if bridge_price_float > 0:
                                    # Use bridge price as execution preview (assume 0% slippage for simulation)
                                    exec_price_preview = bridge_price_float
                                    slip_check = self._check_slippage_buy(
                                        exec_price_preview,
                                        market_price,
                                        slippage_cap_bps=slippage_cap_bps,
                                    )

                                    # For bridge fallback in simulate mode, mirror the live fallback
                                    # slippage assumption so dry-run matches execution intent.
                                    if slip_check.get("quote_failure", False):
                                        synthetic_bps = float(
                                            os.getenv(
                                                "DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS",
                                                "50.0",
                                            )
                                            or 50.0
                                        )
                                        logger.debug(
                                            "Buy/burn dry-run: quote_failure in slippage check, using bridge fallback slippage assumption"
                                        )
                                    else:
                                        synthetic_bps = float(
                                            slip_check.get("slippage_bps", 0.0)
                                            if isinstance(slip_check, dict)
                                            else 0.0
                                        )
                                    logger.info(
                                        f"Buy/burn dry-run: using bridge_vvv price as execution preview "
                                        f"(price=${bridge_price_float:.2f}, slippage_bps={synthetic_bps:.2f})"
                                    )
                                    rationale.update(
                                        {
                                            "bridge_price_fallback": True,
                                            "exec_price_preview": exec_price_preview,
                                            "slippage_bps": synthetic_bps,
                                            "slippage_ok": synthetic_bps
                                            <= float(slippage_cap_bps),
                                            "price_health": price_health_info,
                                            "is_composite": is_composite_active,
                                            "trade_route_meta": trade_route_meta,
                                        }
                                    )
                                    # Use synthetic slippage and continue with execution logic
                                    last_bps = synthetic_bps
                                    bridge_fallback_used = True
                            except Exception:
                                pass

                # If bridge fallback wasn't used, check exact-in fallback
                if not bridge_fallback_used:
                    if not exact_in_fallback_enabled:
                        # Default: skip if no exact-out preview (safe default)
                        logger.info("Buy/burn skipped: no exact-out preview available")
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": "no_exact_out_preview",
                                "exact_in_fallback": False,
                                "price_health": price_health_info,
                                "is_composite": is_composite_active,
                                "trade_route_meta": trade_route_meta,
                            }
                        )
                        self._last_rationale = rationale
                        return False
                # Try exact-in fallback for small trades
                fallback_result = self._try_exact_in_fallback_buy(
                    suggested, market_price, current_inventory_usd, simulate=simulate
                )
                fallback_reason = getattr(self, "_last_exact_in_fallback_reason", None)
                if fallback_result is None:
                    # Exact-in fallback failed - try bridge live fallback before giving up
                    bridge_fallback_success = False
                    if not simulate:
                        bridge_live_enabled = os.getenv(
                            "DIEM_BRIDGE_LIVE_FALLBACK_ENABLE", "0"
                        ).strip().lower() in {"1", "true", "yes", "on"}

                        # Allow bridge fallback if source is bridge_vvv OR if composite
                        # provider is using bridge-based pricing (path includes VVV)
                        price_source = price_health_info.get("source", "")
                        price_provider = price_health_info.get("provider", "")
                        price_path = price_health_info.get("path", [])
                        vvv_in_path = any(
                            "acfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf" in str(p).lower()
                            for p in price_path
                        )
                        is_bridge_compatible = (
                            price_source == "bridge_vvv"
                            or (price_provider == "composite" and vvv_in_path)
                            or (price_source == "diem_canonical" and vvv_in_path)
                        )
                        if bridge_live_enabled and is_bridge_compatible:
                            # Use market_price as bridge price when source is composite
                            bridge_price = (
                                price_health_info.get("price") or market_price
                            )
                            if bridge_price is not None:
                                try:
                                    bridge_price_float = float(bridge_price)
                                    if bridge_price_float > 0:
                                        max_usd = float(
                                            os.getenv(
                                                "DIEM_BRIDGE_LIVE_FALLBACK_MAX_USD",
                                                "5.0",
                                            )
                                            or 5.0
                                        )
                                        diem_decimals = self.risk._diem_decimals()
                                        trade_value_usd = (
                                            float(suggested) / 10**diem_decimals
                                        ) * market_price

                                        if trade_value_usd <= max_usd:
                                            BRIDGE_FALLBACK_SLIPPAGE_BPS = float(
                                                os.getenv(
                                                    "DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS",
                                                    "50.0",
                                                )
                                                or 50.0
                                            )

                                            logger.info(
                                                f"Buy/burn live mode: using bridge_vvv fallback after exact-in failed "
                                                f"(price=${bridge_price_float:.4f}, trade_usd=${trade_value_usd:.2f}, "
                                                f"assumed_slippage_bps={BRIDGE_FALLBACK_SLIPPAGE_BPS})"
                                            )

                                            last_bps = BRIDGE_FALLBACK_SLIPPAGE_BPS
                                            adjusted = suggested
                                            bridge_fallback_success = True

                                            rationale.update(
                                                {
                                                    "bridge_live_fallback": True,
                                                    "bridge_price": bridge_price_float,
                                                    "trade_value_usd": trade_value_usd,
                                                    "assumed_slippage_bps": BRIDGE_FALLBACK_SLIPPAGE_BPS,
                                                    "exact_in_fallback": True,
                                                    "fallback_failure_reason": fallback_reason,
                                                }
                                            )
                                        else:
                                            logger.debug(
                                                f"Bridge fallback skipped: trade_usd={trade_value_usd:.2f} > max={max_usd}"
                                            )
                                except Exception as e:
                                    logger.debug(f"Bridge live fallback error: {e}")

                    if not bridge_fallback_success:
                        logger.info(
                            f"Buy/burn skipped: exact-out unavailable, exact-in fallback also failed "
                            f"(reason={fallback_reason or 'exact_in_fallback_failed'})"
                        )
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": fallback_reason or "exact_in_fallback_failed",
                                "exact_in_fallback": True,
                                "fallback_failure_reason": fallback_reason,
                                "price_health": price_health_info,
                                "is_composite": is_composite_active,
                                "trade_route_meta": trade_route_meta,
                            }
                        )
                        self._last_rationale = rationale
                        return False
                # Fallback succeeded - use its results
                adjusted, last_bps, fallback_quote = fallback_result
                rationale.update(
                    {
                        "exact_in_fallback": True,
                        "price_health": price_health_info,
                        "is_composite": is_composite_active,
                        "trade_route_meta": trade_route_meta,
                        "fallback_quote_amount_in": (
                            int(fallback_quote.amount_in) if fallback_quote else None
                        ),
                        "fallback_quote_amount_out": (
                            int(fallback_quote.amount_out) if fallback_quote else None
                        ),
                        "fallback_provider": (
                            fallback_quote.provider if fallback_quote else None
                        ),
                    }
                )
                logger.info(
                    f"Buy/burn: using exact-in fallback (units={adjusted}, slippage_bps={last_bps}, provider={fallback_quote.provider if fallback_quote else None})"
                )
            if last_bps is not None:
                soft_cap = self._slippage_soft_cap_bps()
                rationale.update(
                    {
                        "slippage_bps": float(last_bps),
                        "slippage_ok": bool(last_bps <= float(slippage_cap_bps)),
                        "slippage_soft_cap_bps": (
                            float(soft_cap) if soft_cap is not None else None
                        ),
                        "liquidity_adjustment_config": {
                            "max_steps": self._liquidity_max_adjust_steps(),
                            "min_trade_usd": self._liquidity_min_trade_usd(),
                        },
                    }
                )
            if adjusted <= 0:
                if wallet_burn_units > 0:
                    logger.info(
                        "DEX leg rejected by liquidity, proceeding with wallet-only burn"
                    )
                    rationale.update(
                        {
                            "decision": "buy_burn",
                            "reason": "dex_leg_blocked_wallet_only",
                            "execution_preview": {
                                "status": "wallet_only",
                                "wallet_burn_units": int(wallet_burn_units),
                            },
                        }
                    )
                    execution_target_units = int(wallet_burn_units)
                    if not simulate:
                        try:
                            execution_result = self.diem.wallet_first_buy_and_burn(
                                diem_amount=execution_target_units,
                                slippage_bps=int(slippage_cap_bps),
                                pool_take_bps=pool_take_bps,
                                simulate=False,
                                portfolio_snapshot=exec_ctx.snapshot,
                            )
                            rationale["execution"] = execution_result
                        except Exception as exc:
                            logger.error(
                                f"wallet-only burn after dex rejection failed: {exc}",
                                exc_info=True,
                            )
                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": "execution_exception",
                                    "execution_error": str(exc),
                                }
                            )
                            self._last_rationale = rationale
                            return False
                    self._last_rationale = rationale
                    return True
                logger.info("Rejected buy due to liquidity/slippage after adjustment")
                rationale.update({"decision": "hold", "reason": "slippage_exceeded"})
                self._last_rationale = rationale
                return False
            suggested = adjusted
            # Call preview_trade for execution preview (both simulate and live modes)
            execution_preview = None
            preview_status_rejected = False
            if adjusted > 0:
                try:
                    # For buy, we need the reversed route (USDC->...->DIEM)
                    # When DIEM_BUY_DIRECT_ONLY=1, prefer direct USDC→DIEM route
                    direct_buy = self._get_direct_buy_route_if_enabled()
                    if direct_buy is not None:
                        buy_route = direct_buy
                    else:
                        buy_route = (
                            routes[0].reversed() if routes and routes[0] else None
                        )
                    buy_intent = ExecutionIntent(
                        side=TradeSide.BUY,
                        token_in="USDC",
                        token_out="DIEM",
                        amount_base_units=adjusted,
                        slippage_bps=int(slippage_cap_bps),
                        pool_take_bps=pool_take_bps,
                        preferred_route=buy_route,
                        metadata={
                            "correlation_id": corr_id,
                            "decision": "buy_burn",
                            "diem_market_price_usd": float(market_price),
                        },
                    )
                    preview_result = self.diem.preview_trade(buy_intent)
                    execution_preview = preview_result.as_dict()
                    preview_status_rejected = (
                        preview_result.status == ExecutionStatus.REJECTED
                    )
                    # Route health dashboard snapshot
                    try:
                        diagnostics_latest = getattr(
                            self.diem.aggregator, "_last_quote_diagnostics", []
                        )
                        route_health = _route_health_summary(diagnostics_latest)
                        if route_health:
                            rationale["route_health"] = route_health
                            logger.info(
                                "route_health_snapshot",
                                extra={
                                    "route_health": route_health,
                                    "correlation_id": corr_id,
                                },
                            )
                    except Exception:
                        pass
                    # Update exec_price_preview from preview result if available
                    if preview_result.effective_price is not None:
                        rationale["exec_price_preview"] = float(
                            preview_result.effective_price
                        )
                    if preview_result.slippage_bps is not None:
                        rationale["slippage_bps"] = float(preview_result.slippage_bps)
                except Exception as exc:
                    logger.debug(f"preview_trade failed: {exc}")
            rationale["execution_preview"] = execution_preview

            # Track trade route for telemetry - use route from execution preview if available
            trade_route_str = None
            if execution_preview and execution_preview.get("route_tokens"):
                # Prefer route from execution preview (actual route used)
                try:
                    route_tokens = execution_preview.get("route_tokens", [])
                    trade_route_str = (
                        "->".join(str(t) for t in route_tokens)
                        if route_tokens
                        else None
                    )
                except Exception:
                    pass
            elif routes and routes[0]:
                # Fallback to filtered route if preview doesn't have route info
                try:
                    route_tokens = (
                        routes[0].tokens if hasattr(routes[0], "tokens") else []
                    )
                    trade_route_str = (
                        "->".join(str(t) for t in route_tokens)
                        if route_tokens
                        else None
                    )
                except Exception:
                    pass

            if adjusted != suggested:
                rationale.update(
                    {
                        "liquidity_adjusted_units": int(adjusted),
                        "portfolioAdjustedUnits": int(adjusted),
                    }
                )
            else:
                rationale.update({"portfolioAdjustedUnits": int(adjusted)})

            if trade_route_str:
                rationale.update({"tradeRoute": trade_route_str})

            # Adaptive slippage fallback: retry with smaller size when slippage is extreme.
            if (
                not simulate
                and adjusted > 1
                and execution_preview
                and execution_preview.get("slippage_bps") is not None
            ):
                try:
                    preview_slip = float(execution_preview.get("slippage_bps"))
                except Exception:
                    preview_slip = float(last_bps) if last_bps is not None else None
                EXTREME_SLIPPAGE_THRESHOLD_BPS = 1000.0
                if (
                    preview_slip is not None
                    and preview_slip >= EXTREME_SLIPPAGE_THRESHOLD_BPS
                ):
                    retry_units = max(1, int(adjusted) // 2)
                    if retry_units < int(adjusted):
                        try:
                            retry_intent = ExecutionIntent(
                                side=TradeSide.BUY,
                                token_in="USDC",
                                token_out="DIEM",
                                amount_base_units=int(retry_units),
                                slippage_bps=int(slippage_cap_bps),
                                pool_take_bps=pool_take_bps,
                                preferred_route=buy_route,
                                metadata={
                                    "correlation_id": corr_id,
                                    "decision": "buy_burn_retry",
                                    "diem_market_price_usd": float(market_price),
                                },
                            )
                            retry_preview = self.diem.preview_trade(retry_intent)
                            if (
                                retry_preview
                                and retry_preview.slippage_bps is not None
                                and float(retry_preview.slippage_bps)
                                < EXTREME_SLIPPAGE_THRESHOLD_BPS
                            ):
                                adjusted = int(retry_units)
                                last_bps = retry_preview.slippage_bps
                                execution_preview = retry_preview.as_dict()
                                rationale["execution_preview"] = execution_preview
                                rationale["adaptive_slippage_retry"] = {
                                    "retry_units": int(retry_units),
                                    "slippage_bps": float(last_bps),
                                }
                        except Exception:
                            pass

            # Check slippage policy before execution
            slippage_ok = last_bps is None or last_bps <= float(slippage_cap_bps)
            # Block execution if slippage exceeds cap or is extremely high (near 10,000 bps = effectively no depth)
            EXTREME_SLIPPAGE_THRESHOLD_BPS = 1000.0
            extreme_slippage = (
                last_bps is not None and last_bps >= EXTREME_SLIPPAGE_THRESHOLD_BPS
            )
            # Check if we have valid execution price preview (quotes available)
            # For buy path, we use exact-out preview which is checked in _adjust_for_liquidity_buy
            # If exact-out fails, we may use exact-in fallback (handled above)
            # If last_bps is None, it means no preview was available (already handled above)
            # Also check execution_preview status - if preview_trade returned REJECTED, block execution
            has_valid_preview = last_bps is not None and not preview_status_rejected

            def _diagnostics_all_failed() -> bool:
                try:
                    if self.diem.aggregator is None:
                        return False
                    diagnostics = getattr(
                        self.diem.aggregator, "_last_quote_diagnostics", []
                    )
                    if not isinstance(diagnostics, list) or len(diagnostics) == 0:
                        return False
                    has_executable_success = False
                    for diag in diagnostics:
                        status = str(diag.get("status", "")).lower()
                        executable = diag.get("executable", True)
                        if status == "ok" and executable:
                            has_executable_success = True
                        # Treat analytic/non-executable quotes as failures for liquidity gating
                        if status == "ok" and not executable:
                            continue
                        if status not in {
                            "empty",
                            "error",
                            "timeout_pending",
                            "no_pool",
                            "zero_liquidity",
                        }:
                            return False
                    return not has_executable_success
                except Exception:
                    return False

            # Hard guard: if every provider failed, block execution regardless of preview/slippage
            if not simulate and _diagnostics_all_failed():
                logger.info(
                    "Buy and burn blocked: diagnostics show zero quotes from all providers"
                )
                rationale.update(
                    {
                        "decision": "hold",
                        "reason": "no_quotes_from_providers",
                        "slippage_bps": (
                            float(last_bps) if last_bps is not None else None
                        ),
                        "policy_checks": {"slippage_ok": False},
                    }
                )
                self._last_rationale = rationale
                return False

            # Bridge_vvv fallback for live mode: when DEX quotes fail but bridge price is trusted
            # This is a controlled fallback for small trade sizes only
            # Only allow if diagnostics don't show all providers failed
            if not simulate and not has_valid_preview:
                diagnostics_show_zero_quotes = _diagnostics_all_failed()

                # Populate price_health_info if not already populated (e.g., when last_bps was not None)
                if not price_health_info:
                    try:
                        md = self._market_provider()
                        health = md.price_health("DIEM")
                        price_health_info = {
                            "source": health.get("source", "unknown"),
                            "valid": health.get("valid", False),
                            "price": health.get("price"),
                        }
                    except Exception:
                        pass

                # Check if bridge_vvv fallback is enabled for live mode
                bridge_live_enabled = os.getenv(
                    "DIEM_BRIDGE_LIVE_FALLBACK_ENABLE", "0"
                ).strip().lower() in {"1", "true", "yes", "on"}

                # Allow bridge fallback if source is bridge_vvv OR composite with bridge path
                fb_price_source = price_health_info.get("source", "")
                fb_price_provider = price_health_info.get("provider", "")
                fb_price_path = price_health_info.get("path", [])
                fb_vvv_in_path = any(
                    "acfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf" in str(p).lower()
                    for p in fb_price_path
                )
                fb_is_bridge_compatible = (
                    fb_price_source == "bridge_vvv"
                    or (fb_price_provider == "composite" and fb_vvv_in_path)
                    or (fb_price_source == "diem_canonical" and fb_vvv_in_path)
                )

                if (
                    bridge_live_enabled
                    and fb_is_bridge_compatible
                    and not diagnostics_show_zero_quotes
                ):
                    bridge_price = price_health_info.get("price") or market_price
                    if bridge_price is not None:
                        try:
                            bridge_price_float = float(bridge_price)
                            if bridge_price_float > 0:
                                # Check trade size limits for fallback
                                max_usd = float(
                                    os.getenv(
                                        "DIEM_BRIDGE_LIVE_FALLBACK_MAX_USD", "5.0"
                                    )
                                    or 5.0
                                )
                                diem_decimals = self.risk._diem_decimals()
                                trade_value_usd = (
                                    float(adjusted) / 10**diem_decimals
                                ) * market_price

                                if trade_value_usd <= max_usd:
                                    # Use bridge price with conservative slippage assumption
                                    # Assume ~50 bps slippage for bridge-anchored execution
                                    BRIDGE_FALLBACK_SLIPPAGE_BPS = float(
                                        os.getenv(
                                            "DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS",
                                            "50.0",
                                        )
                                        or 50.0
                                    )

                                    logger.info(
                                        f"Buy/burn live mode: using bridge_vvv price fallback "
                                        f"(price=${bridge_price_float:.4f}, trade_usd=${trade_value_usd:.2f}, "
                                        f"assumed_slippage_bps={BRIDGE_FALLBACK_SLIPPAGE_BPS})"
                                    )

                                    last_bps = BRIDGE_FALLBACK_SLIPPAGE_BPS
                                    has_valid_preview = True
                                    preview_status_rejected = False  # Reset since bridge fallback provides valid preview

                                    rationale.update(
                                        {
                                            "bridge_live_fallback": True,
                                            "bridge_price": bridge_price_float,
                                            "trade_value_usd": trade_value_usd,
                                            "assumed_slippage_bps": BRIDGE_FALLBACK_SLIPPAGE_BPS,
                                        }
                                    )
                                else:
                                    logger.debug(
                                        f"Bridge fallback skipped: trade_usd={trade_value_usd:.2f} > max={max_usd}"
                                    )
                        except Exception as e:
                            logger.debug(f"Bridge live fallback error: {e}")

            # Block execution if no valid quotes/preview available (unless in simulate mode)
            if not simulate and not has_valid_preview:
                logger.info(
                    "Buy and burn blocked: no valid execution price preview available"
                )
                # Check diagnostics for specific failure reasons
                reason = "no_execution_preview"
                if execution_preview:
                    diag = execution_preview.get("diagnostics", {})
                    failure_class = diag.get("failure_classification")
                    if failure_class == "diem_bridge_fallback_disabled":
                        reason = "no_executable_bridge_route_fallback_disabled"
                    elif failure_class == "diem_bridge_leg_failure":
                        reason = "no_executable_bridge_route_leg_failure"
                    elif failure_class == "diem_bridge_no_executable_route" or (
                        diag.get("bridge_route_available")
                        and not diag.get("bridge_quotes_found")
                    ):
                        reason = "no_executable_bridge_route"

                rationale.update(
                    {
                        "decision": "hold",
                        "reason": reason,
                        "slippage_bps": (
                            float(last_bps) if last_bps is not None else None
                        ),
                        "policy_checks": {"slippage_ok": slippage_ok},
                    }
                )
                self._last_rationale = rationale
                return False
            # Block execution if slippage exceeds policy cap or is extreme
            if (not slippage_ok or extreme_slippage) and not rationale.get(
                "exact_in_fallback"
            ):
                reason = (
                    "extreme_slippage"
                    if extreme_slippage
                    else "slippage_exceeded_policy"
                )
                # Track route failures for fallback logic
                if last_bps is not None and last_bps >= 9000.0:
                    self._diem_buy_route_failures += 1
                else:
                    self._diem_buy_route_failures = 0

                logger.info(
                    f"Buy and burn blocked: {reason} (slippage_bps={last_bps}, cap={slippage_cap_bps})"
                )

                # Fallback to stake-only recovery if locked ratio exceeds cap
                cap = float(self._locked_svvv_ratio_cap())
                if cap < 1.0:
                    try:
                        wallet_balances = exec_ctx.balances
                        if wallet_balances.get("SVVV"):
                            lock_state = self._svvv_lock_state(wallet_balances)
                            locked_ratio = lock_state.get("locked_ratio")
                            min_units = int(self._locked_svvv_ratio_min_total_units())
                            total_units = int(lock_state.get("total_units") or 0)
                            if (
                                locked_ratio is not None
                                and float(locked_ratio) > cap
                                and total_units >= min_units
                            ):
                                logger.info(
                                    f"Buy/burn failed ({reason}), attempting stake-only recovery "
                                    f"(locked_ratio={float(locked_ratio) * 100:.1f}% > cap={cap * 100:.1f}%)"
                                )
                                # Get variables from rationale or function scope
                                threshold_mult_fallback = float(
                                    rationale.get("threshold_mult", threshold_mult)
                                )
                                mint_rate_fallback = float(
                                    rationale.get("mint_rate", mint_rate_svvv_per_diem)
                                )
                                vvv_price_fallback = float(
                                    rationale.get("vvv_price_usd", vvv_price)
                                )
                                recovered = self._maybe_capacity_recovery(
                                    market_price=float(market_price),
                                    fair_value=float(fair_value),
                                    threshold_mult=threshold_mult_fallback,
                                    mint_rate=mint_rate_fallback,
                                    slippage_cap_bps=float(slippage_cap_bps),
                                    pool_take_bps=pool_take_bps,
                                    corr_id=corr_id,
                                    simulate=bool(simulate),
                                    exec_ctx=exec_ctx,
                                    svvv_lock_state=lock_state,
                                    rationale=rationale,
                                    utilization_ratio=utilization_ratio,
                                    vol_bps=vol_bps,
                                    current_inventory_usd=current_inventory_usd,
                                    vvv_price_usd=vvv_price_fallback,
                                    force_stake_only=True,
                                )
                                if recovered:
                                    return True
                    except Exception as exc:
                        logger.debug(f"Stake-only recovery fallback failed: {exc}")

                rationale.update(
                    {
                        "decision": "hold",
                        "reason": reason,
                        "slippage_bps": (
                            float(last_bps) if last_bps is not None else None
                        ),
                        "policy_checks": {"slippage_ok": slippage_ok},
                    }
                )
                self._last_rationale = rationale
                return False
            # Structured decision log with route, venue, slippage, pool-take, policy checks
            # Determine venue and slippage source based on whether fallback was used
            venue = "exact_out"  # Default to exact-out
            slippage_source = "market_depth"
            if rationale.get("bridge_live_fallback"):
                venue = "bridge_vvv_fallback"
                slippage_source = "bridge_vvv"
            elif rationale.get("exact_in_fallback"):
                venue = "exact_in_fallback"
                slippage_source = "exact_in_fallback"
            elif last_bps is not None:
                if last_bps >= 9000.0:
                    slippage_source = "quote_failure"
                    # Track route failures for fallback logic
                    self._diem_buy_route_failures += 1
                elif last_bps >= 1000.0:
                    slippage_source = "extreme_market_depth"
                    self._diem_buy_route_failures = 0
                else:
                    slippage_source = "market_depth"
                    self._diem_buy_route_failures = 0
            execution_slippage_tolerance_bps = None
            try:
                execution_slippage_tolerance_bps = int(slippage_cap_bps)
                if self._slippage_override_enabled() and last_bps is not None:
                    execution_slippage_tolerance_bps = int(last_bps)
            except Exception:
                execution_slippage_tolerance_bps = None
            decision_log = {
                "decision": "mint_sell" if use_exact_in_only else "buy_burn",
                "units": int(execution_target_units),
                "dex_units": int(adjusted),
                "wallet_burn_units": int(wallet_burn_units),
                "want_units": int(want),
                "price_deviation_bps_initial": rationale.get(
                    "price_deviation_bps_initial"
                ),
                "price_deviation_bps_final": float(last_bps)
                if last_bps is not None
                else None,
                "slippage_bps": float(last_bps) if last_bps is not None else None,
                "slippage_source": slippage_source,
                "pool_take_bps": (
                    int(pool_take_bps) if pool_take_bps is not None else None
                ),
                "reserve_cap_units": (
                    int(reserve_cap_units) if reserve_cap_units is not None else None
                ),
                "trade_route": trade_route_str,
                "venue": venue,
                "slippage_cap_bps": float(slippage_cap_bps),
                "execution_slippage_tolerance_bps": execution_slippage_tolerance_bps,
                "liquidity_downsized": rationale.get("liquidity_downsized"),
                "policy_checks": {
                    "slippage_ok": slippage_ok,
                    "pool_take_ok": pool_take_bps is None
                    or pool_take_bps <= 100,  # Default cap
                    "reserve_cap_ok": reserve_cap_units is None
                    or adjusted <= reserve_cap_units,
                },
            }
            logger.info(
                f"Signal: Buy and burn DIEM (units={adjusted}, want={want}) simulate={simulate} "
                f"price_deviation_bps_initial={rationale.get('price_deviation_bps_initial')} "
                f"price_deviation_bps_final={last_bps} "
                f"execution_slippage_tolerance_bps={execution_slippage_tolerance_bps} "
                f"downsized={rationale.get('liquidity_downsized')} "
                f"venue={venue} pool_take_bps={pool_take_bps} route={trade_route_str}"
            )
            rationale.update(decision_log)
            _metrics_inc(
                "agent_decisions_total",
                labels={"agent": "arbi_diem", "action": "buy_burn"},
            )
            if not simulate:
                # Pre-flight gas budget: ensure we can afford burn (+ swap if needed)
                try:
                    portfolio = exec_ctx.balances
                    wallet_diem_units = int(
                        (portfolio.get("DIEM") or {}).get("units", 0)
                    )
                    needs_buy = adjusted > 0
                    gas_budget = self.diem.estimate_gas_budget_wei(
                        include_swap=needs_buy
                    )
                    eth_balance = self.diem._eth_balance_wei()
                    if (
                        gas_budget
                        and eth_balance is not None
                        and eth_balance < int(gas_budget.get("required_wei", 0))
                    ):
                        rationale.update(
                            {
                                "decision": "hold",
                                "reason": (
                                    "insufficient_gas_for_buy"
                                    if needs_buy
                                    else "insufficient_gas_funds"
                                ),
                                "execution_error": "insufficient_gas_funds",
                                "execution_error_class": "insufficient_gas_funds",
                                "eth_balance_wei": int(eth_balance),
                                "gas_required_wei": int(
                                    gas_budget.get("required_wei", 0)
                                ),
                                "gas_price_wei": gas_budget.get("effective_gas_price"),
                                "wallet_diem_units": wallet_diem_units,
                            }
                        )
                        self._last_rationale = rationale
                        return False
                except Exception:
                    pass

                if not self._check_factory_registration():
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "factory_registration_missing",
                        }
                    )
                    self._last_rationale = rationale
                    return False
                # Use buy_and_burn_diem helper for live execution
                try:
                    # Determine slippage to use
                    # Recompute after fallback adjustments to honor final slippage preview.
                    effective_slippage_bps = (
                        int(last_bps) if last_bps is not None else None
                    )
                    execution_slippage_bps = int(slippage_cap_bps)
                    slippage_override_enabled = os.getenv(
                        "DIEM_SLIPPAGE_OVERRIDE_ENABLE", "0"
                    ).strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    if slippage_override_enabled and effective_slippage_bps is not None:
                        execution_slippage_bps = effective_slippage_bps
                    rationale["execution_slippage_bps"] = execution_slippage_bps
                    execution_result = self.diem.wallet_first_buy_and_burn(
                        diem_amount=int(execution_target_units),
                        slippage_bps=execution_slippage_bps,
                        pool_take_bps=pool_take_bps,
                        simulate=False,
                        portfolio_snapshot=exec_ctx.snapshot,
                    )
                    # Record execution results in rationale
                    rationale["execution"] = execution_result
                    # Check if execution was successful
                    buy_result = (
                        execution_result.get("buy", {})
                        if isinstance(execution_result, dict)
                        else {}
                    )
                    overall_status = (
                        execution_result.get("status")
                        if isinstance(execution_result, dict)
                        else None
                    )
                    buy_status = "unknown"
                    if isinstance(buy_result, dict):
                        buy_status = buy_result.get(
                            "status", overall_status or "unknown"
                        )
                    if overall_status is None:
                        overall_status = buy_status
                    if overall_status in (
                        ExecutionStatus.REJECTED.value,
                        ExecutionStatus.FAILED.value,
                        "error",
                    ):
                        # Extract detailed error information from buy or burn results
                        error_msg = buy_result.get("error", "unknown_error")
                        diagnostics = buy_result.get("diagnostics", {})

                        # Also check burn result for errors (common with no_locked_svvv)
                        burn_result = (
                            execution_result.get("burn", {})
                            if isinstance(execution_result, dict)
                            else {}
                        )
                        if isinstance(burn_result, dict):
                            burn_error = burn_result.get("error")
                            if burn_error:
                                error_msg = burn_error
                            burn_steps = burn_result.get("steps", [])
                            for step in burn_steps:
                                if step.get("status") == "error" and step.get("error"):
                                    error_msg = step.get("error")
                                    break
                        error_class = "execution_error"
                        err_lower = str(error_msg).lower()

                        # Check for no_locked_svvv errors (DIEM purchased on DEX cannot be burned)
                        if "no_locked_svvv" in err_lower or (
                            isinstance(burn_result, dict)
                            and burn_result.get("error") == "no_locked_svvv"
                        ):
                            error_class = "no_locked_svvv"
                            logger.warning(
                                "Burn blocked: DIEM was purchased on DEX, not minted. "
                                "Cannot burn without locked sVVV. Recommend selling on DEX instead."
                            )
                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": "cannot_burn_purchased_diem",
                                    "execution_error": error_msg,
                                    "execution_error_class": error_class,
                                    "recommendation": "DIEM was purchased, not minted. "
                                    "Sell on DEX instead of burning.",
                                    "burn_eligibility": execution_result.get(
                                        "internal", {}
                                    ).get("burn_eligibility"),
                                }
                            )
                            self._last_rationale = rationale
                            return False
                        if "insufficient funds" in err_lower:
                            error_class = "insufficient_gas_funds"
                        elif "revert" in err_lower:
                            error_class = "execution_revert"

                        # Check if this is a liquidity error
                        is_liquidity_error = diagnostics.get(
                            "is_liquidity_error", False
                        )
                        # Also check error message patterns for liquidity issues
                        if not is_liquidity_error:
                            liquidity_keywords = [
                                "no executable",
                                "unhealthy",
                                "no pool",
                                "zero liquidity",
                                "revert",
                                "no quotes",
                                "all routes",
                            ]
                            is_liquidity_error = any(
                                keyword in str(error_msg).lower()
                                for keyword in liquidity_keywords
                            )

                        # Determine specific reason from diagnostics if available
                        if is_liquidity_error:
                            # Map liquidity errors to no_onchain_liquidity
                            reason = "no_onchain_liquidity"
                            # Update fair_value_components to reflect liquidity constraint
                            if "fair_value_components" in rationale:
                                fv_components = rationale.get(
                                    "fair_value_components", {}
                                )
                                if isinstance(fv_components, dict):
                                    fv_components["has_onchain_liquidity"] = False
                                    rationale["fair_value_components"] = fv_components
                            rationale["has_onchain_liquidity"] = False
                        else:
                            reason = (
                                error_class
                                if error_class != "execution_error"
                                else f"execution_{overall_status}"
                            )
                            if diagnostics:
                                if diagnostics.get(
                                    "bridge_route_available"
                                ) and not diagnostics.get("bridge_quotes_found"):
                                    reason = "execution_rejected_bridge_no_quotes"
                                elif (
                                    diagnostics.get("quotes_attempted", 0) > 0
                                    and diagnostics.get("valid_quotes", 0) == 0
                                ):
                                    reason = "execution_rejected_no_valid_quotes"
                                elif "no_quotes" in str(error_msg).lower():
                                    reason = "execution_rejected_no_quotes"

                            rationale.update(
                                {
                                    "decision": "hold",
                                    "reason": reason,
                                    "execution_error": error_msg,
                                    "execution_error_class": error_class,
                                    "execution_diagnostics": diagnostics,
                                }
                            )
                        self._last_rationale = rationale
                        return False
                except Exception as exc:
                    logger.error(f"buy_and_burn_diem failed: {exc}", exc_info=True)
                    rationale.update(
                        {
                            "decision": "hold",
                            "reason": "execution_exception",
                            "execution_error": str(exc),
                        }
                    )
                    self._last_rationale = rationale
                    return False
            self._last_rationale = rationale
            return True
        logger.info("No-op: market not favorable")
        _metrics_inc(
            "agent_decisions_total", labels={"agent": "arbi_diem", "action": "hold"}
        )
        rationale.update({"decision": "hold", "reason": "market_not_favorable"})
        self._last_rationale = rationale
        return False
