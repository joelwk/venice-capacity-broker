from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

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

