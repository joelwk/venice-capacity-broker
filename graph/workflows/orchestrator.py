from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Any

from libs.telemetry.logger import get_logger
from libs.telemetry.tracing import annotate_span

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # noqa: BLE001
    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return

logger = get_logger("workflow.orchestrator")


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

        if dry_run:
            try:
                px = float(os.getenv("DIEM_FAKE_PRICE") or os.getenv("TEST_DIEM_PRICE") or 1.0)
            except Exception:
                px = 1.0
            prices: Dict[str, float] = {"DIEM": px}
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
                        mint_rate=mint_rate,
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
                        mint_rate=mint_rate,
                        desired_units=None,
                        current_inventory_usd=None,
                        simulate=True,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
                else:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=mint_rate,
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
                        mint_rate=mint_rate,
                        desired_units=None,
                        current_inventory_usd=current_inventory_usd,
                        corr_id=corr,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
                else:
                    decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                        px,
                        mint_rate=mint_rate,
                        desired_units=None,
                        current_inventory_usd=current_inventory_usd,
                        utilization_ratio=utilization_ratio if "utilization_ratio" in params else None,
                        vol_bps=vol_bps if "vol_bps" in params else None,
                    )
            except Exception:
                decision = self.arbi.evaluate_and_maybe_mint(  # type: ignore[attr-defined]
                    px, mint_rate=mint_rate, desired_units=None, current_inventory_usd=current_inventory_usd
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
