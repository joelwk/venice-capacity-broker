from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from libs.telemetry.logger import get_logger
from libs.telemetry.tracing import annotate_span

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # noqa: BLE001
    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return

logger = get_logger("workflow.orchestrator")


def _is_invalid_price(value: object) -> bool:
    try:
        if value is None:
            return True
        val = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(val) or val <= 0.0


def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Orchestrator:
    market: Any
    arbi: Any

    def run_once(self, mint_rate: float = 1.0, dry_run: bool = True) -> Dict[str, Any]:
        # In dry-run, avoid importing web3/DEX providers to prevent heavy deps or platform issues.
        # Maintain simple price history for realized volatility (non-persistent)
        try:
            if not hasattr(self, "_px_hist"):
                setattr(self, "_px_hist", [])
        except Exception:
            pass

        utilization_ratio: float | None = None
        vol_bps: float | None = None

        effective_mint_rate = float(mint_rate)
        mint_rate_source = "param"

        if dry_run:
            try:
                px = float(os.getenv("DIEM_FAKE_PRICE") or os.getenv("TEST_DIEM_PRICE") or 1.0)
            except Exception:
                px = 1.0
            prices: Dict[str, float] = {"DIEM": px}
            try:
                fake_rate = os.getenv("DIEM_FAKE_MINT_RATE") or os.getenv("DIEM_MINT_RATE")
                if fake_rate:
                    effective_mint_rate = float(fake_rate)
                    mint_rate_source = "env_dry_run"
            except Exception:
                pass
        else:
            # Warm minimal market signals and caches (best-effort)
            try:
                # Emits signal.market.signals and populates internal caches
                sig = self.market.unified_signals(ttl_s=30)
                # utilization from VVV metrics if available
                try:
                    vvv = sig.get("vvv") if isinstance(sig, dict) else None
                    if isinstance(vvv, dict):
                        ur = vvv.get("utilization")
                        if ur is not None:
                            utilization_ratio = float(ur)
                except Exception:
                    utilization_ratio = None
            except Exception:
                pass
            prices = self.market.prices(["DIEM", "VVV", "USDC"]) or {}
            px = float(prices.get("DIEM", 1.0))
            try:
                mint_info = self.market.diem_mint_rate(ttl_s=60)
                if isinstance(mint_info, dict):
                    candidate = mint_info.get("tokens_per_diem")
                    if candidate not in (None, 0):
                        effective_mint_rate = float(candidate)  # type: ignore[arg-type]
                        mint_rate_source = str(mint_info.get("source", "market"))
            except Exception:
                pass
            # Append to history and compute simple realized volatility
            try:
                hist = getattr(self, "_px_hist", [])
                hist.append(float(px))
                if len(hist) > 16:
                    del hist[: len(hist) - 16]
                setattr(self, "_px_hist", hist)
                vol_bps = float(self.arbi.risk.volatility_bps(hist)) if hasattr(self.arbi, "risk") else None
                # Optional: persist price ticks for analytics if DB configured
                import os as _os
                if (_os.getenv("RISK_VOL_PERSIST") or "false").strip().lower() in {"1", "true", "yes", "on"}:
                    try:
                        from db.session import create_db_and_tables
                        from sqlmodel import Session
                        from db.session import get_engine
                        from db.models import PriceTick
                        from datetime import datetime as _dt

                        create_db_and_tables()
                        eng = get_engine()
                        with Session(eng) as _s:  # type: ignore[call-arg]
                            _s.add(PriceTick(symbol="DIEM", price_usd=float(px), ts=_dt.utcnow()))
                            _s.commit()
                    except Exception:
                        pass
            except Exception:
                vol_bps = None
        # Optional portfolio cap wiring (env-gated)
        current_inventory_usd = None
        if not dry_run:
            try:
                import os as _os

                if (_os.getenv("RISK_ENABLE_PORTFOLIO_CAP") or "false").strip().lower() in {"1", "true", "yes", "on"}:
                    # Read inventory units from env (token base units)
                    def _i(name: str) -> int:
                        v = _os.getenv(name)
                        try:
                            return int(v) if v is not None and str(v).strip() != "" else 0
                        except Exception:
                            return 0

                    diem_u = _i("DIEM_INVENTORY_UNITS")
                    vvv_u = _i("VVV_INVENTORY_UNITS")
                    usdc_u = _i("USDC_INVENTORY_UNITS")
                    # Use arbi.risk exposure calculation
                    total_usd, _ = self.arbi.risk.exposure_usd(
                        diem_units=diem_u, vvv_units=vvv_u, usdc_units=usdc_u, prices_usd=prices
                    )
                    current_inventory_usd = float(total_usd)
            except Exception:
                current_inventory_usd = None

        corr = str(uuid.uuid4())
        if (os.getenv("AGENTS_PAUSED") or "false").strip().lower() in {"1", "true", "yes", "on"}:
            decision = False
        elif dry_run:
            # Use agent evaluation without sending on-chain actions
            try:
                import inspect as _ins

                params = _ins.signature(self.arbi.evaluate_and_maybe_mint).parameters  # type: ignore[attr-defined]
                if "simulate" in params and "corr_id" in params:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=effective_mint_rate,
                        desired_units=None,
                        current_inventory_usd=None,
                        corr_id=corr,
                        simulate=True,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
                elif "simulate" in params:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=effective_mint_rate,
                        desired_units=None,
                        current_inventory_usd=None,
                        simulate=True,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
                else:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=effective_mint_rate,
                        desired_units=None,
                        current_inventory_usd=None,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
            except Exception:
                decision = px > 0
        else:
            # Pass correlation id if agent supports it
            try:
                import inspect as _ins

                params = _ins.signature(self.arbi.evaluate_and_maybe_mint).parameters  # type: ignore[attr-defined]
                if "corr_id" in params:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=effective_mint_rate,
                        desired_units=None,
                        current_inventory_usd=current_inventory_usd,
                        corr_id=corr,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
                else:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=effective_mint_rate,
                        desired_units=None,
                        current_inventory_usd=current_inventory_usd,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
            except Exception:
                decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                    px, mint_rate=effective_mint_rate, desired_units=None, current_inventory_usd=current_inventory_usd
                )
        # Prefer agent-provided decision label when available
        last_why = getattr(self.arbi, "_last_rationale", None)
        action_label = None
        try:
            if isinstance(last_why, dict):
                lbl = last_why.get("decision")
                if isinstance(lbl, str) and lbl:
                    action_label = lbl
        except Exception:
            action_label = None
        record = {
            "agent": "arbi_diem",
            "action": action_label or ("mint_sell" if decision else "hold"),
            "price": px,
            "inventoryUsd": current_inventory_usd,
            "dry_run": dry_run,
            "correlationId": corr,
            "ts": time.time(),
            "mintRate": effective_mint_rate,
            "mintRateSource": mint_rate_source,
            "limits": {
                "slippage_bps_cap": getattr(self.arbi.risk, "slippage_bps_cap", None),
                "max_trade_usd": getattr(self.arbi.risk, "max_trade_usd", None),
                "max_inventory_usd": getattr(self.arbi.risk, "max_inventory_usd", None),
                "max_trade_units": getattr(self.arbi.risk, "max_trade_units", None),
            },
            "signals": {"utilization_ratio": utilization_ratio, "vol_bps": vol_bps},
            "outcome": bool(decision),
            "why": getattr(self.arbi, "_last_rationale", None),
        }
        try:
            _metrics_inc("agent_decisions_total", labels={"agent": str(record["agent"]), "action": str(record["action"])})
        except Exception:
            pass
        try:
            annotate_span({"orchestrator": record}, name="vvv.orchestrator.decision")
        except Exception:
            pass
        # Persist decision if SQL is available
        try:
            from sqlmodel import Session
            from db.session import get_engine, create_db_and_tables
            from db.models import Decision

            create_db_and_tables()
            eng = get_engine()
            with Session(eng) as s:  # type: ignore[call-arg]
                s.add(Decision(agent=str(record["agent"]), action=str(record["action"]), correlation_id=corr, details=json.dumps(record)))
                s.commit()
        except Exception:
            pass
        logger.info(f"orchestrator decision: {record}")
        return record

    def run_loop(self, interval_s: float = 5.0, backoff_s: float = 1.0, max_backoff_s: float = 60.0, dry_run: bool = True, max_cycles: int = 0) -> None:
        """Run orchestrator with interval and simple exponential backoff on error.

        max_cycles=0 means run indefinitely.
        """
        cycle = 0
        cur_backoff = float(backoff_s)
        while True:
            cycle += 1
            try:
                self.run_once(dry_run=dry_run)
                cur_backoff = float(backoff_s)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"orchestrator error: {e}; backing off {cur_backoff:.1f}s")
                time.sleep(cur_backoff)
                cur_backoff = min(cur_backoff * 2.0, float(max_backoff_s))
            else:
                time.sleep(max(0.0, float(interval_s)))
            if max_cycles and cycle >= int(max_cycles):
                break


@dataclass
class SingleLoopOrchestrator:
    """Sequential v1 loop: StakeMaster → ArbiDiem → CapacityBroker."""

    stake_master: Any
    arbi: Any
    capacity_broker: Any
    market: Any
    quorum: Optional[Any] = None
    parent_key: Optional[str] = None
    memory_store: Optional[Any] = None
    reflection: Optional[Any] = None
    reflex_guard: Optional[Any] = None

    def _summarize_capacity(self, cap_summary: Any) -> Dict[str, Any]:
        if not isinstance(cap_summary, dict):
            return {"status": cap_summary}
        summary: Dict[str, Any] = {"status": cap_summary.get("status")}
        violations = cap_summary.get("violations")
        if isinstance(violations, list):
            summary["violations"] = len(violations)
        enforce = cap_summary.get("enforce_limits")
        if enforce is not None:
            summary["enforce_limits"] = bool(enforce)
        usage = cap_summary.get("usage")
        if isinstance(usage, dict):
            data = usage.get("data")
            if isinstance(data, list):
                summary["usage_items"] = len(data)
            obj = usage.get("object")
            if isinstance(obj, str):
                summary["usage_object"] = obj
        limits = cap_summary.get("limits")
        if isinstance(limits, dict):
            keys = list(limits.keys())
            summary["limit_section_count"] = len(keys)
            if keys:
                summary["limit_sections"] = keys[:4]
        warning = cap_summary.get("warningMessage")
        if warning:
            summary["warning"] = str(warning)
        return summary

    def _log_cycle_payload(self, cycle_record: Dict[str, Any]) -> Dict[str, Any]:
        log_cycle = dict(cycle_record)
        cap_summary = self._summarize_capacity(cycle_record.get("capacity"))
        log_cycle["capacity"] = cap_summary
        agents = log_cycle.get("agents")
        if isinstance(agents, dict):
            agents_copy = dict(agents)
            if "capacity_broker" in agents_copy:
                agents_copy["capacity_broker"] = cap_summary
            log_cycle["agents"] = agents_copy
        return log_cycle

    def _price_guard_recent_stats(self, limit: int = 20) -> tuple[int, list[float]]:
        streak = 0
        diffs: list[float] = []
        records: list[Dict[str, Any]] = []
        if self.memory_store is not None:
            try:
                records = list(self.memory_store.recent(max(1, int(limit))))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"price guard history load failed: {exc}")
                records = []
        for entry in reversed(records):
            cycle = entry.get("cycle") if isinstance(entry, dict) else entry
            if not isinstance(cycle, dict):
                continue
            arbi = cycle.get("arbi")
            if not isinstance(arbi, dict):
                continue
            guard_info = arbi.get("priceGuard")
            if not isinstance(guard_info, dict):
                break
            if str(guard_info.get("reason")) != "price_guard":
                break
            streak += 1
            details = guard_info.get("details") if isinstance(guard_info.get("details"), dict) else {}
            diff_val = details.get("diff")
            if diff_val is None:
                price_health = arbi.get("priceHealth") if isinstance(arbi.get("priceHealth"), dict) else {}
                diff_val = price_health.get("diff")
            try:
                if diff_val is not None:
                    diffs.append(float(diff_val))
            except Exception:
                continue
        runtime_streak = getattr(self, "_price_guard_runtime_streak", 0)
        if streak == 0 and runtime_streak:
            streak = int(runtime_streak)
        return streak, diffs

    def _evaluate_price_guard_bypass(
        self,
        *,
        diff: Optional[float],
        threshold: Optional[float],
        streak_prev: int,
        streak_cap: int,
        min_streak: int,
        drift_cap: float,
        vol_bps: Optional[float],
        util_vol_bps: Optional[float],
        vol_cap: float,
        recent_diffs: list[float],
    ) -> Optional[Dict[str, Any]]:
        min_required = max(1, int(min_streak))
        if streak_prev < min_required:
            return None
        if streak_cap > 0 and streak_prev >= streak_cap:
            return None

        effective_diff: Optional[float] = None
        candidates: list[float] = []
        if diff is not None:
            candidates.append(float(diff))
        candidates.extend(reversed(recent_diffs))
        for candidate in candidates:
            try:
                effective = float(candidate)
            except Exception:
                continue
            else:
                effective_diff = effective
                break
        if effective_diff is None:
            return None
        if drift_cap > 0 and effective_diff > drift_cap:
            return None

        def _vol_ok(value: Optional[float]) -> bool:
            if vol_cap <= 0:
                return True
            if value is None:
                return True
            try:
                return float(value) <= vol_cap
            except Exception:
                return False

        if not _vol_ok(vol_bps) or not _vol_ok(util_vol_bps):
            return None

        return {
            "streak": streak_prev,
            "streak_cap": streak_cap,
            "min_streak": min_required,
            "diff": float(effective_diff),
            "drift_cap": float(drift_cap),
            "threshold": float(threshold) if threshold is not None else None,
            "vol_bps": float(vol_bps) if vol_bps is not None else None,
            "util_vol_bps": float(util_vol_bps) if util_vol_bps is not None else None,
        }

    def _progressive_threshold(self) -> int:
        try:
            raw = os.getenv("STAKEMASTER_PROGRESSIVE_CYCLES")
            if raw is None or str(raw).strip() == "":
                return 5
            return max(1, int(raw))
        except Exception:
            return 5

    def _prepare_progressive_state(self, enabled: bool) -> Optional[Dict[str, Any]]:
        setattr(self, "_progressive_requested", bool(enabled))
        if not enabled:
            return None
        if not _env_flag("STAKEMASTER_PROGRESSIVE_ENABLE", True):
            return None
        state = getattr(self, "_progressive_state", None)
        if state is None:
            state = {
                "counter": 0,
                "live": False,
                "threshold": self._progressive_threshold(),
                "enabled": True,
            }
            setattr(self, "_progressive_state", state)
        return state

    def _update_progressive_state(
        self,
        state: Dict[str, Any],
        cycle_record: Dict[str, Any],
        *,
        live_intent: bool,
    ) -> None:
        if not state or not live_intent:
            return
        stake = cycle_record.get("stake") if isinstance(cycle_record, dict) else None
        heartbeat_info = (stake or {}).get("heartbeat") if isinstance(stake, dict) else None
        heartbeat_sent = bool((heartbeat_info or {}).get("sent"))
        heartbeat_error = (heartbeat_info or {}).get("error") if isinstance(heartbeat_info, dict) else None
        forced_heartbeat = bool((heartbeat_info or {}).get("forced"))
        status_ok = (stake or {}).get("status") == "ok"
        allow_missing = _env_flag("STAKEMASTER_PROGRESSIVE_ALLOW_NO_HEARTBEAT", False)
        tolerate_error = heartbeat_error in {"venice_client_unavailable"}
        treat_as_success = heartbeat_sent and status_ok
        if not treat_as_success and status_ok and forced_heartbeat and allow_missing and tolerate_error:
            treat_as_success = True
            try:
                logger.info(
                    "progressive heartbeat bypass",
                    extra={"reason": heartbeat_error, "counter": int(state.get("counter", 0)) + 1},
                )
            except Exception:
                pass
        if treat_as_success:
            state["counter"] = int(state.get("counter", 0)) + 1
        else:
            state["counter"] = 0
        if heartbeat_error:
            state["last_heartbeat_error"] = heartbeat_error
        elif "last_heartbeat_error" in state:
            state.pop("last_heartbeat_error", None)
        threshold = max(1, int(state.get("threshold") or self._progressive_threshold()))
        if not state.get("live") and state.get("counter", 0) >= threshold:
            state["live"] = True
            state["enabled_at"] = time.time()
            try:
                logger.info(
                    "progressive live enabled",
                    extra={
                        "threshold": threshold,
                        "counter": state.get("counter", 0),
                    },
                )
            except Exception:
                pass

    def _invoke_arbi(
        self,
        price: float,
        *,
        mint_rate: float,
        current_inventory_usd: Optional[float],
        utilization_ratio: Optional[float],
        vol_bps: Optional[float],
        corr_id: Optional[str],
        simulate: Optional[bool],
    ) -> Any:
        import inspect as _ins

        params = _ins.signature(self.arbi.evaluate_and_maybe_mint).parameters  # type: ignore[attr-defined]
        kwargs: Dict[str, Any] = {}
        if "mint_rate" in params:
            kwargs["mint_rate"] = mint_rate
        if "desired_units" in params:
            kwargs["desired_units"] = None
        if "current_inventory_usd" in params:
            kwargs["current_inventory_usd"] = current_inventory_usd
        if "utilization_ratio" in params:
            kwargs["utilization_ratio"] = utilization_ratio
        if "vol_bps" in params:
            kwargs["vol_bps"] = vol_bps
        if corr_id is not None and "corr_id" in params:
            kwargs["corr_id"] = corr_id
        if simulate is not None and "simulate" in params:
            kwargs["simulate"] = simulate
        elif simulate and "simulate" not in params:
            logger.debug("ArbiDiem evaluate lacks simulate param; running without it")
        return self.arbi.evaluate_and_maybe_mint(price, **kwargs)

    def run_cycle(
        self,
        *,
        dry_run: bool = True,
        enable_live: bool = False,
        mint_rate: float = 1.0,
        progressive_live: bool = False,
    ) -> Dict[str, Any]:
        cycle_ts = time.time()

        # --- StakeMaster step ---
        try:
            stake_live_allowed = _env_flag("ORCHESTRATOR_STAKE_LIVE", False)
            stake_live = bool(enable_live and not dry_run and (stake_live_allowed or progressive_live))
            stake_result = self.stake_master.run_once(live=stake_live)
        except Exception as exc:  # noqa: BLE001
            stake_result = {"status": "error", "error": str(exc)}
            logger.warning(f"StakeMaster step failed: {exc}")

        # --- Market signals ---
        guard_streak_prev, guard_diffs_prev = self._price_guard_recent_stats()
        price_guard_bypass: Optional[Dict[str, Any]] = None
        utilization_ratio: Optional[float] = None
        vol_bps: Optional[float] = None
        util_vol_bps: Optional[float] = None
        effective_mint_rate = float(mint_rate)
        mint_rate_source = "param"
        prices: Dict[str, float] = {}
        price_health: Optional[Dict[str, Any]] = None
        skip_due_to_price = False
        price_guard_why: Optional[Dict[str, Any]] = None
        try:
            guard_streak_cap = int(os.getenv("ARBI_PRICE_GUARD_STREAK_MAX") or 15)
        except Exception:
            guard_streak_cap = 15
        try:
            guard_min_release = int(os.getenv("ARBI_PRICE_GUARD_MIN_STREAK") or 5)
        except Exception:
            guard_min_release = 5
        try:
            guard_drift_cap = float(os.getenv("ARBI_PRICE_GUARD_MAX_DRIFT") or 0.2)
        except Exception:
            guard_drift_cap = 0.2
        try:
            guard_vol_cap = float(os.getenv("ARBI_PRICE_GUARD_MAX_VOL_BPS") or 25.0)
        except Exception:
            guard_vol_cap = 25.0

        if dry_run:
            try:
                px = float(os.getenv("DIEM_FAKE_PRICE") or os.getenv("TEST_DIEM_PRICE") or 1.0)
            except Exception:
                px = 1.0
            if _is_invalid_price(px):
                try:
                    logger.warning("Dry-run DIEM price invalid; falling back to 1.0", extra={"value": px})
                except Exception:
                    pass
                px = 1.0
            prices = {"DIEM": px, "VVV": 0.0, "USDC": 1.0}
            try:
                fake_rate = os.getenv("DIEM_FAKE_MINT_RATE") or os.getenv("DIEM_MINT_RATE")
                if fake_rate:
                    effective_mint_rate = float(fake_rate)
                    mint_rate_source = "env_dry_run"
            except Exception:
                pass
            try:
                sig = self.market.unified_signals(ttl_s=30)
                if isinstance(sig, dict):
                    vvv = sig.get("vvv")
                    if isinstance(vvv, dict):
                        ur = vvv.get("utilization")
                        if ur is not None:
                            utilization_ratio = float(ur)
            except Exception:
                pass
            try:
                util_vol_bps = self.market.utilization_volatility_bps(window=3)
                if vol_bps in (None, 0.0) and util_vol_bps is not None:
                    vol_bps = float(util_vol_bps)
            except Exception:
                pass
        else:
            prices = self.market.prices(["DIEM", "VVV", "USDC"]) or {}
            px = float(prices.get("DIEM", 1.0))
            if _is_invalid_price(px) and not skip_due_to_price:
                skip_due_to_price = True
                price_guard_why = {"decision": "hold", "reason": "price_missing", "details": {"price": px}}
                price_health = {"valid": False, "reason": "missing_price", "source": "missing", "price": px}
                try:
                    logger.warning("Skipping ArbiDiem: DIEM price missing or invalid", extra={"price": px, "prices": prices})
                except Exception:
                    pass
                px = 0.0
            health_callable = getattr(self.market, "price_health", None)
            if callable(health_callable):
                try:
                    price_health = health_callable("DIEM", max_age=120.0)
                except Exception:
                    price_health = None
            if isinstance(price_health, dict):
                source = str(price_health.get("source") or "")
                clamped = bool(price_health.get("clamped"))
                valid_flag = price_health.get("valid")
                source_ok = source.startswith("aggregator")
                if clamped or (valid_flag is False) or not source_ok:
                    skip_due_to_price = True
                    price_guard_why = {"decision": "hold", "reason": "price_guard", "details": dict(price_health)}
                    if isinstance(price_guard_why.get("details"), dict):
                        price_guard_why["details"]["streak"] = guard_streak_prev + 1
                    logger.warning("Skipping ArbiDiem due to DIEM price health", extra={"price_health": price_health})
                    bypass = self._evaluate_price_guard_bypass(
                        diff=price_health.get("diff"),
                        threshold=price_health.get("threshold"),
                        streak_prev=guard_streak_prev,
                        streak_cap=guard_streak_cap,
                        min_streak=guard_min_release,
                        drift_cap=guard_drift_cap,
                        vol_bps=vol_bps,
                        util_vol_bps=util_vol_bps,
                        vol_cap=guard_vol_cap,
                        recent_diffs=guard_diffs_prev,
                    )
                    if bypass is not None:
                        skip_due_to_price = False
                        price_guard_bypass = dict(bypass)
                        price_guard_bypass["source"] = source
                        price_guard_bypass["streak_prev"] = guard_streak_prev
                        price_guard_bypass["vol_cap"] = guard_vol_cap
                        price_guard_bypass["drift_cap"] = guard_drift_cap
                        price_guard_why = {
                            "decision": "proceed",
                            "reason": "price_guard_bypass",
                            "details": dict(price_guard_bypass),
                        }
                        if isinstance(price_health, dict):
                            price_health["bypassed"] = True
                        try:
                            logger.info(
                                "Price guard bypassed after stable clamp",
                                extra={
                                    "streak": price_guard_bypass.get("streak"),
                                    "diff": price_guard_bypass.get("diff"),
                                    "vol_bps": price_guard_bypass.get("vol_bps"),
                                    "util_vol_bps": price_guard_bypass.get("util_vol_bps"),
                                },
                            )
                        except Exception:
                            pass
            try:
                sig = self.market.unified_signals(ttl_s=30)
                if isinstance(sig, dict):
                    vvv = sig.get("vvv")
                    if isinstance(vvv, dict):
                        ur = vvv.get("utilization")
                        if ur is not None:
                            utilization_ratio = float(ur)
            except Exception:
                pass
            try:
                mint_info = self.market.diem_mint_rate(ttl_s=60)
                if isinstance(mint_info, dict):
                    candidate = mint_info.get("tokens_per_diem")
                    if candidate not in (None, 0):
                        effective_mint_rate = float(candidate)  # type: ignore[arg-type]
                        mint_rate_source = str(mint_info.get("source", "market"))
            except Exception:
                pass
            try:
                hist = getattr(self, "_px_hist", [])
                hist.append(float(px))
                if len(hist) > 16:
                    del hist[: len(hist) - 16]
                setattr(self, "_px_hist", hist)
                if hasattr(self.arbi, "risk"):
                    vol_bps = float(self.arbi.risk.volatility_bps(hist))
                if (vol_bps is None or vol_bps <= 0.0) and hasattr(self.market, "utilization_volatility_bps"):
                    util_vol_bps = self.market.utilization_volatility_bps(window=3)
                    if util_vol_bps is not None:
                        vol_bps = float(util_vol_bps)
                elif hasattr(self.market, "utilization_volatility_bps"):
                    util_vol_bps = self.market.utilization_volatility_bps(window=3)
                if _env_flag("RISK_VOL_PERSIST", False):
                    try:
                        from db.session import create_db_and_tables, get_engine
                        from db.models import PriceTick
                        from sqlmodel import Session
                        from datetime import datetime as _dt

                        create_db_and_tables()
                        eng = get_engine()
                        with Session(eng) as s:  # type: ignore[call-arg]
                            s.add(PriceTick(symbol="DIEM", price_usd=float(px), ts=_dt.utcnow()))
                            s.commit()
                    except Exception:
                        pass
            except Exception:
                vol_bps = None

        current_inventory_usd: Optional[float] = None
        if not dry_run and _env_flag("RISK_ENABLE_PORTFOLIO_CAP", False):
            try:
                def _i(name: str) -> int:
                    v = os.getenv(name)
                    try:
                        return int(v) if v is not None and str(v).strip() != "" else 0
                    except Exception:
                        return 0

                diem_u = _i("DIEM_INVENTORY_UNITS")
                vvv_u = _i("VVV_INVENTORY_UNITS")
                usdc_u = _i("USDC_INVENTORY_UNITS")
                total_usd, _ = self.arbi.risk.exposure_usd(
                    diem_units=diem_u,
                    vvv_units=vvv_u,
                    usdc_units=usdc_u,
                    prices_usd=prices,
                )
                current_inventory_usd = float(total_usd)
            except Exception:
                current_inventory_usd = None

        correlation_id = str(uuid.uuid4())
        reflex_info: Optional[Dict[str, Any]] = None
        reflex_blocked = False
        if self.reflex_guard is not None:
            last_cycle = None
            if self.memory_store is not None:
                try:
                    hist = self.memory_store.recent(1)
                    last_cycle = hist[0] if hist else None
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"Reflex history lookup failed: {exc}")
                    last_cycle = None
            try:
                reflex_info = self.reflex_guard.evaluate(
                    price=px,
                    utilization=utilization_ratio,
                    vol_bps=vol_bps,
                    stake=stake_result,
                    dry_run=dry_run,
                    enable_live=enable_live,
                    last_cycle=last_cycle,
                )
                reflex_blocked = bool(reflex_info.get("halt"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Reflex guardian error: {exc}")
                reflex_info = {"halt": False, "error": str(exc)}

        paused = _env_flag("AGENTS_PAUSED", False)
        skip_agents = paused or reflex_blocked
        live_mode = bool(enable_live and not dry_run)

        signal_decision = False
        executed_decision = False
        quorum_info: Optional[Dict[str, Any]] = None
        execution_summary: Dict[str, Any] = {"status": "skipped", "executed": False}
        price_guard_triggered = False

        if skip_agents:
            if paused:
                execution_summary = {"status": "paused", "executed": False}
            elif reflex_blocked:
                execution_summary = {"status": "reflex_halt", "executed": False}
        else:
            if skip_due_to_price:
                execution_summary = {"status": "price_guard", "executed": False}
                signal_decision = False
                executed_decision = False
                price_guard_triggered = True
            else:
                try:
                    sim_result = self._invoke_arbi(
                        px,
                        mint_rate=effective_mint_rate,
                        current_inventory_usd=None if dry_run else current_inventory_usd,
                        utilization_ratio=utilization_ratio,
                        vol_bps=vol_bps,
                        corr_id=correlation_id,
                        simulate=True,
                    )
                except Exception:
                    sim_result = px > 0
                signal_decision = bool(sim_result)

                if signal_decision and live_mode:
                    quorum_allowed = True
                    if self.quorum is not None:
                        try:
                            quorum_allowed = bool(self.quorum.decide())
                            quorum_info = {"status": "approved" if quorum_allowed else "blocked"}
                        except Exception as exc:  # noqa: BLE001
                            quorum_allowed = False
                            quorum_info = {"status": "error", "error": str(exc)}
                    if quorum_allowed:
                        try:
                            live_result = self._invoke_arbi(
                                px,
                                mint_rate=effective_mint_rate,
                                current_inventory_usd=current_inventory_usd,
                                utilization_ratio=utilization_ratio,
                                vol_bps=vol_bps,
                                corr_id=correlation_id,
                                simulate=False,
                            )
                            executed_decision = bool(live_result)
                            execution_summary = {
                                "status": "executed" if executed_decision else "no_action",
                                "executed": executed_decision,
                            }
                        except Exception as exc:  # noqa: BLE001
                            execution_summary = {"status": "error", "error": str(exc), "executed": False}
                            executed_decision = False
                            signal_decision = False
                    else:
                        execution_summary = {"status": quorum_info.get("status", "blocked"), "executed": False}
                        executed_decision = False
                elif signal_decision:
                    execution_summary = {"status": "dry_run", "executed": False}

        if reflex_blocked:
            last_why = {"decision": "hold", "reason": "reflex_guard", "details": reflex_info}
        else:
            if price_guard_triggered and price_guard_why is not None:
                last_why = price_guard_why
            elif price_guard_bypass is not None and price_guard_why is not None:
                last_why = price_guard_why
            else:
                last_why = getattr(self.arbi, "_last_rationale", None)
        action_label = None
        try:
            if isinstance(last_why, dict):
                lbl = last_why.get("decision")
                if isinstance(lbl, str) and lbl:
                    action_label = lbl
        except Exception:
            action_label = None

        if reflex_blocked:
            signal_decision = False
            executed_decision = False

        arbi_record = {
            "agent": "arbi_diem",
            "action": action_label or ("mint_sell" if signal_decision else "hold"),
            "price": px,
            "inventoryUsd": current_inventory_usd,
            "dry_run": dry_run,
            "correlationId": None if skip_agents else correlation_id,
            "ts": cycle_ts,
            "mintRate": effective_mint_rate,
            "mintRateSource": mint_rate_source,
            "limits": {
                "slippage_bps_cap": getattr(self.arbi.risk, "slippage_bps_cap", None),
                "max_trade_usd": getattr(self.arbi.risk, "max_trade_usd", None),
                "max_inventory_usd": getattr(self.arbi.risk, "max_inventory_usd", None),
                "max_trade_units": getattr(self.arbi.risk, "max_trade_units", None),
            },
            "signals": {
                "utilization_ratio": utilization_ratio,
                "vol_bps": vol_bps,
                "utilization_vol_bps": util_vol_bps,
            },
            "outcome": bool(signal_decision if (dry_run or not live_mode) else executed_decision),
            "why": last_why,
            "execution": execution_summary,
            "signalDecision": bool(signal_decision),
        }
        if price_health is not None:
            arbi_record["priceHealth"] = price_health
        if price_guard_triggered:
            guard_info = {"status": "skipped", "reason": "price_guard"}
            if isinstance(price_guard_why, dict) and isinstance(price_guard_why.get("details"), dict):
                guard_info["details"] = price_guard_why["details"]
            arbi_record["priceGuard"] = guard_info
        elif price_guard_bypass is not None:
            guard_info = {"status": "bypassed", "reason": "price_guard", "details": price_guard_bypass}
            arbi_record["priceGuard"] = guard_info
            arbi_record["priceGuardBypass"] = price_guard_bypass
        if quorum_info is not None:
            arbi_record["quorum"] = quorum_info
        if reflex_info is not None:
            arbi_record["reflex"] = reflex_info

        try:
            _metrics_inc("agent_decisions_total", labels={"agent": "arbi_diem", "action": str(arbi_record["action"])})
        except Exception:
            pass
        try:
            annotate_span({"single_loop": arbi_record}, name="vvv.orchestrator.single_loop")
        except Exception:
            pass

        try:
            cap_summary = self.capacity_broker.run_once(parent_key=self.parent_key)
        except Exception as exc:  # noqa: BLE001
            cap_summary = {"status": "error", "error": str(exc)}

        cycle_record = {
            "ts": cycle_ts,
            "stake": stake_result,
            "arbi": arbi_record,
            "capacity": cap_summary,
        }
        cycle_record["agents"] = {
            "stake_master": stake_result,
            "arbi_diem": arbi_record,
            "capacity_broker": cap_summary,
        }
        cycle_record["reflex"] = reflex_info
        prog_state = getattr(self, "_progressive_state", None)
        if isinstance(prog_state, dict):
            prog_snapshot = dict(prog_state)
        else:
            prog_snapshot = None
        cycle_record["progressive"] = {
            "requested": bool(getattr(self, "_progressive_requested", False)),
            "override": bool(progressive_live),
            "live": bool(enable_live and not dry_run),
            "state": prog_snapshot,
        }

        if price_guard_triggered:
            setattr(self, "_price_guard_runtime_streak", guard_streak_prev + 1)
        else:
            setattr(self, "_price_guard_runtime_streak", 0)

        if self.reflection is not None:
            try:
                history_limit = getattr(self.reflection, "lookback", 10)
                history = None
                if self.memory_store is not None:
                    history = self.memory_store.recent(int(history_limit))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Reflection history fetch failed: {exc}")
                history = None
            try:
                reflection = self.reflection.reflect(cycle_record, history=history)
                if reflection:
                    cycle_record["reflection"] = reflection
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Reflection engine error: {exc}")

        if self.memory_store is not None:
            try:
                self.memory_store.record_cycle(cycle_record)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Memory store write failed: {exc}")

        log_payload = self._log_cycle_payload(cycle_record)
        logger.info(f"single-loop cycle: {log_payload}")
        return cycle_record

    def run_loop(
        self,
        *,
        interval_s: float = 15.0,
        max_cycles: int = 0,
        dry_run: bool = True,
        enable_live: bool = False,
        mint_rate: float = 1.0,
        progressive_live: bool = False,
    ) -> None:
        cycle = 0
        base_dry = bool(dry_run)
        base_live = bool(enable_live)
        progressive_state = self._prepare_progressive_state(progressive_live or enable_live)
        live_intent = bool(base_live or (progressive_state is not None and progressive_live))

        while True:
            cycle += 1
            if progressive_state is not None:
                current_live = bool(progressive_state.get("live"))
                enable_flag = current_live
                dry_flag = not current_live
                progressive_override = current_live
            else:
                enable_flag = live_intent
                dry_flag = base_dry
                progressive_override = False

            cycle_record: Optional[Dict[str, Any]] = None
            try:
                cycle_record = self.run_cycle(
                    dry_run=dry_flag,
                    enable_live=enable_flag,
                    mint_rate=mint_rate,
                    progressive_live=progressive_override,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"single-loop error: {exc}")
            else:
                if progressive_state is not None and cycle_record is not None:
                    self._update_progressive_state(progressive_state, cycle_record, live_intent=live_intent)
            if max_cycles and cycle >= max_cycles:
                break
            time.sleep(max(0.0, float(interval_s)))
