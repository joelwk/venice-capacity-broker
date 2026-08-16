from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from libs.telemetry.logger import get_logger
from services.broker.inventory import (
    broker_inventory_utilization,
    failsafe_actions,
    failsafe_status,
    save_inventory_policy,
)
from services.venice_keys.manager import KeyManager

logger = get_logger("agent.capacity_broker")


@dataclass
class CapacityBroker:
    keys: KeyManager
    _last_price: float | None = None
    _last_price_ts: float | None = None
    _price_history: list[dict[str, Any]] = None  # type: ignore
    _last_seen_key_created_at: datetime | None = None
    _last_seen_revoked_tenants: set[str] = None  # type: ignore

    def __post_init__(self) -> None:
        if self._price_history is None:
            self._price_history = []
        if self._last_seen_revoked_tenants is None:
            self._last_seen_revoked_tenants = set()

    def _redact_key_payload(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        redacted = dict(payload)
        for k in ("apiKey", "api_key", "key", "token"):
            if redacted.get(k):
                redacted[k] = "[redacted]"
        return redacted

    def issue_tenant_key(
        self,
        parent_key: str,
        tenant_id: str,
        daily_quota: int,
        expires_at: str | None = None,
    ) -> dict[str, str]:
        if expires_at is None or str(expires_at).strip() == "":
            try:
                from apps.broker_api.config import compute_expires_at
            except Exception:
                compute_expires_at = None  # type: ignore[assignment]
            if compute_expires_at is not None:
                expires_at = compute_expires_at(time.time())
        if expires_at is None or str(expires_at).strip() == "":
            error_msg = (
                "expires_at is required for tenant subkeys "
                "(set BROKER_DEFAULT_EXPIRY_DAYS>0)"
            )
            raise ValueError(error_msg)
        label = f"tenant:{tenant_id}"
        res = self.keys.issue_scoped_key(
            parent_key,
            label=label,
            consumption_limit=daily_quota,
            expires_at=expires_at,
        )
        logger.info("Issued key for %s: %s", tenant_id, self._redact_key_payload(res))
        return {"status": "ok", "tenant": tenant_id}

    def _broker_activity_snapshot(self) -> dict[str, Any]:
        """Best-effort broker activity counters for orchestration summaries."""
        if os.getenv("PYTEST_CURRENT_TEST") and (
            os.getenv("BROKER_ACTIVITY_SNAPSHOT_ENABLE_IN_TESTS") or ""
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            return {}
        try:
            from sqlmodel import Session, select

            from db.models import Key as DbKey
            from db.models import Tenant as DbTenant
            from db.session import get_engine
        except Exception:
            return {}

        issued_delta = 0
        revoked_delta = 0
        active_tenants = 0
        last_issue_ts: int | None = None

        engine = get_engine()
        with Session(engine) as s:  # type: ignore[call-arg]
            try:
                active_rows = s.exec(
                    select(DbTenant.id).where(DbTenant.status == "active")
                ).all()
                active_tenants = len(active_rows)
            except Exception:
                active_tenants = 0

            try:
                revoked_rows = s.exec(
                    select(DbTenant.id).where(DbTenant.status != "active")
                ).all()
                revoked_now = {str(r) for r in revoked_rows}
                revoked_delta = len(revoked_now - self._last_seen_revoked_tenants)
                self._last_seen_revoked_tenants = revoked_now
            except Exception:
                revoked_delta = 0

            last_created: datetime | None = None
            try:
                last_created = s.exec(
                    select(DbKey.created_at).order_by(DbKey.created_at.desc()).limit(1)
                ).first()
            except Exception:
                last_created = None

            if last_created is not None:
                try:
                    if last_created.tzinfo is None:
                        last_created = last_created.replace(tzinfo=timezone.utc)
                    last_issue_ts = int(last_created.timestamp())
                except Exception:
                    last_issue_ts = None

            if self._last_seen_key_created_at is None:
                self._last_seen_key_created_at = last_created
                issued_delta = 0
            elif last_created is None:
                issued_delta = 0
            else:
                try:
                    issued_rows = s.exec(
                        select(DbKey.id).where(
                            DbKey.created_at > self._last_seen_key_created_at
                        )
                    ).all()
                    issued_delta = len(issued_rows)
                except Exception:
                    issued_delta = 0
                self._last_seen_key_created_at = last_created

        return {
            "active_tenants": int(active_tenants),
            "issued_keys": int(issued_delta),
            "revoked_keys": int(revoked_delta),
            "last_key_issue_ts": last_issue_ts,
        }

    def run_once(
        self,
        *,
        parent_key: str | None = None,
        enforce_limits: bool = True,
    ) -> dict[str, Any]:
        """Check broker health, usage, and scoped-key policy compliance."""

        client = self.keys.client
        effective_key = parent_key or client.config.api_key
        if not effective_key:
            logger.debug("CapacityBroker skipped: no parent key configured")
            return {"status": "skipped", "reason": "missing_parent_key"}

        usage: dict[str, Any] | None = None
        limits: dict[str, Any] | None = None
        violations: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        utilization_ratio: float | None = None
        pricing: dict[str, Any] | None = None
        failsafe: dict[str, Any] | None = None

        try:
            usage = client.get_usage()
        except Exception as exc:
            errors["usage"] = str(exc)

        try:
            limits = client.get_rate_limits()
        except Exception as exc:
            errors["limits"] = str(exc)

        if enforce_limits and limits is not None:
            entries: list[dict[str, Any]] = []
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

        utilization_ratio = broker_inventory_utilization(usage, limits)

        if utilization_ratio is not None:
            pricing, failsafe = self._derive_inventory_policy(utilization_ratio)

            # Track pricing changes for hysteresis and rollback
            if pricing and pricing.get("proposed") is not None:
                import time

                proposed = float(pricing["proposed"])
                if self._last_price is None or abs(proposed - self._last_price) > 1e-6:
                    self._price_history.append(
                        {
                            "ts": time.time(),
                            "utilization": utilization_ratio,
                            "proposed": proposed,
                            "current": self._last_price,
                            "mode": pricing.get("mode"),
                        }
                    )
                    # Keep last 10 price changes
                    if len(self._price_history) > 10:
                        self._price_history.pop(0)
                self._last_price = proposed
                self._last_price_ts = time.time()
                pricing["lastApplied"] = self._last_price_ts
                pricing["historyLength"] = len(self._price_history)

            status = (
                str(failsafe.get("status"))
                if failsafe
                else failsafe_status(utilization_ratio)
            )
            save_inventory_policy(
                utilization=utilization_ratio,
                status=status,
                pricing=pricing,
            )

        summary: dict[str, Any] = {
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
        summary.update(self._broker_activity_snapshot())

        if violations:
            logger.warning(f"CapacityBroker policy violations detected: {violations}")
        return summary

    def _derive_inventory_policy(
        self, utilization: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        surge_threshold = float(os.getenv("BROKER_UTIL_SURGE_THRESHOLD", "0.85"))
        relax_threshold = float(os.getenv("BROKER_UTIL_RELAX_THRESHOLD", "0.40"))
        base_price = float(os.getenv("BROKER_BASE_PRICE_USD", "1.0"))
        surge_multiplier = float(os.getenv("BROKER_SURGE_MULTIPLIER", "2.0"))
        util_target = float(os.getenv("BROKER_UTIL_TARGET", "0.65"))
        price_step_bps = int(os.getenv("BROKER_PRICE_STEP_BPS", "50"))
        discount_max_bps = int(os.getenv("BROKER_DISCOUNT_MAX_BPS", "500"))
        hysteresis_window = float(os.getenv("BROKER_HYSTERESIS_WINDOW", "0.05"))

        pricing: dict[str, Any] | None = None
        failsafe: dict[str, Any] | None = None

        current_price = self._last_price if self._last_price is not None else base_price
        price_delta_bps = 0

        if utilization >= surge_threshold:
            intensity = max(
                0.0,
                min(
                    1.0,
                    (utilization - surge_threshold) / max(1e-6, 1.0 - surge_threshold),
                ),
            )
            surge_factor = 1.0 + intensity * surge_multiplier
            suggested = round(base_price * surge_factor, 6)

            if utilization > util_target + hysteresis_window:
                price_delta_bps = min(
                    price_step_bps * 2, int((utilization - util_target) * 10000)
                )
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
                "actions": failsafe_actions("hot"),
            }
        elif utilization <= relax_threshold:
            discount_factor = max(0.0, min(0.25, (relax_threshold - utilization) * 0.5))
            suggested = round(base_price * (1 - discount_factor), 6)

            if utilization < util_target - hysteresis_window:
                price_delta_bps = -min(
                    discount_max_bps, int((util_target - utilization) * 10000)
                )
            elif utilization < util_target:
                price_delta_bps = -price_step_bps

            proposed_price = max(
                base_price * 0.5, current_price * (1.0 + price_delta_bps / 10000.0)
            )

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
                "actions": failsafe_actions("calm"),
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
