from __future__ import annotations

from datetime import datetime, timezone

try:  # optional: only needed when building real SQL tables
    import sqlalchemy as sa  # type: ignore

    _HAS_SA = hasattr(sa, "Column") and hasattr(sa, "Numeric")
except Exception:
    sa = None  # type: ignore
    _HAS_SA = False

try:
    from sqlmodel import Field, SQLModel
except Exception:
    # Soft fallback to avoid import errors if sqlmodel isn't installed yet.
    # FastAPI route registration imports this module at startup. When
    # sqlmodel isn't available, class definitions like
    #   class Foo(SQLModel, table=True)
    # would raise because the base doesn't accept the "table" kw.
    # Implement __init_subclass__ to swallow any kwargs so import succeeds.
    class SQLModel:  # type: ignore
        @classmethod
        def __init_subclass__(cls, **kwargs):  # type: ignore[override]
            # Ignore SQLModel-specific subclass kwargs (e.g., table=True)
            return None

    def Field(*args, **kwargs):  # type: ignore
        # Placeholder that allows annotations to parse without sqlmodel.
        return None

else:
    # On reloads during tests we may already have tables bound to the shared
    # SQLModel metadata. Clearing avoids duplicate table definitions when this
    # module is re-imported after sys.modules entries are purged.
    if hasattr(SQLModel, "metadata") and getattr(SQLModel.metadata, "tables", None):
        SQLModel.metadata.clear()
    # SQLModel also keeps a declarative registry that surfaces SAWarnings when
    # classes are redefined under new module names (common in importlib tests).
    # Disposing resets the class lookup table so repeated imports remain clean.
    try:
        registry = SQLModel._sa_registry  # type: ignore[attr-defined]
    except AttributeError:
        registry = None
    if registry is not None:
        try:
            registry.dispose()
        except Exception:
            # Fall back to clearing the internal class map when dispose is unavailable.
            try:
                registry._class_registry.clear()  # type: ignore[attr-defined]
            except Exception:
                pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(SQLModel, table=True):  # type: ignore[call-arg]
    id: str = Field(primary_key=True)
    label: str
    status: str = Field(default="active")
    owner_address: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Key(SQLModel, table=True):  # type: ignore[call-arg]
    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id")
    label: str | None = None
    subkey: str
    key_id: str | None = Field(default=None)
    quota: int = 0
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


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

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id")
    scope: str = Field(default="chat")
    model: str | None = None
    bucket_start: datetime
    bucket_seconds: int = 60
    count: int = 0
    created_at: datetime = Field(default_factory=_utcnow)


# --- New: Quotes and Purchases for Buyer Flow ---
try:
    from sqlmodel import Index  # type: ignore
except Exception:
    Index = None  # type: ignore


class Quote(SQLModel, table=True):  # type: ignore[call-arg]
    """Priced offer to purchase units of compute.

    units: logical units (e.g., diem/day); asset: ETH|USDC; prices in smallest unit for asset
    status: open|expired|filled
    """

    id: int | None = Field(default=None, primary_key=True)
    quote_id: str = Field(index=True)
    units: float
    asset: str
    # Use high-precision NUMERIC to avoid overflow for wei amounts on Postgres
    if _HAS_SA:
        unit_price: int = Field(sa_column=sa.Column(sa.Numeric(78, 0), nullable=False))  # type: ignore[call-arg]
        total_price: int = Field(sa_column=sa.Column(sa.Numeric(78, 0), nullable=False))  # type: ignore[call-arg]
        accepted_min: float | None = Field(
            default=None, sa_column=sa.Column(sa.Numeric(24, 6), nullable=True)
        )  # type: ignore[call-arg]
        accepted_max: float | None = Field(
            default=None, sa_column=sa.Column(sa.Numeric(24, 6), nullable=True)
        )  # type: ignore[call-arg]
    else:
        unit_price: int  # price per unit in smallest unit of asset
        total_price: int  # total price in smallest unit of asset
        accepted_min: float | None = None
        accepted_max: float | None = None
    expires_at: datetime
    status: str = Field(default="open")
    created_at: datetime = Field(default_factory=_utcnow)


class Purchase(SQLModel, table=True):  # type: ignore[call-arg]
    id: int | None = Field(default=None, primary_key=True)
    purchase_id: str = Field(index=True)
    quote_id: str = Field(index=True)
    buyer_address: str
    asset: str
    if _HAS_SA:
        amount_paid: int = Field(sa_column=sa.Column(sa.Numeric(78, 0), nullable=False))  # type: ignore[call-arg]
        # Unique: one payment transaction can only ever fund one purchase,
        # which serializes concurrent verify calls (double key mint guard).
        tx_hash: str = Field(
            sa_column=sa.Column(sa.String, nullable=False, unique=True, index=True)
        )  # type: ignore[call-arg]
    else:
        amount_paid: int
        tx_hash: str = Field(index=True)
    status: str = Field(default="pending")  # pending|confirmed|fulfilled|failed
    tenant_id: str | None = None
    subkey: str | None = None
    key_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    fulfilled_at: datetime | None = None
    # JSON blob with verification/audit details (tx, amounts, chain, etc.)
    receipt: str | None = None

    # Simple unique constraint alternatives via indexes when SQLModel supports; rely on application checks otherwise.


# --- Token Tracking (BaseScan/Etherscan) ---
class AssetToken(SQLModel, table=True):  # type: ignore[call-arg]
    """Metadata for a tracked ERC-20 token on a given chain."""

    address: str = Field(primary_key=True)
    chain: str = Field(default="base")
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class TokenSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    """Periodic snapshot of token market and chain stats.

    Prices are stored as floats for simplicity; on critical paths prefer fixed‑point.
    """

    id: int | None = Field(default=None, primary_key=True)
    token_address: str = Field(foreign_key="assettoken.address")
    ts: datetime = Field(default_factory=_utcnow, index=True)
    price_usd: float | None = None
    # Use high-precision NUMERIC to avoid BIGINT overflow for large ERC-20 supplies
    # In test environments without SQLAlchemy, fall back to plain Optional[int]
    if _HAS_SA:
        supply_total: int | None = Field(
            sa_column=sa.Column(sa.Numeric(78, 0), nullable=True)
        )  # type: ignore[call-arg]
        supply_circulating: int | None = Field(
            sa_column=sa.Column(sa.Numeric(78, 0), nullable=True)
        )  # type: ignore[call-arg]
    else:
        supply_total: int | None = None
        supply_circulating: int | None = None
    holders: int | None = None
    transfers_24h: int | None = None
    marketcap_usd: float | None = None
    if _HAS_SA:
        max_total_supply: int | None = Field(
            sa_column=sa.Column(sa.Numeric(78, 0), nullable=True)
        )  # type: ignore[call-arg]
    else:
        max_total_supply: int | None = None
    raw_json: str | None = None  # lightly structured JSON payload for auditing


class Decision(SQLModel, table=True):  # type: ignore[call-arg]
    """Agent/orchestrator decision log for observability."""

    id: int | None = Field(default=None, primary_key=True)
    agent: str = Field(index=True)
    action: str
    correlation_id: str | None = None
    details: str | None = None  # JSON string with context
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class PriceTick(SQLModel, table=True):  # type: ignore[call-arg]
    """Lightweight price tick storage for optional volatility analytics.

    Enabled via env `RISK_VOL_PERSIST`. SQLite/Postgres supported via SQLModel.
    """

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(default="DIEM")
    ts: datetime = Field(default_factory=_utcnow, index=True)
    price_usd: float


class DexFactoryCursor(SQLModel, table=True):  # type: ignore[call-arg]
    """Track last processed block for each factory when monitoring PairCreated events."""

    factory_address: str = Field(primary_key=True)
    factory_type: str = Field(default="uniswap_v2")
    chain_id: int | None = None
    last_block: int | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class DexPool(SQLModel, table=True):  # type: ignore[call-arg]
    """Light catalog of discovered pools/pairs for automatic route synthesis."""

    pool_address: str = Field(primary_key=True)
    factory_address: str = Field(index=True)
    factory_type: str = Field(default="uniswap_v2", index=True)
    chain_id: int | None = Field(default=None, index=True)
    token0: str = Field(index=True)
    token1: str = Field(index=True)
    fee: int | None = Field(default=None)
    stable: bool | None = Field(default=None)
    tick_spacing: int | None = Field(default=None)
    block_number: int | None = None
    tx_hash: str | None = Field(default=None, index=True)
    discovered_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    if _HAS_SA:
        reserve0: int | None = Field(
            sa_column=sa.Column(sa.Numeric(78, 0), nullable=True)
        )  # type: ignore[call-arg]
        reserve1: int | None = Field(
            sa_column=sa.Column(sa.Numeric(78, 0), nullable=True)
        )  # type: ignore[call-arg]
        liquidity: int | None = Field(
            sa_column=sa.Column(sa.Numeric(78, 0), nullable=True)
        )  # type: ignore[call-arg]
    else:
        reserve0: int | None = None
        reserve1: int | None = None
        liquidity: int | None = None


# --- Bids (EIP-712 purchase intents) ---
class Bid(SQLModel, table=True):  # type: ignore[call-arg]
    id: int | None = Field(default=None, primary_key=True)
    bid_id: str = Field(index=True)
    buyer_address: str = Field(index=True)
    units: float
    max_price: int  # price per unit in smallest unit of asset
    asset: str
    expiry: datetime
    slippage_bps: int = 0
    nonce: int = 0
    quote_id: str | None = Field(default=None, index=True)
    status: str = Field(
        default="received"
    )  # received|out_of_band|in_band|accepted_window|expired|filled
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    context: str | None = None  # JSON blob with extra info (e.g., last clearing price)


# --- Agent memory (SQL-backed) ---
class AgentMemory(SQLModel, table=True):  # type: ignore[call-arg]
    """Agent cycle memory entries with retention policy.

    Payload stores sanitized JSON context for the cycle.
    """

    # String UUID for portability without adding a hard dependency
    id: str | None = Field(default=None, primary_key=True)
    agent: str = Field(index=True)
    cycle_id: str | None = None
    # Foreign key to Decision when available (nullable)
    decision_id: int | None = Field(default=None, foreign_key="decision.id")
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    if _HAS_SA:
        payload: dict | None = Field(
            default=None, sa_column=sa.Column(sa.JSON, nullable=True)
        )  # type: ignore[call-arg]
    else:
        payload: str | None = None  # fallback: store as JSON string when SA is absent
