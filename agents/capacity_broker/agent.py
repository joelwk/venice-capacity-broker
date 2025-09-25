from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from libs.telemetry.logger import get_logger
from services.venice_keys.manager import KeyManager


logger = get_logger("agent.capacity_broker")


@dataclass
class CapacityBroker:
    keys: KeyManager

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

        if violations:
            logger.warning(f"CapacityBroker policy violations detected: {violations}")
        return summary

