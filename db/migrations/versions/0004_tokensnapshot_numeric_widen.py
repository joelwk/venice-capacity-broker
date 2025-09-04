from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Widen integer supply fields to NUMERIC(78,0) to avoid BIGINT overflow
    # Use explicit USING casts for PostgreSQL
    try:
        op.alter_column(
            "tokensnapshot",
            "supply_total",
            type_=sa.Numeric(78, 0),
            existing_type=sa.BigInteger(),
            existing_nullable=True,
            postgresql_using="supply_total::numeric",
        )
    except Exception:
        pass
    try:
        op.alter_column(
            "tokensnapshot",
            "supply_circulating",
            type_=sa.Numeric(78, 0),
            existing_type=sa.BigInteger(),
            existing_nullable=True,
            postgresql_using="supply_circulating::numeric",
        )
    except Exception:
        pass
    try:
        op.alter_column(
            "tokensnapshot",
            "max_total_supply",
            type_=sa.Numeric(78, 0),
            existing_type=sa.BigInteger(),
            existing_nullable=True,
            postgresql_using="max_total_supply::numeric",
        )
    except Exception:
        pass


def downgrade() -> None:
    # Best-effort: narrow back to BIGINT. May fail if values exceed BIGINT range.
    try:
        op.alter_column(
            "tokensnapshot",
            "supply_total",
            type_=sa.BigInteger(),
            existing_type=sa.Numeric(78, 0),
            existing_nullable=True,
            postgresql_using="supply_total::bigint",
        )
    except Exception:
        pass
    try:
        op.alter_column(
            "tokensnapshot",
            "supply_circulating",
            type_=sa.BigInteger(),
            existing_type=sa.Numeric(78, 0),
            existing_nullable=True,
            postgresql_using="supply_circulating::bigint",
        )
    except Exception:
        pass
    try:
        op.alter_column(
            "tokensnapshot",
            "max_total_supply",
            type_=sa.BigInteger(),
            existing_type=sa.Numeric(78, 0),
            existing_nullable=True,
            postgresql_using="max_total_supply::bigint",
        )
    except Exception:
        pass

