from __future__ import annotations

from datetime import datetime
from typing import Optional

try:
    from sqlmodel import SQLModel, Field
except Exception:  # noqa: BLE001
    # Soft fallback to avoid import errors if sqlmodel isn't installed yet
    class SQLModel:  # type: ignore
        pass

    def Field(*args, **kwargs):  # type: ignore
        return None


class Tenant(SQLModel, table=True):  # type: ignore[call-arg]
    id: str = Field(primary_key=True)
    label: str
    status: str = Field(default="active")
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())
    updated_at: datetime = Field(default_factory=lambda: datetime.utcnow())


class Key(SQLModel, table=True):  # type: ignore[call-arg]
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id")
    label: Optional[str] = None
    subkey: str
    quota: int = 0
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())


class Plan(SQLModel, table=True):  # type: ignore[call-arg]
    name: str = Field(primary_key=True)
    monthly_quota: int = 0
    rps: int = 60
    burst: int = 60


class Counter(SQLModel, table=True):  # type: ignore[call-arg]
    """Aggregated usage counters per tenant and time bucket.

    Fields
    - tenant_id: foreign key to tenant.id
    - scope: logical grouping (e.g., 'chat')
    - model: optional model identifier
    - bucket_start: window start (UTC)
    - bucket_seconds: window size
    - count: aggregated count within the bucket
    - created_at: insertion time
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id")
    scope: str = Field(default="chat")
    model: Optional[str] = None
    bucket_start: datetime
    bucket_seconds: int = 60
    count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())
