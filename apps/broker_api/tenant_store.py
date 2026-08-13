from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Tenant:
    id: str
    label: str
    subkey: str
    quota: float
    expires_at: str | None = None
    status: str = "active"  # active|revoked
    # Optional Venice API key id to allow revoke via API
    key_id: str | None = None
    owner_address: str | None = None


class TenantStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(
            path or os.getenv("BROKER_STORE_FILE", "apps/broker_api/tenants.json")
        )
        self._lock = threading.Lock()
        self._data: dict[str, Tenant] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self._data = {k: Tenant(**v) for k, v in raw.items()}
            except Exception:
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        obj = {k: asdict(v) for k, v in self._data.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        tmp.replace(self.path)

    def upsert(self, t: Tenant) -> None:
        with self._lock:
            self._data[t.id] = t
            self._save()

    def get(self, tenant_id: str) -> Tenant | None:
        return self._data.get(tenant_id)

    def delete(self, tenant_id: str) -> None:
        with self._lock:
            if tenant_id in self._data:
                del self._data[tenant_id]
                self._save()

    def all(self) -> dict[str, Tenant]:
        return dict(self._data)
