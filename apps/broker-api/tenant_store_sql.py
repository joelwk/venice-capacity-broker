from __future__ import annotations

"""
SQL-backed TenantStore replacement using SQLModel.

This mirrors the JSON TenantStore interface but persists Tenants/Keys
in the database defined by `db.session`.

Enable by setting `BROKER_STORE_BACKEND=sql` and configuring
`SQL_DATABASE_URL` (or POSTGRES_* envs). Falls back to JSON store if
SQLModel is unavailable or initialization fails.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from datetime import datetime, timezone
import threading

from db.models import Tenant as DbTenant, Key as DbKey
from db.session import get_session, create_db_and_tables


@dataclass
class Tenant:
    id: str
    label: str
    subkey: str
    quota: int
    expires_at: Optional[str] = None
    status: str = "active"  # active|revoked
    owner_address: Optional[str] = None
    key_id: Optional[str] = None


class SQLTenantStore:
    def __init__(self) -> None:
        # Ensure tables exist
        create_db_and_tables()
        self._cache: Dict[str, Tenant] = {}
        self._cache_lock = threading.Lock()
        self._refresh_cache()

    def _refresh_cache(self) -> None:
        try:
            from sqlmodel import select  # lazy import within method
        except Exception:
            with self._cache_lock:
                self._cache = {}
            return

        records: Dict[str, Tenant] = {}
        with next(get_session()) as session:  # type: ignore[call-arg]
            tenants = session.exec(select(DbTenant)).all()
            for db_t in tenants:
                stmt = (
                    select(DbKey)
                    .where(DbKey.tenant_id == db_t.id)
                    .order_by(DbKey.created_at.desc())
                    .limit(1)
                )
                key = session.exec(stmt).first()
                subkey = key.subkey if key is not None else ""
                quota = int(key.quota) if key is not None else 0
                exp = key.expires_at.isoformat().replace('+00:00', 'Z') if (key and key.expires_at) else None
                records[db_t.id] = Tenant(
                    id=db_t.id,
                    label=db_t.label,
                    subkey=subkey,
                    quota=quota,
                    expires_at=exp,
                    status=db_t.status,
                    owner_address=db_t.owner_address,
                    key_id=getattr(key, "key_id", None),
                )
        with self._cache_lock:
            self._cache = records

    def _parse_expires(self, expires_at: Optional[str]) -> Optional[datetime]:
        if not expires_at:
            return None
        s = expires_at.strip()
        try:
            if s.endswith("Z"):
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
            # fromisoformat supports "+00:00" style
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def upsert(self, t: Tenant) -> None:
        from sqlmodel import select  # lazy import within method

        with next(get_session()) as session:  # type: ignore[call-arg]
            # Upsert tenant row
            db_t = session.get(DbTenant, t.id)
            if db_t is None:
                db_t = DbTenant(id=t.id, label=t.label, status=t.status, owner_address=t.owner_address)
                session.add(db_t)
                # Ensure tenant row exists before inserting dependent key
                session.flush()
            else:
                db_t.label = t.label
                db_t.status = t.status
                if t.owner_address:
                    db_t.owner_address = t.owner_address
                db_t.updated_at = datetime.now(timezone.utc)

            # Insert key record (latest subkey/quota/expiry)
            expires_dt = self._parse_expires(t.expires_at)
            key = DbKey(tenant_id=t.id, label=t.label, subkey=t.subkey, quota=int(t.quota), expires_at=expires_dt, key_id=t.key_id)
            session.add(key)
            session.commit()
        self._refresh_cache()

    def get(self, tenant_id: str) -> Optional[Tenant]:
        with self._cache_lock:
            cached = self._cache.get(tenant_id)
        if cached is not None:
            return cached

        from sqlmodel import select  # lazy import within method

        with next(get_session()) as session:  # type: ignore[call-arg]
            db_t = session.get(DbTenant, tenant_id)
            if db_t is None:
                return None
            stmt = (
                select(DbKey)
                .where(DbKey.tenant_id == tenant_id)
                .order_by(DbKey.created_at.desc())
                .limit(1)
            )
            key = session.exec(stmt).first()
            subkey = key.subkey if key is not None else ""
            quota = int(key.quota) if key is not None else 0
            exp = key.expires_at.isoformat().replace("+00:00", "Z") if (key and key.expires_at) else None
            result = Tenant(id=db_t.id, label=db_t.label, subkey=subkey, quota=quota, expires_at=exp, status=db_t.status, owner_address=db_t.owner_address, key_id=getattr(key, "key_id", None))
        with self._cache_lock:
            self._cache[tenant_id] = result
        return result

    def delete(self, tenant_id: str) -> None:
        # Soft-delete by marking revoked to avoid FK issues; mirror JSON store semantics where possible.
        with next(get_session()) as session:  # type: ignore[call-arg]
            db_t = session.get(DbTenant, tenant_id)
            if db_t is None:
                return
            db_t.status = "revoked"
            db_t.updated_at = datetime.now(timezone.utc)
            session.add(db_t)
            session.commit()
        with self._cache_lock:
            self._cache.pop(tenant_id, None)

    def all(self) -> Dict[str, Tenant]:
        with self._cache_lock:
            if self._cache:
                return dict(self._cache)
        self._refresh_cache()
        with self._cache_lock:
            return dict(self._cache)
