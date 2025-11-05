from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from libs.telemetry.logger import get_logger
from services.venice_keys.manager import KeyManager


logger = get_logger("agent.capacity_broker")


@dataclass
class CapacityBroker:
    keys: KeyManager
    _last_price: Optional[float] = None
    _last_price_ts: Optional[float] = None
    _price_history: List[Dict[str, Any]] = None  # type: ignore

    def __post_init__(self) -> None:
        if self._price_history is None:
            self._price_history = []

    def issue_tenant_key(self, parent_key: str, tenant_id: str, daily_quota: int) -> Dict[str, str]:
        label = f"tenant:{tenant_id}"
        res = self.keys.issue_scoped_key(parent_key, label=label, consumption_limit=daily_quota)
        logger.info(f"Issued key for {tenant_id}: {res}")
        return {"status": "ok", "tenant": tenant_id}

    def run_once(
        self,
        *,
        parent_key: Optional[str] = None,
        enforce_limits: bool = True,
    ) -> Dict[str, Any]:
        """Check broker health, usage, and scoped-key policy compliance."""

        client = self.keys.client
        effective_key = parent_key or client.config.api_key
        if not effective_key:
            logger.debug("CapacityBroker skipped: no parent key configured")
            return {"status": "skipped", "reason": "missing_parent_key"}

        usage: Dict[str, Any] | None = None
        limits: Dict[str, Any] | None = None
        violations: List[Dict[str, Any]] = []
        errors: Dict[str, str] = {}
        utilization_ratio: Optional[float] = None
        pricing: Optional[Dict[str, Any]] = None
        failsafe: Optional[Dict[str, Any]] = None

        try:
            usage = client.get_usage()
        except Exception as exc:  # noqa: BLE001
            errors["usage"] = str(exc)

        try:
            limits = client.get_rate_limits()
        except Exception as exc:  # noqa: BLE001
            errors["limits"] = str(exc)

        if enforce_limits and limits is not None:
            entries: List[Dict[str, Any]] = []
            if isinstance(limits, dict):
                for key in ("data", "items", "keys"):
                    value = limits.get(key)
                    if isinstance(value, list):
                        entries = value
                        break
            elif isinstance(limits, list):
                entries = limits

            for entry in entries:
                limit = entry.get("consumptionLimit") or entry.get("consumption_limit")
                expiry = entry.get("expiresAt") or entry.get("expires_at")
                if not limit or not expiry:
                    violations.append(
                        {
                            "id": entry.get("id"),
                            "label": entry.get("description") or entry.get("label"),
                            "missing_limit": limit is None,
                            "missing_expiry": not bool(expiry),
                        }
                    )

        usage_total = self._extract_usage_diem(usage)
        limit_total = self._extract_limit_total(limits)
        if limit_total and limit_total > 0 and usage_total is not None:
            utilization_ratio = max(0.0, min(1.0, float(usage_total) / float(limit_total)))

        if utilization_ratio is not None:
            pricing, failsafe = self._derive_inventory_policy(utilization_ratio)
            
            # Track pricing changes for hysteresis and rollback
            if pricing and pricing.get("proposed") is not None:
                import time
                proposed = float(pricing["proposed"])
                if self._last_price is None or abs(proposed - self._last_price) > 1e-6:
                    self._price_history.append({
                        "ts": time.time(),
                        "utilization": utilization_ratio,
                        "proposed": proposed,
                        "current": self._last_price,
                        "mode": pricing.get("mode"),
                    })
                    # Keep last 10 price changes
                    if len(self._price_history) > 10:
                        self._price_history.pop(0)
                self._last_price = proposed
                self._last_price_ts = time.time()
                pricing["lastApplied"] = self._last_price_ts
                pricing["historyLength"] = len(self._price_history)

        summary: Dict[str, Any] = {
            "status": "ok" if not errors else "degraded",
            "enforce_limits": bool(enforce_limits),
            "violations": violations,
        }
        if usage is not None:
            summary["usage"] = usage
        if limits is not None:
            summary["limits"] = limits
        if errors:
            summary["errors"] = errors
        if utilization_ratio is not None:
            summary["utilization"] = utilization_ratio
        if pricing is not None:
            summary["pricing"] = pricing
        if failsafe is not None:
            summary["inventoryFailsafe"] = failsafe

        if violations:
            logger.warning(f"CapacityBroker policy violations detected: {violations}")
        return summary

    # ------------------------------------------------------------------
    def _extract_usage_diem(self, usage: Any) -> Optional[float]:
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
            totals: List[float] = []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                for field in ("dailyAverageDiem", "daily_average_diem", "consumptionDaily", "consumption"):
                    if field in entry:
                        try:
                            totals.append(float(entry[field]))
                        except Exception:
                            continue
                        break
            if totals:
                return sum(totals) / len(totals)
        aggregate = usage.get("aggregate")
        if isinstance(aggregate, dict):
            for key in ("daily", "diemDaily"):
                if key in aggregate:
                    try:
                        return float(aggregate[key])
                    except Exception:
                        continue
        return None

    def _extract_limit_total(self, limits: Any) -> Optional[float]:
        if limits is None:
            return None
        entries: List[Any]
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
                entries = [value for value in limits.values() if isinstance(value, dict)]
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
            if amount is None:
                continue
            try:
                total += float(amount)
            except Exception:
                continue
        return total if total > 0 else None

    def _derive_inventory_policy(self, utilization: float) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        surge_threshold = float(os.getenv("BROKER_UTIL_SURGE_THRESHOLD", "0.85"))
        relax_threshold = float(os.getenv("BROKER_UTIL_RELAX_THRESHOLD", "0.40"))
        base_price = float(os.getenv("BROKER_BASE_PRICE_USD", "1.0"))
        surge_multiplier = float(os.getenv("BROKER_SURGE_MULTIPLIER", "2.0"))
        util_target = float(os.getenv("BROKER_UTIL_TARGET", "0.65"))
        price_step_bps = int(os.getenv("BROKER_PRICE_STEP_BPS", "50"))
        discount_max_bps = int(os.getenv("BROKER_DISCOUNT_MAX_BPS", "500"))
        hysteresis_window = float(os.getenv("BROKER_HYSTERESIS_WINDOW", "0.05"))
        
        pricing: Optional[Dict[str, Any]] = None
        failsafe: Optional[Dict[str, Any]] = None

        current_price = self._last_price if self._last_price is not None else base_price
        price_delta_bps = 0
        
        if utilization >= surge_threshold:
            intensity = max(0.0, min(1.0, (utilization - surge_threshold) / max(1e-6, 1.0 - surge_threshold)))
            surge_factor = 1.0 + intensity * surge_multiplier
            suggested = round(base_price * surge_factor, 6)
            
            if utilization > util_target + hysteresis_window:
                price_delta_bps = min(price_step_bps * 2, int((utilization - util_target) * 10000))
            elif utilization > util_target:
                price_delta_bps = price_step_bps
            
            proposed_price = current_price * (1.0 + price_delta_bps / 10000.0)
            
            pricing = {
                "mode": "surge",
                "base": base_price,
                "current": current_price,
                "suggested": suggested,
                "proposed": proposed_price,
                "surgeFactor": surge_factor,
                "priceDeltaBps": price_delta_bps,
            }
            failsafe = {
                "status": "hot",
                "utilization": utilization,
                "actions": ["pause_low_tier", "raise_price", "offer_rental"],
            }
        elif utilization <= relax_threshold:
            discount_factor = max(0.0, min(0.25, (relax_threshold - utilization) * 0.5))
            suggested = round(base_price * (1 - discount_factor), 6)
            
            if utilization < util_target - hysteresis_window:
                price_delta_bps = -min(discount_max_bps, int((util_target - utilization) * 10000))
            elif utilization < util_target:
                price_delta_bps = -price_step_bps
            
            proposed_price = max(base_price * 0.5, current_price * (1.0 + price_delta_bps / 10000.0))
            
            pricing = {
                "mode": "discount",
                "base": base_price,
                "current": current_price,
                "suggested": suggested,
                "proposed": proposed_price,
                "discount": discount_factor,
                "priceDeltaBps": price_delta_bps,
            }
            failsafe = {
                "status": "calm",
                "utilization": utilization,
                "actions": ["open_intake"],
            }
        else:
            if abs(utilization - util_target) < hysteresis_window:
                proposed_price = current_price
            else:
                if utilization > util_target:
                    price_delta_bps = price_step_bps
                else:
                    price_delta_bps = -price_step_bps
                proposed_price = current_price * (1.0 + price_delta_bps / 10000.0)
            
            pricing = {
                "mode": "normal",
                "base": base_price,
                "current": current_price,
                "proposed": proposed_price,
                "priceDeltaBps": price_delta_bps,
            }

        return pricing, failsafe

