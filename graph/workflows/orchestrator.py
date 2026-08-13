from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agents.quorum.models import QuorumContext
from libs.telemetry.logger import get_logger
from libs.telemetry.tracing import annotate_span

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:

    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return


try:
    from libs.telemetry.metrics import set_gauge as _metrics_set_gauge
except Exception:

    def _metrics_set_gauge(name: str, value: float, labels: dict | None = None) -> None:  # type: ignore
        return


try:
    from libs.telemetry.events import emit_event as _emit_event
except Exception:

    def _emit_event(name: str, payload: dict | None = None) -> None:  # type: ignore
        return


# Environment helper (production detection)
try:
    from libs.env import is_production  # type: ignore
except Exception:

    def is_production() -> bool:  # type: ignore
        return (os.getenv("APP_ENV") or "").strip().lower() in {"production", "prod"}


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


def _mint_rate_force_env() -> bool:
    """When true, allow DIEM_MINT_RATE env overrides to mask the on-chain mint rate."""

    return _env_flag("DIEM_MINT_RATE_FORCE", False) or _env_flag(
        "DIEM_MINT_RATE_FORCE_ENV", False
    )


def _resolve_diem_fv_adoption_base() -> float:
    """Resolve the DIEM FV adoption baseline (0..1) from env with safe defaults."""
    for name in ("DIEM_FV_ADOPTION_BASE", "DIEM_ADOPTION_BASE"):
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(raw)
        except Exception:
            continue
        if not math.isfinite(val):
            continue
        return max(0.0, min(1.0, val))
    return 0.60


def _resolve_utilization_fallback() -> tuple[float, str]:
    """Return (utilization_ratio, utilization_source) when live utilization is unavailable."""
    hint = os.getenv("MARKETDATA_UTILIZATION_HINT")
    if hint is not None and str(hint).strip() != "":
        try:
            val = float(hint)
            if math.isfinite(val):
                return max(0.0, min(1.0, val)), "hint"
        except Exception:
            pass
    return _resolve_diem_fv_adoption_base(), "fallback"


def _resolve_diem_price_for_dry_run(market: Any) -> tuple[float, dict[str, float]]:
    """Resolve DIEM price for dry-run cycles.

    Prefer live market data when available and fall back to DIEM_FAKE_* env vars
    or a conservative default only when needed.
    """
    fake_raw = os.getenv("DIEM_FAKE_PRICE") or os.getenv("TEST_DIEM_PRICE")
    fake_px: float | None = None
    if fake_raw:
        try:
            candidate = float(fake_raw)
            if not _is_invalid_price(candidate):
                fake_px = candidate
        except Exception:
            fake_px = None

    real_prices: dict[str, float] = {}
    real_px: float | None = None
    try:
        real_prices = market.prices(["DIEM", "VVV", "USDC"]) or {}
        raw_real = real_prices.get("DIEM")
        if raw_real not in (None, 0, 0.0):
            try:
                value = float(raw_real)  # type: ignore[arg-type]
            except Exception:
                value = 0.0
            if not _is_invalid_price(value):
                real_px = value
    except Exception as exc:
        try:
            if fake_px is not None:
                logger.warning(
                    "DRY-RUN MODE: Using fake DIEM price $%.2f; failed to fetch real prices (%s)",
                    fake_px,
                    exc,
                )
            else:
                logger.warning(
                    "DRY-RUN MODE: Failed to fetch real DIEM price; falling back to $1.00 (%s)",
                    exc,
                )
        except Exception:
            pass
        real_prices = {}
        real_px = None

    # If primary price failed, try bridge_vvv fallback via diem_price_with_fallback
    if real_px is None:
        try:
            bridge_px_method = getattr(market, "diem_price_with_fallback", None)
            if callable(bridge_px_method):
                bridge_result = bridge_px_method()
                if bridge_result is not None and not _is_invalid_price(
                    float(bridge_result)
                ):
                    real_px = float(bridge_result)
                    real_prices["DIEM"] = real_px
                    logger.info(
                        "DRY-RUN MODE: Using bridge_vvv fallback DIEM price $%.4f",
                        real_px,
                    )
        except Exception as exc:
            logger.debug("DRY-RUN MODE: bridge_vvv fallback failed: %s", exc)

    if real_px is not None:
        px = real_px
        try:
            if fake_px is not None:
                logger.info(
                    "DRY-RUN MODE: Using real DIEM price $%.4f (DIEM_FAKE_PRICE override ignored)",
                    px,
                )
            else:
                logger.info(
                    "DRY-RUN MODE: Using real DIEM price $%.4f for simulations",
                    px,
                )
        except Exception:
            pass
    elif fake_px is not None:
        px = fake_px
        try:
            logger.warning(
                "DRY-RUN MODE: Using fake DIEM price $%.2f, real DIEM price unavailable",
                px,
            )
        except Exception:
            pass
    else:
        px = 1.0
        try:
            logger.warning(
                "DRY-RUN MODE: Using fallback DIEM price $%.2f; real DIEM price unavailable",
                px,
            )
        except Exception:
            pass

    return float(px), real_prices


@dataclass
class Orchestrator:
    market: Any
    arbi: Any
    _last_cycle_dry_run: bool | None = field(default=None, init=False, repr=False)

    def _propagate_run_mode(self, dry_run: bool) -> None:
        transition_to_live = False
        if self._last_cycle_dry_run is not None:
            if self._last_cycle_dry_run and not dry_run:
                transition_to_live = True
        self._last_cycle_dry_run = dry_run

        # Clear price history on dry-run to live transition to prevent fallback prices
        # contaminating live volatility calculations
        if transition_to_live:
            self._px_hist = []
            logger.info("Cleared price history on dry-run to live transition")

        handler = getattr(self.arbi, "on_run_mode", None)
        if callable(handler):
            try:
                handler(dry_run=dry_run, transitioned_to_live=transition_to_live)
            except Exception as exc:
                logger.debug("Failed to propagate run mode to ArbiDiem: %s", exc)

    def run_once(
        self, mint_rate: float | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        # In dry-run, avoid importing web3/DEX providers to prevent heavy deps or platform issues.
        # Maintain simple price history for realized volatility (non-persistent)
        try:
            if not hasattr(self, "_px_hist"):
                self._px_hist = []  # type: ignore[attr-defined]
        except Exception:
            pass

        self._propagate_run_mode(dry_run)

        utilization_ratio: float | None = None
        utilization_source: str | None = None
        vol_bps: float | None = None

        # Let ArbiDiem resolve mint rate from on-chain if not explicitly provided
        effective_mint_rate: float | None = mint_rate
        mint_rate_source = "param" if mint_rate is not None else None

        if dry_run:
            px, real_prices = _resolve_diem_price_for_dry_run(self.market)
            prices = {
                "DIEM": px,
                "VVV": float(real_prices.get("VVV", 0.0) or 0.0),
                "USDC": float(real_prices.get("USDC", 1.0) or 1.0),
            }
            try:
                fake_rate = os.getenv("DIEM_FAKE_MINT_RATE") or os.getenv(
                    "DIEM_MINT_RATE"
                )
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
                            utilization_source = "venice"
                except Exception:
                    utilization_ratio = None
                    utilization_source = None
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
            # If market mint-rate is unavailable, fall back to on-chain mint-rate from the DIEM client.
            # Only do this when the caller didn't override the mint rate (default is 1.0).
            try:
                param_is_default = abs(float(mint_rate) - 1.0) < 1e-12
            except Exception:
                param_is_default = True
            # Treat env mint-rate = 1.0 as a placeholder unless explicitly forced.
            force_env = _mint_rate_force_env()
            market_is_env = str(mint_rate_source).startswith("env")
            placeholder_env = False
            try:
                placeholder_env = (
                    market_is_env
                    and not force_env
                    and abs(float(effective_mint_rate) - 1.0) < 1e-9
                )
            except Exception:
                placeholder_env = False

            if (
                placeholder_env
                or (mint_rate_source == "param" and param_is_default)
                or effective_mint_rate
                in (
                    None,
                    0,
                    0.0,
                )
            ):
                try:
                    diem = getattr(self.arbi, "diem", None)
                    query_fn = getattr(diem, "_query_mint_rate_onchain_safe", None)
                    if callable(query_fn):
                        units = query_fn()
                        if units not in (None, 0):
                            # services.diem.client returns (sVVV per DIEM) scaled by 1e18.
                            # Convert to token-per-token ratio, respecting decimals when provided.
                            try:
                                diem_dec = int(os.getenv("DIEM_DECIMALS") or "18")
                            except Exception:
                                diem_dec = 18
                            try:
                                svvv_dec = int(
                                    os.getenv("SVVV_DECIMALS")
                                    or os.getenv("VVV_DECIMALS")
                                    or "18"
                                )
                            except Exception:
                                svvv_dec = 18
                            scale = float(10 ** max(0, int(diem_dec))) / float(
                                10 ** max(0, int(svvv_dec))
                            )
                            effective_mint_rate = (float(units) / 1e18) * float(scale)
                            mint_rate_source = "onchain"
                except Exception:
                    pass
            # Append to history and compute simple realized volatility
            try:
                hist = getattr(self, "_px_hist", [])
                # Price continuity guard: detect and reset on extreme jumps
                max_jump_ratio = 0.5  # 50% max deviation from last price
                if hist:
                    last_px = hist[-1]
                    if last_px > 0:
                        deviation = abs(float(px) / last_px - 1.0)
                        if deviation > max_jump_ratio:
                            logger.warning(
                                "Price history discontinuity detected: %.4f -> %.4f (%.1f%% jump), resetting history",
                                last_px,
                                float(px),
                                deviation * 100,
                            )
                            hist = []  # Reset history on extreme jump
                hist.append(float(px))
                if len(hist) > 16:
                    del hist[: len(hist) - 16]
                self._px_hist = hist
                vol_bps = (
                    float(self.arbi.risk.volatility_bps(hist))
                    if hasattr(self.arbi, "risk")
                    else None
                )
                # Optional: persist price ticks for analytics if DB configured
                import os as _os

                if (_os.getenv("RISK_VOL_PERSIST") or "false").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    try:
                        from datetime import datetime as _dt

                        from sqlmodel import Session

                        from db.models import PriceTick
                        from db.session import create_db_and_tables, get_engine

                        # Production-like environments require explicit SQL_CREATE_ALL_ON_START=true
                        _prod_like = bool(
                            _os.getenv("SQL_DATABASE_URL")
                            or _os.getenv("DATABASE_URL")
                            or _os.getenv("POSTGRES_HOST")
                        )
                        _create_all_env = _os.getenv("SQL_CREATE_ALL_ON_START")

                        if _prod_like:
                            # Production: only create if explicitly enabled
                            _create_all = (
                                _create_all_env is not None
                                and _create_all_env.strip().lower()
                                in {"1", "true", "yes", "on"}
                            )
                        else:
                            # Non-production: default to True unless explicitly disabled
                            _create_all = (
                                _create_all_env is None
                                or _create_all_env.strip().lower()
                                in {"1", "true", "yes", "on"}
                            )

                        if _create_all:
                            create_db_and_tables()
                        eng = get_engine()
                        with Session(eng) as _s:  # type: ignore[call-arg]
                            _s.add(
                                PriceTick(
                                    symbol="DIEM", price_usd=float(px), ts=_dt.utcnow()
                                )
                            )
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

                if (
                    _os.getenv("RISK_ENABLE_PORTFOLIO_CAP") or "false"
                ).strip().lower() in {"1", "true", "yes", "on"}:
                    # Try portfolio inventory service first, fallback to env vars
                    inventory_usd: float | None = None
                    if (
                        hasattr(self, "portfolio_inventory")
                        and self.portfolio_inventory is not None
                    ):
                        try:
                            portfolio_snapshot = self.portfolio_inventory.snapshot(
                                include_eth=False
                            )
                            inventory_usd = portfolio_snapshot.inventory_usd
                        except Exception:
                            pass

                    # Fallback to env vars if portfolio service unavailable
                    if inventory_usd is None or inventory_usd <= 0:

                        def _i(name: str) -> int:
                            v = _os.getenv(name)
                            try:
                                return (
                                    int(v)
                                    if v is not None and str(v).strip() != ""
                                    else 0
                                )
                            except Exception:
                                return 0

                        diem_u = _i("DIEM_INVENTORY_UNITS")
                        vvv_u = _i("VVV_INVENTORY_UNITS")
                        usdc_u = _i("USDC_INVENTORY_UNITS")
                        # Use arbi.risk exposure calculation
                        total_usd, _ = self.arbi.risk.exposure_usd(
                            diem_units=diem_u,
                            vvv_units=vvv_u,
                            usdc_units=usdc_u,
                            prices_usd=prices,
                        )
                        inventory_usd = float(total_usd)

                    current_inventory_usd = inventory_usd
            except Exception:
                current_inventory_usd = None

        if not dry_run and utilization_ratio is None:
            try:
                utilization_ratio, utilization_source = _resolve_utilization_fallback()
                _metrics_inc(
                    "orchestrator_utilization_fallback_total",
                    labels={"source": str(utilization_source)},
                )
                logger.warning(
                    "utilization_source=%s utilization_ratio=%.4f (live utilization unavailable)",
                    utilization_source,
                    float(utilization_ratio),
                )
            except Exception:
                utilization_ratio = None
                utilization_source = None

        corr = str(uuid.uuid4())
        if (os.getenv("AGENTS_PAUSED") or "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            decision = False
        elif dry_run:
            # Use agent evaluation without sending on-chain actions
            try:
                import inspect as _ins

                params = _ins.signature(self.arbi.evaluate_and_maybe_mint).parameters  # type: ignore[attr-defined]
                kwargs: dict[str, Any] = {
                    "mint_rate": effective_mint_rate,
                    "desired_units": None,
                    "current_inventory_usd": None,
                }
                if "utilization_ratio" in params:
                    kwargs["utilization_ratio"] = utilization_ratio
                if "vol_bps" in params:
                    kwargs["vol_bps"] = vol_bps
                if "mint_rate_source" in params:
                    kwargs["mint_rate_source"] = mint_rate_source
                if "simulate" in params:
                    kwargs["simulate"] = True
                if "corr_id" in params:
                    kwargs["corr_id"] = corr

                decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                    px,
                    **kwargs,
                )
            except Exception:
                decision = px > 0
        else:
            # Pass correlation id if agent supports it
            try:
                import inspect as _ins

                params = _ins.signature(self.arbi.evaluate_and_maybe_mint).parameters  # type: ignore[attr-defined]
                kwargs = {
                    "mint_rate": effective_mint_rate,
                    "desired_units": None,
                    "current_inventory_usd": current_inventory_usd,
                }
                if "utilization_ratio" in params:
                    kwargs["utilization_ratio"] = utilization_ratio
                if "vol_bps" in params:
                    kwargs["vol_bps"] = vol_bps
                if "mint_rate_source" in params:
                    kwargs["mint_rate_source"] = mint_rate_source
                if "corr_id" in params:
                    kwargs["corr_id"] = corr

                if "corr_id" in params:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        **kwargs,
                    )
                else:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        **kwargs,
                    )
            except Exception:
                decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                    px,
                    mint_rate=effective_mint_rate,
                    desired_units=None,
                    current_inventory_usd=current_inventory_usd,
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
            "signals": {
                "utilization_ratio": utilization_ratio,
                "utilization_source": utilization_source,
                "vol_bps": vol_bps,
            },
            "outcome": bool(decision),
            "why": getattr(self.arbi, "_last_rationale", None),
        }
        try:
            _metrics_inc(
                "agent_decisions_total",
                labels={"agent": str(record["agent"]), "action": str(record["action"])},
            )
        except Exception:
            pass
        try:
            annotate_span({"orchestrator": record}, name="vvv.orchestrator.decision")
        except Exception:
            pass
        # Persist decision if SQL is available
        try:
            from sqlmodel import Session

            from db.models import Decision
            from db.session import get_engine

            eng = get_engine()
            with Session(eng) as s:  # type: ignore[call-arg]
                s.add(
                    Decision(
                        agent=str(record["agent"]),
                        action=str(record["action"]),
                        correlation_id=corr,
                        details=json.dumps(record),
                    )
                )
                s.commit()
        except Exception as _e:
            try:
                _metrics_inc("sql_persist_error_total", labels={"entity": "decision"})
            except Exception:
                pass
            if is_production():
                logger.critical(
                    "decision persistence failed; aborting in production: %s", _e
                )
                raise
            logger.warning("decision persistence failed (dev mode): %s", _e)
        logger.info(f"orchestrator decision: {record}")
        return record

    def run_loop(
        self,
        interval_s: float = 5.0,
        backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        dry_run: bool = True,
        max_cycles: int = 0,
    ) -> None:
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
            except Exception as e:
                logger.warning(
                    f"orchestrator error: {e}; backing off {cur_backoff:.1f}s"
                )
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
    quorum: Any | None = None
    ai_treasurer: Any | None = None
    parent_key: str | None = None
    memory_store: Any | None = None
    reflection: Any | None = None
    reflex_guard: Any | None = None
    portfolio_inventory: Any | None = None
    _last_cycle_dry_run: bool | None = field(default=None, init=False, repr=False)
    _cycle_count: int = field(default=0, init=False, repr=False)
    _low_usdc_warned_cycle: int = field(default=0, init=False, repr=False)
    _stake_recommendation: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )
    _pending_stake_recommendation: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._last_listen_interval: float | None = None
        self._last_capacity_usage: dict[str, Any] | None = None
        self._startup_quote_consolidation: dict[str, Any] | None = None
        self._stake_recommendation = None
        self._pending_stake_recommendation = None

    def _propagate_run_mode(self, dry_run: bool) -> None:
        """Notify components about current run mode and transition events.

        Mirrors the simple propagation used by `Orchestrator` while
        supporting the v1 single-loop components.
        """
        transition_to_live = False
        if self._last_cycle_dry_run is not None:
            if self._last_cycle_dry_run and not dry_run:
                transition_to_live = True
        self._last_cycle_dry_run = dry_run

        # Propagate to components that expose `on_run_mode(dry_run, transitioned_to_live)`.
        for comp in (self.arbi, self.stake_master, self.capacity_broker):
            handler = getattr(comp, "on_run_mode", None)
            if callable(handler):
                try:
                    handler(dry_run=dry_run, transitioned_to_live=transition_to_live)
                except Exception as exc:
                    try:
                        logger.debug(
                            "Failed to propagate run mode to %s: %s", comp, exc
                        )
                    except Exception:
                        pass

        if hasattr(self.stake_master, "ingest_recommendation"):
            try:
                self.stake_master.ingest_recommendation(
                    self._pending_stake_recommendation or self._stake_recommendation
                )
                self._pending_stake_recommendation = None
            except Exception:
                pass

    def _extract_stake_recommendation(self, rationale: Any) -> dict[str, Any] | None:
        if not isinstance(rationale, dict):
            return None
        rec = rationale.get("stake_recommendation") or rationale.get(
            "stakeRecommendation"
        )
        if isinstance(rec, dict):
            try:
                rec_copy = dict(rec)
            except Exception:
                rec_copy = rec
            rec_copy.setdefault("source", "arbi_diem")
            if "correlation_id" not in rec_copy and rationale.get("correlation_id"):
                rec_copy["correlation_id"] = rationale.get("correlation_id")
            return rec_copy
        if str(rationale.get("reason", "")).lower() != "insufficient_svvv":
            return None
        mint_check = rationale.get("mint_check")
        if not isinstance(mint_check, dict):
            return None
        try:
            required = int(mint_check.get("required_svvv"))
            available = int(mint_check.get("available_svvv"))
        except Exception:
            return None
        shortfall = max(0, required - available)
        if shortfall <= 0:
            return None
        rec = {
            "action": "stake_vvv",
            "reason": "insufficient_svvv",
            "required_units": required,
            "available_units": available,
            "shortfall_units": shortfall,
            "source": "arbi_diem",
        }
        try:
            mint_needed = rationale.get("mint_needed_units")
            if mint_needed is not None:
                rec["mint_needed_units"] = int(mint_needed)
        except Exception:
            pass
        if rationale.get("mint_rate") is not None:
            rec["mint_rate"] = rationale.get("mint_rate")
        if rationale.get("correlation_id"):
            rec["correlation_id"] = rationale.get("correlation_id")
        return rec

    def _dynamic_listen_enabled(self) -> bool:
        return _env_flag("ORCHESTRATOR_DYNAMIC_LISTEN", True)

    def _compute_listen_interval(
        self, base_interval: float, cycle_record: dict[str, Any]
    ) -> float:
        if not self._dynamic_listen_enabled():
            self._last_listen_interval = base_interval
            return base_interval
        min_interval = float(os.getenv("ORCHESTRATOR_INTERVAL_MIN", "5.0"))
        max_interval = float(os.getenv("ORCHESTRATOR_INTERVAL_MAX", "60.0"))
        vol_ref = max(1.0, float(os.getenv("ORCHESTRATOR_VOL_REF_BPS", "25.0")))
        interval = float(base_interval)

        arbi_block = cycle_record.get("arbi") or {}
        signals = arbi_block.get("signals") or {}
        vol_bps = float(signals.get("vol_bps") or 0.0)
        util = float(signals.get("utilization_ratio") or 0.0)
        quorum_info = arbi_block.get("quorum") or {}
        quorum_confidence = 0.0
        if isinstance(quorum_info, dict):
            conf = quorum_info.get("confidence")
            if isinstance(conf, (int, float)):
                quorum_confidence = max(0.0, min(1.0, float(conf)))
        stress = max(
            util,
            min(1.0, vol_bps / vol_ref),
            quorum_confidence,
        )
        if bool(arbi_block.get("signalDecision")):
            stress = max(stress, 0.6)

        if stress >= 0.8:
            interval = base_interval * 0.5
        elif stress >= 0.5:
            interval = base_interval * 0.75
        elif stress <= 0.2:
            interval = base_interval * 1.3

        interval = max(min_interval, min(max_interval, interval))
        self._last_listen_interval = interval
        return interval

    def _update_quorum_context(
        self,
        *,
        price: float,
        mint_rate: float,
        utilization_ratio: float | None,
        vol_bps: float | None,
        stake_result: Any,
        rationale: Any,
        reflex: dict[str, Any] | None,
        price_guard: dict[str, Any] | None,
        inventory_usd: float | None,
        dry_run: bool,
        live_mode: bool,
        simulate_decision: bool,
    ) -> None:
        if self.quorum is None or not hasattr(self.quorum, "update"):
            return
        try:
            premium: float | None = None
            suggested: int | None = None
            if isinstance(rationale, dict):
                raw_premium = rationale.get("premium")
                try:
                    premium = float(raw_premium) if raw_premium is not None else None
                except Exception:
                    premium = None
                raw_suggested = rationale.get("suggested_units")
                try:
                    suggested = (
                        int(raw_suggested) if raw_suggested is not None else None
                    )
                except Exception:
                    suggested = None
            # Extract execution preview from rationale if available
            execution_preview = None
            if isinstance(rationale, dict):
                preview_data = rationale.get("execution_preview")
                if isinstance(preview_data, dict):
                    execution_preview = preview_data
                # Also check if rationale contains preview fields directly
                elif rationale.get("exec_price_preview") is not None:
                    execution_preview = {
                        "effective_price": rationale.get("exec_price_preview"),
                        "slippage_bps": rationale.get("slippage_bps"),
                        "slippage_ok": rationale.get("slippage_ok"),
                        "route": rationale.get("tradeRoute"),
                    }

            ctx = QuorumContext(
                price=float(price),
                mint_rate=float(mint_rate),
                premium=premium,
                suggested_units=suggested,
                utilization_ratio=utilization_ratio,
                vol_bps=vol_bps,
                inventory_usd=inventory_usd,
                stake=stake_result if isinstance(stake_result, dict) else None,
                rationale=rationale if isinstance(rationale, dict) else None,
                reflex=reflex if isinstance(reflex, dict) else reflex,
                price_guard=price_guard if isinstance(price_guard, dict) else None,
                capacity_usage=(
                    self._last_capacity_usage
                    if isinstance(self._last_capacity_usage, dict)
                    else None
                ),
                execution_preview=execution_preview,
                dry_run=dry_run,
                live_mode=live_mode,
                simulate_decision=bool(simulate_decision),
            )
            self.quorum.update(ctx)  # type: ignore[attr-defined]
        except Exception as exc:
            try:
                logger.debug(f"Quorum context update failed: {exc}")
            except Exception:
                pass

    def _capture_capacity_usage(self, summary: Any) -> None:
        if not isinstance(summary, dict):
            return
        usage = summary.get("usage")
        if isinstance(usage, dict):
            self._last_capacity_usage = usage
        elif usage is None:
            return
        else:
            try:
                self._last_capacity_usage = {"data": usage}
            except Exception:
                self._last_capacity_usage = None

    def _summarize_capacity(self, cap_summary: Any) -> dict[str, Any]:
        if not isinstance(cap_summary, dict):
            return {"status": cap_summary}
        summary: dict[str, Any] = {"status": cap_summary.get("status")}
        for k in ("issued_keys", "revoked_keys", "active_tenants", "last_key_issue_ts"):
            if k in cap_summary:
                summary[k] = cap_summary.get(k)
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
        utilization = cap_summary.get("utilization")
        if utilization is not None:
            try:
                summary["utilization"] = float(utilization)
            except Exception:
                summary["utilization"] = utilization
        pricing = cap_summary.get("pricing")
        if isinstance(pricing, dict):
            summary["pricing_mode"] = pricing.get("mode")
            summary["pricing_suggested"] = pricing.get("suggested")
        failsafe = cap_summary.get("inventoryFailsafe")
        if isinstance(failsafe, dict):
            summary["failsafe_status"] = failsafe.get("status")
            summary["failsafe_actions"] = failsafe.get("actions")
        return summary

    def _extract_usage_daily(self, usage: Any) -> float | None:
        if not isinstance(usage, dict):
            return None
        for key in ("dailyAverageDiem", "daily_average_diem", "avgDailyDiem"):
            if key in usage:
                try:
                    return float(usage[key])
                except Exception:
                    continue
        data = usage.get("data")
        if isinstance(data, list) and data:
            totals: list[float] = []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                for candidate in (
                    "dailyAverageDiem",
                    "daily_average_diem",
                    "consumptionDaily",
                    "consumption",
                ):
                    if candidate in entry:
                        try:
                            totals.append(float(entry[candidate]))
                        except Exception:
                            continue
                        break
            if totals:
                return sum(totals) / len(totals)
        aggregate = usage.get("aggregate")
        if isinstance(aggregate, dict):
            for key in ("daily", "daily_diem"):
                value = aggregate.get(key)
                if value is not None:
                    try:
                        return float(value)
                    except Exception:
                        continue
        return None

    def _extract_limit_total(self, limits: Any) -> float | None:
        if limits is None:
            return None
        entries: list[Any]
        if isinstance(limits, list):
            entries = limits
        elif isinstance(limits, dict):
            entries = []
            for key in ("data", "items", "keys"):
                value = limits.get(key)
                if isinstance(value, list):
                    entries = value
                    break
            if not entries:
                entries = (
                    list(limits.values())
                    if all(isinstance(v, dict) for v in limits.values())
                    else []
                )
        else:
            return None
        total = 0.0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            limit = entry.get("consumptionLimit") or entry.get("consumption_limit")
            amount = None
            if isinstance(limit, dict):
                amount = limit.get("diem") or limit.get("daily")
            elif isinstance(limit, (int, float)):
                amount = limit
            if amount is not None:
                try:
                    total += float(amount)
                except Exception:
                    continue
        return total if total > 0 else None

    def _compute_treasury_plan(
        self, cap_summary: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.ai_treasurer is None:
            return None
        usage = cap_summary.get("usage")
        limits = cap_summary.get("limits")
        avg_daily = self._extract_usage_daily(usage)
        capacity_total = self._extract_limit_total(limits)
        if avg_daily is None or capacity_total is None:
            return None
        try:
            delta = float(self.ai_treasurer.rebalance(avg_daily, capacity_total))
        except Exception as exc:
            logger.debug(f"AI Treasurer error: {exc}")
            return {
                "status": "error",
                "error": str(exc),
                "avgDailyDiem": avg_daily,
                "currentCapacity": capacity_total,
            }

        # Determine action based on delta and portfolio state
        action = "hold"
        thought = f"Buffer delta: {delta:.2f} DIEM"

        if delta > 0:
            action = "accumulate_buffer"
            thought = f"Need {delta:.2f} more DIEM; accumulate buffer"
        elif delta < -10:  # Surplus threshold
            action = "recycle_profits"
            thought = (
                f"Surplus {abs(delta):.2f} DIEM; recycle USDC profits to VVV stake"
            )

        # Check if pricing adjustment needed (utilization-based)
        utilization = cap_summary.get("utilization", 0.0)
        if utilization and (utilization > 0.85 or utilization < 0.40):
            action = "adjust_pricing"
            thought = f"Utilization {utilization:.2%} triggers pricing adjustment"

        return {
            "status": "computed",
            "action": action,
            "thought": thought,
            "avgDailyDiem": avg_daily,
            "currentCapacity": capacity_total,
            "delta": delta,
        }

    def _log_cycle_payload(self, cycle_record: dict[str, Any]) -> dict[str, Any]:
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
        records: list[dict[str, Any]] = []
        if self.memory_store is not None:
            try:
                records = list(self.memory_store.recent(max(1, int(limit))))
            except Exception as exc:
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
            details = (
                guard_info.get("details")
                if isinstance(guard_info.get("details"), dict)
                else {}
            )
            diff_val = details.get("diff")
            if diff_val is None:
                price_health = (
                    arbi.get("priceHealth")
                    if isinstance(arbi.get("priceHealth"), dict)
                    else {}
                )
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
        diff: float | None,
        threshold: float | None,
        streak_prev: int,
        streak_cap: int,
        min_streak: int,
        drift_cap: float,
        vol_bps: float | None,
        util_vol_bps: float | None,
        vol_cap: float,
        recent_diffs: list[float],
    ) -> dict[str, Any] | None:
        min_required = max(1, int(min_streak))
        if streak_prev < min_required:
            return None
        if streak_cap > 0 and streak_prev >= streak_cap:
            return None

        effective_diff: float | None = None
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

        def _vol_ok(value: float | None) -> bool:
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

    def _prepare_progressive_state(self, enabled: bool) -> dict[str, Any] | None:
        self._progressive_requested = bool(enabled)
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
            self._progressive_state = state
        return state

    def _update_progressive_state(
        self,
        state: dict[str, Any],
        cycle_record: dict[str, Any],
        *,
        live_intent: bool,
    ) -> None:
        if not state or not live_intent:
            return
        stake = cycle_record.get("stake") if isinstance(cycle_record, dict) else None
        heartbeat_info = (
            (stake or {}).get("heartbeat") if isinstance(stake, dict) else None
        )
        heartbeat_sent = bool((heartbeat_info or {}).get("sent"))
        heartbeat_error = (
            (heartbeat_info or {}).get("error")
            if isinstance(heartbeat_info, dict)
            else None
        )
        forced_heartbeat = bool((heartbeat_info or {}).get("forced"))
        status_ok = (stake or {}).get("status") == "ok"
        allow_missing = _env_flag("STAKEMASTER_PROGRESSIVE_ALLOW_NO_HEARTBEAT", False)
        tolerate_error = heartbeat_error in {"venice_client_unavailable"}

        treat_as_success = False
        if status_ok:
            if heartbeat_sent or not forced_heartbeat:
                treat_as_success = True
            elif forced_heartbeat and allow_missing and tolerate_error:
                treat_as_success = True
                try:
                    logger.info(
                        "progressive heartbeat bypass",
                        extra={
                            "reason": heartbeat_error,
                            "counter": int(state.get("counter", 0)) + 1,
                        },
                    )
                except Exception:
                    pass
        prev_counter = int(state.get("counter", 0))
        counter_changed = False
        if treat_as_success:
            new_counter = prev_counter + 1
            state["counter"] = new_counter
            counter_changed = new_counter != prev_counter
            try:
                logger.info(
                    "progressive state: counter incremented",
                    extra={
                        "counter": new_counter,
                        "threshold": max(
                            1,
                            int(
                                state.get("threshold") or self._progressive_threshold()
                            ),
                        ),
                        "heartbeat_sent": heartbeat_sent,
                        "status_ok": status_ok,
                    },
                )
            except Exception:
                pass
        else:
            if prev_counter > 0:
                counter_changed = True
                try:
                    logger.info(
                        "progressive state: counter reset",
                        extra={
                            "prev_counter": prev_counter,
                            "heartbeat_sent": heartbeat_sent,
                            "heartbeat_error": heartbeat_error,
                            "status_ok": status_ok,
                            "forced_heartbeat": forced_heartbeat,
                        },
                    )
                except Exception:
                    pass
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
                        "enabled_at": state["enabled_at"],
                    },
                )
            except Exception:
                pass
        if counter_changed:
            try:
                is_live = bool(state.get("live"))
                logger.info(
                    "progressive state",
                    extra={
                        "counter": int(state.get("counter", 0)),
                        "threshold": threshold,
                        "live": is_live,
                        "enabled_at": state.get("enabled_at") if is_live else None,
                    },
                )
            except Exception:
                pass

    def _invoke_arbi(
        self,
        price: float,
        *,
        mint_rate: float,
        mint_rate_source: str | None,
        current_inventory_usd: float | None,
        utilization_ratio: float | None,
        vol_bps: float | None,
        corr_id: str | None,
        simulate: bool | None,
        recovery_only: bool | None = None,
    ) -> Any:
        import inspect as _ins

        params = _ins.signature(self.arbi.evaluate_and_maybe_mint).parameters  # type: ignore[attr-defined]
        kwargs: dict[str, Any] = {}
        positional_price: Any | None = price
        if "market_price" in params:
            kwargs["market_price"] = price
            positional_price = None
        elif "price" in params:
            kwargs["price"] = price
            positional_price = None
        # If the callable only accepts **kwargs (no positional parameters besides self),
        # send price as a keyword to avoid TypeError.
        has_var_kw = any(p.kind == _ins.Parameter.VAR_KEYWORD for p in params.values())
        required_positional = any(
            p.kind
            in (
                _ins.Parameter.POSITIONAL_ONLY,
                _ins.Parameter.POSITIONAL_OR_KEYWORD,
            )
            and p.default is _ins.Parameter.empty
            and p.name != "self"
            for p in params.values()
        )
        if has_var_kw and not required_positional and positional_price is not None:
            kwargs.setdefault("market_price", positional_price)
            positional_price = None
        if "mint_rate" in params:
            kwargs["mint_rate"] = mint_rate
        if "mint_rate_source" in params:
            kwargs["mint_rate_source"] = mint_rate_source
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
        if recovery_only is not None and "recovery_only" in params:
            kwargs["recovery_only"] = bool(recovery_only)
        if positional_price is None:
            return self.arbi.evaluate_and_maybe_mint(**kwargs)
        return self.arbi.evaluate_and_maybe_mint(positional_price, **kwargs)

    def _check_quote_consolidation(
        self, dry_run: bool, *, force: bool = False
    ) -> dict[str, Any] | None:
        """Optionally swap USDbC into the configured quote token (USDC)."""

        enabled = _env_flag("QUOTE_TOKEN_CONSOLIDATE_ENABLE", False)
        if not enabled:
            return None
        if dry_run:
            return {"action": "skipped", "reason": "dry_run"}
        if not force and not _env_flag("QUOTE_TOKEN_CONSOLIDATE_EACH_CYCLE", False):
            return None

        try:
            from services.wallet.gas_refuel import QuoteTokenConsolidator

            svc = QuoteTokenConsolidator()
            result = svc.consolidate()
            payload = result.to_dict() if hasattr(result, "to_dict") else result
            try:
                action = payload.get("action") if isinstance(payload, dict) else None
                if action == "converted":
                    logger.info(
                        "Quote consolidation converted USDbC -> USDC",
                        extra={"result": payload},
                    )
                elif action == "skipped":
                    logger.debug(
                        "Quote consolidation skipped",
                        extra={
                            "reason": payload.get("reason")
                            if isinstance(payload, dict)
                            else None
                        },
                    )
            except Exception:
                pass
            return payload
        except Exception as exc:
            logger.warning("Quote consolidation failed: %s", exc)
            return {"action": "error", "error": str(exc)}

    def _check_gas_refuel(self, dry_run: bool) -> dict[str, Any] | None:
        """Check if gas refueling is needed and attempt refuel if enabled.

        Returns refuel result dict or None if refueling is disabled/skipped.
        """
        if dry_run:
            return None
        if not _env_flag("GAS_REFUEL_ENABLE", True):
            return None
        try:
            from services.wallet.gas_refuel import GasRefuelService

            refuel_service = GasRefuelService()
            status = refuel_service.get_status()

            if not status.get("needs_refuel"):
                logger.debug(
                    "Gas refuel check: OK (balance=%.6f ETH)",
                    status.get("eth_balance_eth") or 0.0,
                )
                return {"action": "skipped", "reason": "sufficient_balance", **status}

            logger.warning(
                "Gas refuel needed: balance=%.6f ETH (min=%.6f ETH)",
                status.get("eth_balance_eth") or 0.0,
                status.get("min_eth_wei", 0) / 1e18,
            )

            result = refuel_service.check_and_refuel()

            if result.success:
                # CRITICAL: Log tx_hash for on-chain transaction traceability
                logger.info(
                    "Gas refuel %s: %s -> %.6f ETH (was %.6f ETH) tx_hash=%s unwrap_tx=%s",
                    result.action,
                    result.asset_used or "N/A",
                    (result.eth_balance_after_wei or 0) / 1e18,
                    result.eth_balance_before_wei / 1e18,
                    result.tx_hash or "none",
                    result.unwrap_tx_hash or "none",
                )
            else:
                logger.error("Gas refuel failed: %s - %s", result.reason, result.error)

            return result.to_dict()
        except Exception as exc:
            logger.warning("Gas refuel check failed: %s", exc)
            return {"action": "error", "error": str(exc)}

    def _check_low_usdc_balance(self, cycle: int) -> None:
        """Warn once per N cycles if USDC balance is critically low."""
        threshold = float(os.getenv("PORTFOLIO_USDC_LOW_BALANCE_USD", "5.0"))
        warn_interval = int(os.getenv("PORTFOLIO_USDC_WARN_INTERVAL_CYCLES", "10"))

        if not hasattr(self, "portfolio_inventory") or self.portfolio_inventory is None:
            return

        try:
            snapshot = self.portfolio_inventory.snapshot(include_eth=False)
            usdc_usd = snapshot.per_asset_usd.get("USDC", 0.0)
            wallet_addr = getattr(snapshot, "address", None) or getattr(
                self, "wallet_address", "unknown"
            )

            # Expose gauges for external monitors
            try:
                _metrics_set_gauge(
                    "wallet_usdc_balance_usd",
                    usdc_usd,
                    labels={"wallet": str(wallet_addr)},
                )
                _metrics_set_gauge(
                    "wallet_usdc_sufficient",
                    1.0 if usdc_usd >= threshold else 0.0,
                    labels={"wallet": str(wallet_addr)},
                )
            except Exception:
                pass

            if (
                usdc_usd < threshold
                and (cycle - self._low_usdc_warned_cycle) >= warn_interval
            ):
                logger.warning(
                    "USDC balance critically low: $%.2f (threshold=$%.2f). "
                    "Deposit USDC to enable buy/burn trades.",
                    usdc_usd,
                    threshold,
                )
                self._low_usdc_warned_cycle = cycle
                try:
                    _emit_event(
                        "portfolio.usdc_balance_low",
                        {
                            "balance_usd": usdc_usd,
                            "threshold_usd": threshold,
                            "cycle": cycle,
                            "action_required": "deposit_usdc",
                            "wallet": str(wallet_addr),
                        },
                    )
                except Exception:
                    pass
        except Exception:
            pass  # Non-critical check

    def run_cycle(
        self,
        *,
        dry_run: bool = True,
        enable_live: bool = False,
        mint_rate: float | None = None,
        progressive_live: bool = False,
        listen_base: float | None = None,
    ) -> dict[str, Any]:
        cycle_ts = time.time()
        try:
            self._cycle_count += 1
        except Exception:
            self._cycle_count = 1
        try:
            max_cycle_seconds = float(
                os.getenv("ORCHESTRATOR_MAX_CYCLE_SECONDS", "120")
            )
        except Exception:
            max_cycle_seconds = 120.0
        max_cycle_seconds = max(0.0, float(max_cycle_seconds))

        def _check_cycle_timeout() -> bool:
            if max_cycle_seconds <= 0:
                return False
            return (time.time() - cycle_ts) > max_cycle_seconds

        self._propagate_run_mode(dry_run)

        quote_consolidation_result: dict[str, Any] | None = None
        try:
            quote_consolidation_result = self._check_quote_consolidation(
                dry_run, force=False
            )
        except Exception as exc:
            logger.warning("Quote consolidation step failed: %s", exc)
            quote_consolidation_result = {"action": "error", "error": str(exc)}

        # --- Gas refuel check (before any trades) ---
        gas_refuel_result: dict[str, Any] | None = None
        try:
            gas_refuel_result = self._check_gas_refuel(dry_run)
        except Exception as exc:
            logger.warning("Gas refuel step failed: %s", exc)
            gas_refuel_result = {"action": "error", "error": str(exc)}

        # --- Low balance warning ---
        try:
            self._check_low_usdc_balance(self._cycle_count)
        except Exception:
            pass

        # --- StakeMaster step ---
        try:
            stake_live_allowed = _env_flag("ORCHESTRATOR_STAKE_LIVE", False)
            stake_live = bool(
                enable_live and not dry_run and (stake_live_allowed or progressive_live)
            )
            stake_hint = (
                self._pending_stake_recommendation or self._stake_recommendation
            )
            try:
                stake_result = self.stake_master.run_once(
                    live=stake_live, recommendation=stake_hint
                )
            except TypeError as exc:
                # Support simplified test doubles that do not accept recommendation kwarg.
                if "recommendation" in str(exc):
                    stake_result = self.stake_master.run_once(live=stake_live)
                else:
                    raise
            self._stake_recommendation = None
            self._pending_stake_recommendation = None
        except Exception as exc:
            stake_result = {"status": "error", "error": str(exc)}
            logger.warning(f"StakeMaster step failed: {exc}")

        # --- Market signals ---
        guard_streak_prev, guard_diffs_prev = self._price_guard_recent_stats()
        price_guard_bypass: dict[str, Any] | None = None
        utilization_ratio: float | None = None
        utilization_source: str | None = None
        vol_bps: float | None = None
        util_vol_bps: float | None = None
        try:
            effective_mint_rate = float(mint_rate) if mint_rate is not None else 1.0
        except (TypeError, ValueError):
            effective_mint_rate = 1.0
        mint_rate_source = "param"
        prices: dict[str, float] = {}
        price_health: dict[str, Any] | None = None
        skip_due_to_price = False
        price_guard_why: dict[str, Any] | None = None
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

        def _fetch_signals() -> dict[str, Any] | None:
            fn = getattr(self.market, "unified_signals", None)
            if not callable(fn):
                return None
            try:
                return fn(ttl_s=30)
            except TypeError:
                try:
                    return fn(30)
                except Exception:
                    try:
                        return fn()
                    except Exception:
                        return None
            except Exception:
                return None

        def _fetch_util_vol_bps() -> float | None:
            fn = getattr(self.market, "utilization_volatility_bps", None)
            if not callable(fn):
                return None
            try:
                return fn(window=3)
            except TypeError:
                try:
                    return fn(3)
                except Exception:
                    try:
                        return fn()
                    except Exception:
                        return None
            except Exception:
                return None

        try:
            # mint_rate=None means "use on-chain"; otherwise check if it's the old default of 1.0
            param_is_default_mint_rate = (
                mint_rate is None or abs(float(mint_rate) - 1.0) < 1e-12
            )
        except Exception:
            param_is_default_mint_rate = True

        if dry_run:
            px, real_prices = _resolve_diem_price_for_dry_run(self.market)
            prices = {
                "DIEM": px,
                "VVV": float(real_prices.get("VVV", 0.0) or 0.0),
                "USDC": float(real_prices.get("USDC", 1.0) or 1.0),
            }
            try:
                fake_rate = os.getenv("DIEM_FAKE_MINT_RATE") or os.getenv(
                    "DIEM_MINT_RATE"
                )
                if fake_rate:
                    effective_mint_rate = float(fake_rate)
                    mint_rate_source = "env_dry_run"
            except Exception:
                pass
            # Dry-run on-chain mint rate lookup when env not set and param is default 1.0
            if mint_rate_source == "param" and param_is_default_mint_rate:
                try:
                    diem = getattr(self.arbi, "diem", None)
                    query_fn = getattr(diem, "_query_mint_rate_onchain_safe", None)
                    if callable(query_fn):
                        units = query_fn()
                        if units not in (None, 0):
                            # services.diem.client returns (sVVV per DIEM) scaled by 1e18.
                            # Convert to token-per-token ratio, respecting decimals.
                            try:
                                diem_dec = int(os.getenv("DIEM_DECIMALS") or "18")
                            except Exception:
                                diem_dec = 18
                            try:
                                svvv_dec = int(
                                    os.getenv("SVVV_DECIMALS")
                                    or os.getenv("VVV_DECIMALS")
                                    or "18"
                                )
                            except Exception:
                                svvv_dec = 18
                            scale = float(10 ** max(0, int(diem_dec))) / float(
                                10 ** max(0, int(svvv_dec))
                            )
                            effective_mint_rate = (float(units) / 1e18) * float(scale)
                            mint_rate_source = "onchain"
                except Exception:
                    pass
            try:
                sig = _fetch_signals()
                if isinstance(sig, dict):
                    vvv = sig.get("vvv")
                    if isinstance(vvv, dict):
                        ur = vvv.get("utilization")
                        if ur is not None:
                            utilization_ratio = float(ur)
                            utilization_source = "venice"
            except Exception:
                pass
            try:
                util_vol_bps = _fetch_util_vol_bps()
                if vol_bps in (None, 0.0) and util_vol_bps is not None:
                    vol_bps = float(util_vol_bps)
            except Exception:
                pass
        else:
            prices = self.market.prices(["DIEM", "VVV", "USDC"]) or {}
            px = float(prices.get("DIEM", 1.0))
            if _is_invalid_price(px) and not skip_due_to_price:
                skip_due_to_price = True
                price_guard_why = {
                    "decision": "hold",
                    "reason": "price_missing",
                    "details": {"price": px},
                }
                price_health = {
                    "valid": False,
                    "reason": "missing_price",
                    "source": "missing",
                    "price": px,
                }
                try:
                    logger.warning(
                        "Skipping ArbiDiem: DIEM price missing or invalid",
                        extra={"price": px, "prices": prices},
                    )
                except Exception:
                    pass
                px = 0.0
            health_callable = getattr(self.market, "price_health", None)
            if callable(health_callable):
                try:
                    price_health = health_callable("DIEM", max_age=120.0)
                except Exception:
                    price_health = None
            # If primary price failed (default 1.0) but we have a valid bridge price, use it
            if (px == 1.0 or _is_invalid_price(px)) and isinstance(price_health, dict):
                if price_health.get("source") == "bridge_vvv" and price_health.get(
                    "valid"
                ):
                    bridge_px = price_health.get("price")
                    # Accept any valid bridge price (DIEM trades ~$100-200 per CMC Dec 2024)
                    if bridge_px and not _is_invalid_price(float(bridge_px)):
                        logger.info(
                            f"Orchestrator: using bridge_vvv price fallback: ${float(bridge_px):.4f}"
                        )
                        px = float(bridge_px)
                        prices["DIEM"] = px  # Update the prices dict too
            if isinstance(price_health, dict):
                source = str(price_health.get("source") or "")
                fallback_reason = price_health.get("fallback_reason")
                clamped = bool(price_health.get("clamped"))
                valid_flag = price_health.get("valid")
                # Accept composite path-engine routes, on-chain aggregator routes, DIEM canonical routes, and direct bridge-derived quotes.
                source_ok = (
                    source.startswith("path_engine")
                    or source.startswith("aggregator")
                    or source in {"bridge_vvv", "diem_canonical", "direct_pool"}
                )
                fallback_reason_norm = str(fallback_reason or "").strip().lower()
                if source == "external_reference":
                    allowed_ext_reasons = {
                        "no_onchain_liquidity",
                        "diem_bridge_quote",
                        "rpc_rate_limit",
                    }
                    provider_label = (
                        str(price_health.get("provider") or "").strip().lower()
                    )
                    external_in_bounds = False
                    try:
                        external_in_bounds = 10.0 <= float(px) <= 500.0
                    except Exception:
                        external_in_bounds = False
                    if fallback_reason_norm in allowed_ext_reasons:
                        source_ok = True
                        price_health["trusted_external"] = True
                        logger.info(
                            "DIEM price fallback to external reference (allowed reason)",
                            extra={
                                "fallback_reason": fallback_reason_norm,
                                "price": px,
                                "provider": provider_label,
                            },
                        )
                    elif (
                        external_in_bounds
                        and not clamped
                        and valid_flag is not False
                        and provider_label in {"external", "reference", ""}
                    ):
                        source_ok = True
                        price_health["trusted_external"] = True
                        if not fallback_reason_norm:
                            price_health["fallback_reason"] = "external_bounds_ok"
                        logger.info(
                            "DIEM price fallback to external reference (bounds check passed)",
                            extra={
                                "fallback_reason": price_health.get("fallback_reason"),
                                "price": px,
                                "provider": provider_label,
                            },
                        )
                    else:
                        logger.warning(
                            "DIEM price using external reference (not trusted)",
                            extra={
                                "fallback_reason": fallback_reason_norm,
                                "price": px,
                                "provider": provider_label,
                                "external_in_bounds": external_in_bounds,
                                "clamped": clamped,
                                "valid": valid_flag,
                            },
                        )
                if clamped or (valid_flag is False) or not source_ok:
                    skip_due_to_price = True
                    price_guard_why = {
                        "decision": "hold",
                        "reason": "price_guard",
                        "details": dict(price_health),
                    }
                    if isinstance(price_guard_why.get("details"), dict):
                        price_guard_why["details"]["streak"] = guard_streak_prev + 1
                    logger.warning(
                        "Skipping ArbiDiem due to DIEM price health: source=%s clamped=%s valid=%s",
                        source,
                        clamped,
                        valid_flag,
                        extra={"price_health": price_health},
                    )
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
                                    "util_vol_bps": price_guard_bypass.get(
                                        "util_vol_bps"
                                    ),
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
                            utilization_source = "venice"
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
            # If market mint-rate is unavailable, fall back to on-chain mint-rate from the DIEM client.
            # Only do this when the caller didn't override the mint rate (default is 1.0).
            force_env = _mint_rate_force_env()
            market_is_env = str(mint_rate_source).startswith("env")
            placeholder_env = False
            try:
                placeholder_env = (
                    market_is_env
                    and not force_env
                    and abs(float(effective_mint_rate) - 1.0) < 1e-9
                )
            except Exception:
                placeholder_env = False

            if placeholder_env or (
                mint_rate_source == "param" and param_is_default_mint_rate
            ):
                try:
                    diem = getattr(self.arbi, "diem", None)
                    query_fn = getattr(diem, "_query_mint_rate_onchain_safe", None)
                    if callable(query_fn):
                        units = query_fn()
                        if units not in (None, 0):
                            # services.diem.client returns (sVVV per DIEM) scaled by 1e18.
                            # Convert to token-per-token ratio, respecting decimals when provided.
                            try:
                                diem_dec = int(os.getenv("DIEM_DECIMALS") or "18")
                            except Exception:
                                diem_dec = 18
                            try:
                                svvv_dec = int(
                                    os.getenv("SVVV_DECIMALS")
                                    or os.getenv("VVV_DECIMALS")
                                    or "18"
                                )
                            except Exception:
                                svvv_dec = 18
                            scale = float(10 ** max(0, int(diem_dec))) / float(
                                10 ** max(0, int(svvv_dec))
                            )
                            effective_mint_rate = (float(units) / 1e18) * float(scale)
                            mint_rate_source = "onchain"
                except Exception:
                    pass
        try:
            hist = getattr(self, "_px_hist", [])
            # Price continuity guard: detect and reset on extreme jumps
            max_jump_ratio = 0.5  # 50% max deviation from last price
            if hist:
                last_px = hist[-1]
                if last_px > 0:
                    deviation = abs(float(px) / last_px - 1.0)
                    if deviation > max_jump_ratio:
                        logger.warning(
                            "Price history discontinuity detected: %.4f -> %.4f (%.1f%% jump), resetting history",
                            last_px,
                            float(px),
                            deviation * 100,
                        )
                        hist = []  # Reset history on extreme jump
            hist.append(float(px))
            if len(hist) > 16:
                del hist[: len(hist) - 16]
            self._px_hist = hist
            if hasattr(self.arbi, "risk"):
                vol_bps = float(self.arbi.risk.volatility_bps(hist))
            if (vol_bps is None or vol_bps <= 0.0) and hasattr(
                self.market, "utilization_volatility_bps"
            ):
                util_vol_bps = _fetch_util_vol_bps()
                if util_vol_bps is not None:
                    vol_bps = float(util_vol_bps)
            elif hasattr(self.market, "utilization_volatility_bps"):
                util_vol_bps = _fetch_util_vol_bps()
                if _env_flag("RISK_VOL_PERSIST", False):
                    try:
                        from datetime import datetime as _dt

                        from sqlmodel import Session

                        from db.models import PriceTick
                        from db.session import create_db_and_tables, get_engine

                        # Production-like environments require explicit SQL_CREATE_ALL_ON_START=true
                        _prod_like = bool(
                            os.getenv("SQL_DATABASE_URL")
                            or os.getenv("DATABASE_URL")
                            or os.getenv("POSTGRES_HOST")
                        )
                        _create_all_env = os.getenv("SQL_CREATE_ALL_ON_START")

                        if _prod_like:
                            # Production: only create if explicitly enabled
                            _create_all = (
                                _create_all_env is not None
                                and _create_all_env.strip().lower()
                                in {"1", "true", "yes", "on"}
                            )
                        else:
                            # Non-production: default to True unless explicitly disabled
                            _create_all = (
                                _create_all_env is None
                                or _create_all_env.strip().lower()
                                in {"1", "true", "yes", "on"}
                            )

                        if _create_all:
                            create_db_and_tables()
                        eng = get_engine()
                        with Session(eng) as s:  # type: ignore[call-arg]
                            s.add(
                                PriceTick(
                                    symbol="DIEM", price_usd=float(px), ts=_dt.utcnow()
                                )
                            )
                            s.commit()
                    except Exception:
                        pass
        except Exception:
            vol_bps = None

        # Best-effort: populate utilization signal if still missing.
        if utilization_ratio is None:
            try:
                sig = _fetch_signals()
                if isinstance(sig, dict):
                    vvv = sig.get("vvv")
                    if isinstance(vvv, dict):
                        ur = vvv.get("utilization")
                        if ur is not None:
                            utilization_ratio = float(ur)
                            utilization_source = "venice"
            except Exception:
                pass
        if utilization_ratio is None:
            try:
                utilization_ratio, utilization_source = _resolve_utilization_fallback()
                _metrics_inc(
                    "orchestrator_utilization_fallback_total",
                    labels={"source": str(utilization_source)},
                )
                logger.warning(
                    "utilization_source=%s utilization_ratio=%.4f (live utilization unavailable)",
                    utilization_source,
                    float(utilization_ratio),
                )
            except Exception:
                utilization_ratio = None
                utilization_source = None
        if util_vol_bps is None and hasattr(self.market, "utilization_volatility_bps"):
            util_vol_bps = _fetch_util_vol_bps()
        # Best-effort: propagate utilization volatility into vol_bps when primary calc is missing.
        if vol_bps is None and util_vol_bps is not None:
            try:
                vol_bps = float(util_vol_bps)
            except Exception:
                pass

        current_inventory_usd: float | None = None
        if not dry_run and _env_flag("RISK_ENABLE_PORTFOLIO_CAP", False):
            try:
                # Try portfolio inventory service first, fallback to env vars
                inventory_usd: float | None = None
                if (
                    hasattr(self, "portfolio_inventory")
                    and self.portfolio_inventory is not None
                ):
                    try:
                        portfolio_snapshot = self.portfolio_inventory.snapshot(
                            include_eth=False
                        )
                        inventory_usd = portfolio_snapshot.inventory_usd
                    except Exception:
                        pass

                # Fallback to env vars if portfolio service unavailable
                if inventory_usd is None or inventory_usd <= 0:

                    def _i(name: str) -> int:
                        v = os.getenv(name)
                        try:
                            return (
                                int(v) if v is not None and str(v).strip() != "" else 0
                            )
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
                    inventory_usd = float(total_usd)

                current_inventory_usd = inventory_usd
            except Exception:
                current_inventory_usd = None

        correlation_id = str(uuid.uuid4())
        reflex_info: dict[str, Any] | None = None
        reflex_blocked = False
        # Sticky halt from prior high-severity reflection
        reflection_halt_until = getattr(self, "_reflection_halt_until", 0.0)
        now_ts = time.time()
        if reflection_halt_until and now_ts < reflection_halt_until:
            remaining = max(0.0, reflection_halt_until - now_ts)
            reflex_blocked = True
            reflex_info = {
                "halt": True,
                "reasons": ["reflection_high_severity"],
                "warnings": [],
                "observed": {"remaining_seconds": remaining},
                "limits": {},
            }

        # Pre-reflex recovery opportunity check
        recovery_bypass = False
        if hasattr(self.arbi, "_locked_svvv_ratio_cap") and hasattr(
            self.arbi, "_svvv_lock_state"
        ):
            try:
                cap = float(self.arbi._locked_svvv_ratio_cap())
                if cap < 1.0:
                    # Construct wallet balances from stake_result and portfolio
                    wallet_balances: dict[str, Any] = {}
                    stake_snapshot = (
                        stake_result.get("snapshot")
                        if isinstance(stake_result, dict)
                        else None
                    )
                    if stake_snapshot and isinstance(stake_snapshot, dict):
                        staked_units = stake_snapshot.get("staked", 0)
                        wallet_balances["SVVV"] = {
                            "units": int(staked_units),
                            "decimals": 18,
                        }
                    # Try to get DIEM balance from portfolio
                    if (
                        hasattr(self, "portfolio_inventory")
                        and self.portfolio_inventory is not None
                    ):
                        try:
                            portfolio_snapshot = self.portfolio_inventory.snapshot(
                                include_eth=False
                            )
                            per_asset = getattr(portfolio_snapshot, "per_asset_usd", {})
                            # Get DIEM units if available
                            diem_info = per_asset.get("DIEM", {})
                            if isinstance(diem_info, dict) and "units" in diem_info:
                                wallet_balances["DIEM"] = {
                                    "units": int(diem_info.get("units", 0)),
                                    "decimals": 18,
                                }
                        except Exception:
                            pass

                    if wallet_balances.get("SVVV"):
                        lock_state = self.arbi._svvv_lock_state(wallet_balances)
                        locked_ratio = lock_state.get("locked_ratio")
                        min_units = int(self.arbi._locked_svvv_ratio_min_total_units())
                        total_units = int(lock_state.get("total_units") or 0)
                        if (
                            locked_ratio is not None
                            and float(locked_ratio) > cap
                            and total_units >= min_units
                        ):
                            logger.info(
                                "Locked ratio %.2f%% exceeds cap %.2f%%, recovery eligible",
                                float(locked_ratio) * 100,
                                cap * 100,
                            )
                            recovery_bypass = _env_flag(
                                "ARBI_DIEM_RECOVERY_BYPASS_REFLEX", True
                            )
            except Exception as exc:
                logger.debug(f"Pre-reflex recovery check failed: {exc}")

        if self.reflex_guard is not None and not reflex_blocked:
            last_cycle = None
            if self.memory_store is not None:
                try:
                    hist = self.memory_store.recent(1)
                    last_cycle = hist[0] if hist else None
                except Exception as exc:
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
            except Exception as exc:
                logger.warning(f"Reflex guardian error: {exc}")
                reflex_info = {"halt": False, "error": str(exc)}

        paused = _env_flag("AGENTS_PAUSED", False)
        skip_agents = paused or (reflex_blocked and not recovery_bypass)
        live_mode = bool(enable_live and not dry_run)

        if _check_cycle_timeout():
            elapsed = time.time() - cycle_ts
            logger.warning(
                "Cycle timeout before ArbiDiem step, skipping",
                extra={
                    "elapsed_seconds": elapsed,
                    "max_cycle_seconds": max_cycle_seconds,
                },
            )
            timeout_info = {
                "status": "cycle_timeout",
                "elapsedSeconds": elapsed,
                "maxCycleSeconds": max_cycle_seconds,
            }
            arbi_record = {
                "agent": "arbi_diem",
                "action": "hold",
                "price": px,
                "inventoryUsd": current_inventory_usd,
                "dry_run": dry_run,
                "correlationId": None if skip_agents else correlation_id,
                "ts": cycle_ts,
                "mintRate": effective_mint_rate,
                "mintRateSource": mint_rate_source,
                "signals": {
                    "utilization_ratio": utilization_ratio,
                    "utilization_source": utilization_source,
                    "vol_bps": vol_bps,
                    "utilization_vol_bps": util_vol_bps,
                },
                "outcome": False,
                "why": {"decision": "hold", "reason": "cycle_timeout"},
                "execution": {"status": "cycle_timeout", "executed": False},
                "signalDecision": False,
                "quorum": {"status": "skipped", "reason": "cycle_timeout"},
                "cycleTimeout": timeout_info,
            }
            if price_health is not None:
                arbi_record["priceHealth"] = price_health
            cycle_record = {
                "ts": cycle_ts,
                "stake": stake_result,
                "arbi": arbi_record,
                "capacity": {"status": "skipped", "reason": "cycle_timeout"},
                "cycleTimeout": timeout_info,
            }
            if gas_refuel_result is not None:
                cycle_record["gas_refuel"] = gas_refuel_result
            if quote_consolidation_result is not None:
                cycle_record["quoteConsolidation"] = quote_consolidation_result
            if reflex_info is not None:
                cycle_record["reflex"] = reflex_info
            if self.memory_store is not None:
                try:
                    self.memory_store.record_cycle(cycle_record)
                except Exception as exc:
                    logger.debug(f"Memory store write failed: {exc}")
            log_payload = self._log_cycle_payload(cycle_record)
            logger.info(f"single-loop cycle: {log_payload}")
            return cycle_record

        signal_decision = False
        executed_decision = False
        quorum_info: dict[str, Any] | None = None
        execution_summary: dict[str, Any] = {"status": "skipped", "executed": False}
        price_guard_triggered = False
        hold_quorum_checked = False

        # Initialize quorum_info based on quorum availability
        if self.quorum is None:
            quorum_info = {"status": "disabled", "reason": "quorum_not_configured"}
        elif not _env_flag("QUORUM_ENABLE", True):
            quorum_info = {"status": "disabled", "reason": "QUORUM_ENABLE=false"}

        if skip_agents:
            if paused:
                execution_summary = {"status": "paused", "executed": False}
                if quorum_info is None:
                    quorum_info = {"status": "skipped", "reason": "agents_paused"}
            elif reflex_blocked:
                execution_summary = {"status": "reflex_halt", "executed": False}
                if quorum_info is None:
                    quorum_info = {"status": "skipped", "reason": "reflex_guard"}
        elif skip_due_to_price:
            guard_rationale = getattr(self.arbi, "_last_rationale", None)
            guard_payload = (
                price_guard_why
                if isinstance(price_guard_why, dict)
                else {"status": "price_guard"}
            )
            self._update_quorum_context(
                price=px,
                mint_rate=effective_mint_rate,
                utilization_ratio=utilization_ratio,
                vol_bps=vol_bps,
                stake_result=stake_result,
                rationale=guard_rationale,
                reflex=reflex_info,
                price_guard=guard_payload,
                inventory_usd=current_inventory_usd,
                dry_run=dry_run,
                live_mode=live_mode,
                simulate_decision=signal_decision,
            )
            execution_summary = {"status": "price_guard", "executed": False}
            signal_decision = False
            executed_decision = False
            price_guard_triggered = True
            if quorum_info is None:
                quorum_info = {"status": "skipped", "reason": "price_guard"}
        else:
            try:
                sim_result = self._invoke_arbi(
                    px,
                    mint_rate=effective_mint_rate,
                    mint_rate_source=mint_rate_source,
                    current_inventory_usd=(None if dry_run else current_inventory_usd),
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                    corr_id=correlation_id,
                    simulate=True,
                )
            except Exception:
                sim_result = px > 0
            trade_signal = bool(sim_result)
            arbi_rationale = getattr(self.arbi, "_last_rationale", None)

            pending_recovery_action = None
            recovery_signal = False
            try:
                pending_recovery_action = getattr(
                    self.arbi, "_pending_recovery_action", None
                )
                recovery_signal = bool(
                    isinstance(pending_recovery_action, dict)
                    and pending_recovery_action
                )
            except Exception:
                pending_recovery_action = None
                recovery_signal = False

            signal_decision = bool(trade_signal or recovery_signal)
            # Optional quorum validation for hold decisions
            validate_holds = _env_flag("QUORUM_VALIDATE_HOLDS", False)
            hold_decision = (
                validate_holds
                and not signal_decision
                and isinstance(arbi_rationale, dict)
                and str(arbi_rationale.get("decision", "")).lower() == "hold"
            )

            if recovery_signal and live_mode and (not trade_signal or reflex_blocked):
                self._update_quorum_context(
                    price=px,
                    mint_rate=effective_mint_rate,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                    stake_result=stake_result,
                    rationale=arbi_rationale,
                    reflex=reflex_info,
                    price_guard=None,
                    inventory_usd=current_inventory_usd,
                    dry_run=dry_run,
                    live_mode=live_mode,
                    simulate_decision=True,
                )
                quorum_allowed = True
                if self.quorum is not None:
                    try:
                        if hasattr(self.quorum, "decide_with_details"):
                            quorum_allowed, quorum_meta = (
                                self.quorum.decide_with_details()
                            )
                            quorum_info = dict(quorum_meta or {})
                            quorum_info["status"] = (
                                "approved" if quorum_allowed else "blocked"
                            )
                        else:
                            quorum_allowed = bool(self.quorum.decide())
                            quorum_meta = getattr(self.quorum, "last_info", None) or {}
                            quorum_info = dict(quorum_meta or {})
                            quorum_info["status"] = (
                                "approved" if quorum_allowed else "blocked"
                            )
                            if "breakdown" not in quorum_info and hasattr(
                                self.quorum, "models_snapshot"
                            ):
                                try:
                                    snapshot = self.quorum.models_snapshot()
                                    if snapshot:
                                        quorum_info["models_snapshot"] = snapshot
                                except Exception:
                                    pass
                    except Exception as exc:
                        quorum_allowed = False
                        quorum_info = {"status": "error", "error": str(exc)}
                if quorum_allowed:
                    try:
                        exec_fn = getattr(self.arbi, "_execute_pending_recovery", None)
                        live_result = False
                        try:
                            if callable(exec_fn):
                                live_result = bool(
                                    exec_fn(corr_id=correlation_id, simulate=False)
                                )
                        finally:
                            try:
                                self.arbi._pending_recovery_action = None  # type: ignore[attr-defined]
                            except Exception:
                                pass
                        executed_decision = bool(live_result)

                        updated_rationale = getattr(self.arbi, "_last_rationale", None)
                        execution_details = (
                            updated_rationale.get("execution")
                            if isinstance(updated_rationale, dict)
                            else None
                        )
                        exec_status = None
                        if isinstance(execution_details, dict):
                            exec_status = execution_details.get("status")
                        execution_summary = {
                            "status": (
                                str(exec_status)
                                if exec_status
                                else ("executed" if executed_decision else "no_action")
                            ),
                            "executed": bool(executed_decision),
                        }
                        if execution_details is not None:
                            execution_summary["execution_details"] = execution_details
                    except Exception as exc:
                        execution_summary = {
                            "status": "error",
                            "error": str(exc),
                            "executed": False,
                            "correlation_id": correlation_id,
                        }
                        executed_decision = False
                else:
                    execution_summary = {
                        "status": quorum_info.get("status", "blocked"),
                        "executed": False,
                    }
                    executed_decision = False
            elif signal_decision and live_mode and not reflex_blocked:
                self._update_quorum_context(
                    price=px,
                    mint_rate=effective_mint_rate,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                    stake_result=stake_result,
                    rationale=arbi_rationale,
                    reflex=reflex_info,
                    price_guard=None,
                    inventory_usd=current_inventory_usd,
                    dry_run=dry_run,
                    live_mode=live_mode,
                    simulate_decision=signal_decision,
                )
                quorum_allowed = True
                if self.quorum is not None:
                    try:
                        if hasattr(self.quorum, "decide_with_details"):
                            quorum_allowed, quorum_meta = (
                                self.quorum.decide_with_details()
                            )
                            quorum_info = dict(quorum_meta or {})
                            quorum_info["status"] = (
                                "approved" if quorum_allowed else "blocked"
                            )
                        else:
                            quorum_allowed = bool(self.quorum.decide())
                            quorum_meta = getattr(self.quorum, "last_info", None) or {}
                            quorum_info = dict(quorum_meta or {})
                            quorum_info["status"] = (
                                "approved" if quorum_allowed else "blocked"
                            )
                            # If last_info doesn't have breakdown, try to get it from coordinator
                            if "breakdown" not in quorum_info and hasattr(
                                self.quorum, "models_snapshot"
                            ):
                                try:
                                    snapshot = self.quorum.models_snapshot()
                                    if snapshot:
                                        quorum_info["models_snapshot"] = snapshot
                                except Exception:
                                    pass
                    except Exception as exc:
                        quorum_allowed = False
                        quorum_info = {"status": "error", "error": str(exc)}
                if quorum_allowed:
                    try:
                        live_result = self._invoke_arbi(
                            px,
                            mint_rate=effective_mint_rate,
                            mint_rate_source=mint_rate_source,
                            current_inventory_usd=current_inventory_usd,
                            utilization_ratio=utilization_ratio,
                            vol_bps=vol_bps,
                            corr_id=correlation_id,
                            simulate=False,
                        )
                        executed_decision = bool(live_result)

                        # Extract execution details from ArbiDiem rationale
                        # ArbiDiem stores ExecutionResult.as_dict() in rationale["execution"]
                        execution_details = None
                        updated_rationale = getattr(self.arbi, "_last_rationale", None)
                        if isinstance(updated_rationale, dict):
                            execution_details = updated_rationale.get("execution")

                        execution_summary = None
                        if execution_details and isinstance(execution_details, dict):
                            # execution_details is from buy_and_burn_diem or mint_and_sell_diem
                            # It contains {"buy": {...}, "burn": {...}} or {"mint": {...}, "sell": {...}}
                            buy_result = execution_details.get("buy", {})
                            sell_result = execution_details.get("sell", {})
                            mint_result = execution_details.get("mint", {})
                            burn_result = execution_details.get("burn", {})

                            # Determine primary execution status from trade result
                            error_result = None
                            for res in (
                                buy_result,
                                sell_result,
                                mint_result,
                                burn_result,
                            ):
                                if (
                                    isinstance(res, dict)
                                    and res.get("status") == "error"
                                ):
                                    error_result = res
                                    break

                            if error_result is not None:
                                exec_status = "error"
                                exec_tx_hash = error_result.get("tx_hash")
                                exec_error = error_result.get("error")
                                exec_diagnostics = error_result.get("diagnostics", {})
                                execution_summary = {
                                    "status": exec_status,
                                    "executed": False,
                                    "tx_hash": exec_tx_hash,
                                    "error": exec_error,
                                    "diagnostics": exec_diagnostics,
                                    "buy": buy_result if buy_result else None,
                                    "sell": sell_result if sell_result else None,
                                    "mint": mint_result if mint_result else None,
                                    "burn": burn_result if burn_result else None,
                                }
                            else:
                                primary_result = buy_result or sell_result
                                if primary_result and isinstance(primary_result, dict):
                                    exec_status = primary_result.get(
                                        "status", "unknown"
                                    )
                                    exec_tx_hash = primary_result.get("tx_hash")
                                    exec_error = primary_result.get("error")
                                    exec_diagnostics = primary_result.get(
                                        "diagnostics", {}
                                    )
                                    execution_summary = {
                                        "status": exec_status,
                                        "executed": exec_status
                                        in ("submitted", "confirmed"),
                                        "tx_hash": exec_tx_hash,
                                        "error": exec_error,
                                        "diagnostics": exec_diagnostics,
                                        "buy": buy_result if buy_result else None,
                                        "sell": sell_result if sell_result else None,
                                        "mint": mint_result if mint_result else None,
                                        "burn": burn_result if burn_result else None,
                                    }
                                else:
                                    # Fallback if execution_details structure is unexpected
                                    execution_summary = {
                                        "status": (
                                            "executed"
                                            if executed_decision
                                            else "no_action"
                                        ),
                                        "executed": executed_decision,
                                        "execution_details": execution_details,
                                    }
                        if execution_summary is None:
                            # No execution details available, use simple status
                            execution_summary = {
                                "status": (
                                    "executed" if executed_decision else "no_action"
                                ),
                                "executed": executed_decision,
                            }
                    except Exception as exc:
                        # Enhanced error logging with context
                        error_msg = str(exc)
                        error_type = type(exc).__name__

                        # Extract relevant context from rationale if available
                        rationale_context = {}
                        if isinstance(arbi_rationale, dict):
                            rationale_context = {
                                "action": arbi_rationale.get("decision"),
                                "trade_route": arbi_rationale.get("tradeRoute")
                                or arbi_rationale.get("trade_route"),
                                "venue": arbi_rationale.get("venue"),
                                "slippage_bps": arbi_rationale.get("slippage_bps"),
                                "units": arbi_rationale.get("units"),
                            }

                        logger.error(
                            f"ArbiDiem execution error: {error_type}: {error_msg} "
                            f"(action={rationale_context.get('action')}, "
                            f"route={rationale_context.get('trade_route')}, "
                            f"venue={rationale_context.get('venue')}, "
                            f"units={rationale_context.get('units')}, "
                            f"correlation_id={correlation_id})",
                            exc_info=True,
                            extra={
                                "agent": "arbi_diem",
                                "action": rationale_context.get("action"),
                                "error": error_msg,
                                "error_type": error_type,
                                "correlation_id": correlation_id,
                                "trade_route": rationale_context.get("trade_route"),
                                "venue": rationale_context.get("venue"),
                                "slippage_bps": rationale_context.get("slippage_bps"),
                                "units": rationale_context.get("units"),
                            },
                        )

                        execution_summary = {
                            "status": "error",
                            "error": error_msg,
                            "error_type": error_type,
                            "executed": False,
                            "correlation_id": correlation_id,
                            **rationale_context,
                        }
                        executed_decision = False
                        signal_decision = False
                else:
                    execution_summary = {
                        "status": quorum_info.get("status", "blocked"),
                        "executed": False,
                    }
                    executed_decision = False
            elif signal_decision:
                if reflex_blocked:
                    execution_summary = {"status": "reflex_halt", "executed": False}
                    signal_decision = False
                    executed_decision = False
                    if quorum_info is None:
                        quorum_info = {"status": "skipped", "reason": "reflex_guard"}
                else:
                    self._update_quorum_context(
                        price=px,
                        mint_rate=effective_mint_rate,
                        utilization_ratio=utilization_ratio,
                        vol_bps=vol_bps,
                        stake_result=stake_result,
                        rationale=arbi_rationale,
                        reflex=reflex_info,
                        price_guard=None,
                        inventory_usd=current_inventory_usd,
                        dry_run=dry_run,
                        live_mode=live_mode,
                        simulate_decision=signal_decision,
                    )
                    execution_summary = {"status": "dry_run", "executed": False}
                    if quorum_info is None:
                        quorum_info = {
                            "status": "not_invoked",
                            "reason": "dry_run_mode",
                        }
            elif hold_decision:
                hold_quorum_checked = True
                self._update_quorum_context(
                    price=px,
                    mint_rate=effective_mint_rate,
                    utilization_ratio=utilization_ratio,
                    vol_bps=vol_bps,
                    stake_result=stake_result,
                    rationale=arbi_rationale,
                    reflex=reflex_info,
                    price_guard=None,
                    inventory_usd=current_inventory_usd,
                    dry_run=dry_run,
                    live_mode=live_mode,
                    simulate_decision=False,
                )
                if self.quorum is not None:
                    try:
                        if hasattr(self.quorum, "decide_with_details"):
                            quorum_allowed, quorum_meta = (
                                self.quorum.decide_with_details()
                            )
                            quorum_info = dict(quorum_meta or {})
                            quorum_info["status"] = (
                                "approved" if quorum_allowed else "blocked"
                            )
                        else:
                            quorum_allowed = bool(self.quorum.decide())
                            quorum_meta = getattr(self.quorum, "last_info", None) or {}
                            quorum_info = dict(quorum_meta or {})
                            quorum_info["status"] = (
                                "approved" if quorum_allowed else "blocked"
                            )
                    except Exception as exc:
                        quorum_info = {"status": "error", "error": str(exc)}
                elif quorum_info is None:
                    quorum_info = {
                        "status": "not_invoked",
                        "reason": "quorum_not_configured",
                    }
                execution_summary = {
                    "status": "hold",
                    "executed": False,
                    "quorum_checked": True,
                }
            # signal_decision is False - no trade signal from ArbiDiem
            elif quorum_info is None:
                quorum_info = {
                    "status": "not_invoked",
                    "reason": "no_trade_signal",
                }

        if reflex_blocked:
            last_why = {
                "decision": "hold",
                "reason": "reflex_guard",
                "details": reflex_info,
            }
        elif (price_guard_triggered and price_guard_why is not None) or (
            price_guard_bypass is not None and price_guard_why is not None
        ):
            last_why = price_guard_why
        else:
            last_why = getattr(self.arbi, "_last_rationale", None)
        if last_why is None:
            last_why = {"decision": "hold", "reason": "no_rationale_provided"}
        stake_rec_next = self._extract_stake_recommendation(last_why)
        if stake_rec_next is not None:
            self._stake_recommendation = stake_rec_next
            self._pending_stake_recommendation = stake_rec_next
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

        # Extract slippage source from rationale if available
        slippage_source = None
        if isinstance(last_why, dict):
            slippage_source = last_why.get("slippage_source")
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
                "utilization_source": utilization_source,
                "vol_bps": vol_bps,
                "utilization_vol_bps": util_vol_bps,
            },
            "outcome": bool(
                signal_decision if (dry_run or not live_mode) else executed_decision
            ),
            "why": last_why,
            "execution": execution_summary,
            "signalDecision": bool(signal_decision),
        }
        if stake_rec_next is not None:
            arbi_record["stakeRecommendation"] = stake_rec_next
        if slippage_source:
            arbi_record["slippage_source"] = slippage_source
        if price_health is not None:
            arbi_record["priceHealth"] = price_health
        if price_guard_triggered:
            guard_info = {"status": "skipped", "reason": "price_guard"}
            if isinstance(price_guard_why, dict) and isinstance(
                price_guard_why.get("details"), dict
            ):
                guard_info["details"] = price_guard_why["details"]
            arbi_record["priceGuard"] = guard_info
        elif price_guard_bypass is not None:
            guard_info = {
                "status": "bypassed",
                "reason": "price_guard",
                "details": price_guard_bypass,
            }
            arbi_record["priceGuard"] = guard_info
            arbi_record["priceGuardBypass"] = price_guard_bypass
        # Always include quorum status, defaulting to unknown if somehow still None
        if quorum_info is None:
            quorum_info = {"status": "unknown", "reason": "unexpected_state"}
        if hold_quorum_checked:
            quorum_info.setdefault("notes", []).append("hold_decision_validated")
        arbi_record["quorum"] = quorum_info
        if reflex_info is not None:
            arbi_record["reflex"] = reflex_info

        # Optional DIEM premium diagnostics: emit canonical premiums (px/fair, px/mint-cost)
        # with trust metadata so operator surfaces can distinguish market moves from fallbacks.
        try:
            flag = (os.getenv("DIEM_PREMIUM_DIAGNOSTICS_ENABLE") or "").strip().lower()
            enable_premium_diag = flag in {"1", "true", "yes", "on"}
        except Exception:
            enable_premium_diag = False
        if enable_premium_diag:
            try:
                from libs.pricing.diem_metrics import build_diem_premium_snapshot

                vvv_price = None
                try:
                    if isinstance(prices, dict):
                        vvv_price = (
                            float(prices.get("VVV"))
                            if prices.get("VVV") is not None
                            else None
                        )
                except Exception:
                    vvv_price = None
                fair_value = None
                fv_components = None
                mint_rate_used = None
                try:
                    if isinstance(last_why, dict):
                        fair_value = last_why.get("fair_value")
                        fv_components = last_why.get("fair_value_components")
                        mint_rate_used = last_why.get("mint_rate")
                except Exception:
                    fair_value = None
                    fv_components = None
                    mint_rate_used = None
                if mint_rate_used in (None, 0):
                    mint_rate_used = effective_mint_rate
                snapshot = build_diem_premium_snapshot(
                    price_usd=px,
                    vvv_price_usd=vvv_price,
                    mint_rate=mint_rate_used,
                    fair_value_usd=fair_value,
                    fair_value_components=fv_components
                    if isinstance(fv_components, dict)
                    else None,
                    price_health=price_health
                    if isinstance(price_health, dict)
                    else None,
                    computed_at_ts=cycle_ts,
                )
                arbi_record["diemPremium"] = snapshot
            except Exception:
                # Best-effort only; diagnostics must never break the cycle.
                pass

        try:
            _metrics_inc(
                "agent_decisions_total",
                labels={"agent": "arbi_diem", "action": str(arbi_record["action"])},
            )
        except Exception:
            pass
        try:
            annotate_span(
                {"single_loop": arbi_record}, name="vvv.orchestrator.single_loop"
            )
        except Exception:
            pass

        try:
            cap_summary = self.capacity_broker.run_once(parent_key=self.parent_key)
        except TypeError:
            # Some test doubles accept positional parent_key only
            try:
                cap_summary = self.capacity_broker.run_once(self.parent_key)
            except Exception as exc:
                cap_summary = {"status": "error", "error": str(exc)}
        except Exception as exc:
            cap_summary = {"status": "error", "error": str(exc)}
        self._capture_capacity_usage(cap_summary)

        treasury_plan = None
        if isinstance(cap_summary, dict):
            treasury_plan = self._compute_treasury_plan(cap_summary)

        # Execute treasurer actions if automation enabled
        treasury_execution = None
        if treasury_plan and self.ai_treasurer and self.ai_treasurer.enable_automation:
            action = treasury_plan.get("action", "hold")
            if action != "hold":
                # Determine quorum approval
                quorum_approved = False
                if self.quorum and hasattr(self.quorum, "vote"):
                    # Use quorum vote for treasurer actions
                    quorum_approved = True  # Simplified; enhance with actual vote

                # Get reflex status
                reflex_ok = not reflex_info.get("halt", False) if reflex_info else True

                # Build portfolio snapshot for treasurer
                portfolio_snapshot = None
                if hasattr(self, "portfolio_inventory") and self.portfolio_inventory:
                    try:
                        snap = self.portfolio_inventory.snapshot(include_eth=False)
                        portfolio_snapshot = {
                            "inventoryUsd": snap.inventory_usd,
                            "perAssetUsd": snap.per_asset_usd,
                            "address": snap.address,
                        }
                    except Exception:
                        pass

                # Get broker utilization early for treasurer
                broker_utilization_for_treasurer = None
                if isinstance(cap_summary, dict):
                    broker_utilization_for_treasurer = cap_summary.get("utilization")

                # Execute treasurer action
                try:
                    treasury_execution = self.ai_treasurer.execute(
                        thought=treasury_plan.get("thought", "Automated action"),
                        action=action,
                        portfolio_snapshot=portfolio_snapshot,
                        broker_utilization=broker_utilization_for_treasurer,
                        quorum_approved=quorum_approved,
                        reflex_ok=reflex_ok,
                        dry_run=dry_run,
                        aggregator=(
                            self.market.aggregator()
                            if hasattr(self.market, "aggregator")
                            else None
                        ),
                        stake_master=self.stake_master,
                        capacity_broker=self.capacity_broker,
                    )
                except Exception as exc:
                    logger.exception("Treasurer execution failed")
                    treasury_execution = {"status": "error", "error": str(exc)}

        cycle_record = {
            "ts": cycle_ts,
            "stake": stake_result,
            "arbi": arbi_record,
            "capacity": cap_summary,
        }

        # If premium diagnostics are enabled, compute attribution vs previous cycle snapshot.
        # This makes premium moves explainable (price move vs fair-value inputs vs source/trust flip).
        try:
            flag = (os.getenv("DIEM_PREMIUM_DIAGNOSTICS_ENABLE") or "").strip().lower()
            enable_premium_diag = flag in {"1", "true", "yes", "on"}
        except Exception:
            enable_premium_diag = False
        if enable_premium_diag and isinstance(arbi_record, dict):
            try:
                current_snapshot = arbi_record.get("diemPremium")
                if isinstance(current_snapshot, dict) and self.memory_store is not None:
                    from libs.pricing.diem_metrics import (
                        compute_diem_premium_attribution,
                    )

                    prev_snapshot = None
                    try:
                        history = self.memory_store.recent(25)
                    except Exception:
                        history = []
                    if isinstance(history, list):
                        # Walk newest -> oldest, take the most recent cycle with diemPremium snapshot.
                        for entry in reversed(history):
                            cycle = None
                            if isinstance(entry, dict):
                                cycle = entry.get("cycle")
                            if not isinstance(cycle, dict):
                                continue
                            arbi_prev = cycle.get("arbi")
                            if not isinstance(arbi_prev, dict):
                                continue
                            cand = arbi_prev.get("diemPremium")
                            if isinstance(cand, dict):
                                prev_snapshot = cand
                                break
                    attribution = compute_diem_premium_attribution(
                        current=current_snapshot, previous=prev_snapshot
                    )
                    arbi_record["diemPremiumAttribution"] = attribution
            except Exception:
                # Best-effort only; attribution must never break the cycle.
                pass
        if stake_rec_next is not None:
            cycle_record["stakeRecommendation"] = stake_rec_next
        elif self._pending_stake_recommendation is not None:
            cycle_record["stakeRecommendation"] = self._pending_stake_recommendation
        if gas_refuel_result is not None:
            cycle_record["gas_refuel"] = gas_refuel_result
        if treasury_plan is not None:
            cycle_record["treasury"] = treasury_plan
        if treasury_execution is not None:
            cycle_record["treasury_execution"] = treasury_execution
        if quote_consolidation_result is not None:
            cycle_record["quoteConsolidation"] = quote_consolidation_result

        # Add portfolio inventory and broker utilization to cycle record
        try:
            if (
                hasattr(self, "portfolio_inventory")
                and self.portfolio_inventory is not None
            ):
                portfolio_snapshot = self.portfolio_inventory.snapshot(
                    include_eth=False
                )
                cycle_record["portfolio"] = {
                    "inventoryUsd": portfolio_snapshot.inventory_usd,
                    "perAssetUsd": portfolio_snapshot.per_asset_usd,
                    "address": portfolio_snapshot.address,
                    "errors": portfolio_snapshot.errors,
                }
        except Exception:
            pass

        # Extract broker utilization from capacity summary
        broker_utilization = None
        if isinstance(cap_summary, dict):
            broker_utilization = cap_summary.get("utilization")
        cycle_record["brokerUtilization"] = broker_utilization

        cycle_record["agents"] = {
            "stake_master": stake_result,
            "arbi_diem": arbi_record,
            "capacity_broker": cap_summary,
        }
        if treasury_plan is not None:
            cycle_record["agents"]["ai_treasurer"] = treasury_plan
        cycle_record["reflex"] = reflex_info
        prog_state = getattr(self, "_progressive_state", None)
        if isinstance(prog_state, dict):
            prog_snapshot = dict(prog_state)
            # Log progressive state snapshot each cycle for observability
            try:
                logger.debug(
                    "progressive state snapshot",
                    extra={
                        "counter": prog_snapshot.get("counter", 0),
                        "live": prog_snapshot.get("live", False),
                        "threshold": prog_snapshot.get("threshold", 5),
                        "enabled": prog_snapshot.get("enabled", False),
                        "last_heartbeat_error": prog_snapshot.get(
                            "last_heartbeat_error"
                        ),
                    },
                )
            except Exception:
                pass
        else:
            prog_snapshot = None
        cycle_record["progressive"] = {
            "requested": bool(getattr(self, "_progressive_requested", False)),
            "override": bool(progressive_live),
            "live": bool(enable_live and not dry_run),
            "state": prog_snapshot,
        }

        if price_guard_triggered:
            self._price_guard_runtime_streak = guard_streak_prev + 1
        else:
            self._price_guard_runtime_streak = 0

        if listen_base is not None:
            cycle_record["listenInterval"] = self._compute_listen_interval(
                float(listen_base), cycle_record
            )

        if self.reflection is not None:
            try:
                history_limit = getattr(self.reflection, "lookback", 10)
                history = None
                if self.memory_store is not None:
                    history = self.memory_store.recent(int(history_limit))
            except Exception as exc:
                logger.debug(f"Reflection history fetch failed: {exc}")
                history = None
            try:
                reflection = self.reflection.reflect(cycle_record, history=history)
                if reflection:
                    cycle_record["reflection"] = reflection
                    self._maybe_arm_reflection_halt(reflection)
            except Exception as exc:
                logger.debug(f"Reflection engine error: {exc}")

        if self.memory_store is not None:
            try:
                self.memory_store.record_cycle(cycle_record)
            except Exception as exc:
                logger.debug(f"Memory store write failed: {exc}")

        log_payload = self._log_cycle_payload(cycle_record)
        logger.info(f"single-loop cycle: {log_payload}")
        return cycle_record

    def _maybe_arm_reflection_halt(self, reflection: dict[str, Any]) -> None:
        """Apply a short-lived halt when reflection severity is high."""
        if not isinstance(reflection, dict):
            return
        if not _env_flag("REFLECTION_HALT_ENABLE", True):
            return
        labels_raw = reflection.get("labels")
        labels = set()
        if isinstance(labels_raw, list):
            try:
                labels = {str(lbl).lower() for lbl in labels_raw}
            except Exception:
                labels = set()
        reasons_raw = reflection.get("severity_reasons")
        reasons = []
        if isinstance(reasons_raw, list):
            try:
                reasons = [str(reason).lower() for reason in reasons_raw]
            except Exception:
                reasons = []
        severity = str(reflection.get("severity", "")).lower()
        if severity == "high" and "burn_gas_error" in labels:
            # Avoid sticky halts when the only high-severity trigger is a recoverable
            # gas/fee issue during DIEM burn execution.
            if not reasons or all(
                r.startswith("arbi_execution_error") for r in reasons
            ):
                return
        # Skip halt for purchased DIEM burn errors - this is expected behavior when
        # the wallet holds DEX-purchased DIEM without locked sVVV collateral.
        if "purchased_diem_no_svvv" in labels:
            return
        # Skip halt for post-buy balance sync errors - these are recoverable
        if "balance_sync_delay" in labels or "post_buy_balance_sync" in reasons:
            return
        if severity != "high":
            return
        try:
            ttl = float(os.getenv("REFLECTION_HALT_TTL_SECONDS") or 900.0)
        except Exception:
            ttl = 900.0
        ttl = max(0.0, ttl)
        self._reflection_halt_until = time.time() + ttl
        self._reflection_halt_notes = reflection.get("notes")

    def run_loop(
        self,
        *,
        interval_s: float = 15.0,
        max_cycles: int = 0,
        dry_run: bool = True,
        enable_live: bool = False,
        mint_rate: float | None = None,
        progressive_live: bool = False,
    ) -> None:
        cycle = 0
        base_dry = bool(dry_run)
        base_live = bool(enable_live)
        progressive_state = self._prepare_progressive_state(
            progressive_live or enable_live
        )
        live_intent = bool(
            base_live or (progressive_state is not None and progressive_live)
        )
        base_interval = float(interval_s)
        next_interval = base_interval

        if _env_flag("QUOTE_TOKEN_CONSOLIDATE_ENABLE", False) and _env_flag(
            "QUOTE_TOKEN_CONSOLIDATE_ON_STARTUP", True
        ):
            try:
                self._startup_quote_consolidation = self._check_quote_consolidation(
                    base_dry, force=True
                )
            except Exception as exc:
                logger.warning("Startup quote consolidation failed: %s", exc)

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

            cycle_record: dict[str, Any] | None = None
            try:
                cycle_record = self.run_cycle(
                    dry_run=dry_flag,
                    enable_live=enable_flag,
                    mint_rate=mint_rate,
                    progressive_live=progressive_override,
                    listen_base=base_interval,
                )
            except Exception as exc:
                logger.warning(f"single-loop error: {exc}")
                next_interval = base_interval
            else:
                if progressive_state is not None and cycle_record is not None:
                    self._update_progressive_state(
                        progressive_state, cycle_record, live_intent=live_intent
                    )
                if cycle_record is not None:
                    try:
                        next_interval = float(
                            cycle_record.get("listenInterval", base_interval)
                        )
                    except Exception:
                        next_interval = base_interval
                else:
                    next_interval = base_interval
            if max_cycles and cycle >= max_cycles:
                break
            time.sleep(max(0.0, next_interval))
