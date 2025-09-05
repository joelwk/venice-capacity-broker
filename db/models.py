from __future__ import annotations

from datetime import datetime
from typing import Optional
try:  # optional: only needed when building real SQL tables
    import sqlalchemy as sa  # type: ignore
    _HAS_SA = hasattr(sa, "Column") and hasattr(sa, "Numeric")
except Exception:  # noqa: BLE001
    sa = None  # type: ignore
    _HAS_SA = False

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


# --- New: Quotes and Purchases for Buyer Flow ---
try:
    from sqlmodel import Index  # type: ignore
except Exception:  # noqa: BLE001
    Index = None  # type: ignore


class Quote(SQLModel, table=True):  # type: ignore[call-arg]
    """Priced offer to purchase units of compute.

    units: logical units (e.g., diem/day); asset: ETH|USDC; prices in smallest unit for asset
    status: open|expired|filled
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    quote_id: str = Field(index=True)
    units: int
    asset: str
    unit_price: int  # price per unit in smallest unit of asset
    total_price: int  # total price in smallest unit of asset
    accepted_min: Optional[int] = None
    accepted_max: Optional[int] = None
    expires_at: datetime
    status: str = Field(default="open")
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())


class Purchase(SQLModel, table=True):  # type: ignore[call-arg]
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: str = Field(index=True)
    quote_id: str = Field(index=True)
    buyer_address: str
    asset: str
    amount_paid: int
    tx_hash: str = Field(index=True)
    status: str = Field(default="pending")  # pending|confirmed|fulfilled|failed
    tenant_id: Optional[str] = None
    subkey: Optional[str] = None
    key_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())
    fulfilled_at: Optional[datetime] = None

    # Simple unique constraint alternatives via indexes when SQLModel supports; rely on application checks otherwise.


# --- Token Tracking (BaseScan/Etherscan) ---
class AssetToken(SQLModel, table=True):  # type: ignore[call-arg]
    """Metadata for a tracked ERC-20 token on a given chain."""

    address: str = Field(primary_key=True)
    chain: str = Field(default="base")
    symbol: Optional[str] = None
    name: Optional[str] = None
    decimals: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())
    updated_at: datetime = Field(default_factory=lambda: datetime.utcnow())


class TokenSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    """Periodic snapshot of token market and chain stats.

    Prices are stored as floats for simplicity; on critical paths prefer fixed‑point.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    token_address: str = Field(foreign_key="assettoken.address")
    ts: datetime = Field(default_factory=lambda: datetime.utcnow(), index=True)
    price_usd: Optional[float] = None
    # Use high-precision NUMERIC to avoid BIGINT overflow for large ERC-20 supplies
    # In test environments without SQLAlchemy, fall back to plain Optional[int]
    if _HAS_SA:
        supply_total: Optional[int] = Field(sa_column=sa.Column(sa.Numeric(78, 0), nullable=True))  # type: ignore[call-arg]
        supply_circulating: Optional[int] = Field(sa_column=sa.Column(sa.Numeric(78, 0), nullable=True))  # type: ignore[call-arg]
    else:
        supply_total: Optional[int] = None
        supply_circulating: Optional[int] = None
    holders: Optional[int] = None
    transfers_24h: Optional[int] = None
    marketcap_usd: Optional[float] = None
    if _HAS_SA:
        max_total_supply: Optional[int] = Field(sa_column=sa.Column(sa.Numeric(78, 0), nullable=True))  # type: ignore[call-arg]
    else:
        max_total_supply: Optional[int] = None
    raw_json: Optional[str] = None  # lightly structured JSON payload for auditing


class Decision(SQLModel, table=True):  # type: ignore[call-arg]
    """Agent/orchestrator decision log for observability."""

    id: Optional[int] = Field(default=None, primary_key=True)
    agent: str
    action: str
    details: Optional[str] = None  # JSON string with context
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())
