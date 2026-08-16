"""create bid table and link settled quotes

Bids were modeled in SQLModel without an Alembic revision, so production
migrations never created the table. Settlement now persists a quote and
stores quote_id on the bid so verify can fill the same row.

Revision ID: 0011_add_bid_quote_id
Revises: 0010_purchase_tx_hash_unique
Create Date: 2026-08-16 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = "0011_add_bid_quote_id"
down_revision = "0010_purchase_tx_hash_unique"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    try:
        insp = Inspector.from_engine(conn)  # type: ignore[arg-type]
        return name in set(insp.get_table_names())
    except Exception:
        return False


def _has_column(conn, table: str, column: str) -> bool:
    try:
        insp = Inspector.from_engine(conn)  # type: ignore[arg-type]
        return any(col.get("name") == column for col in insp.get_columns(table))
    except Exception:
        return False


def _has_index(conn, table: str, index: str) -> bool:
    try:
        insp = Inspector.from_engine(conn)  # type: ignore[arg-type]
        return any(idx.get("name") == index for idx in insp.get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "bid"):
        op.create_table(
            "bid",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("bid_id", sa.String(), nullable=False),
            sa.Column("buyer_address", sa.String(), nullable=False),
            sa.Column("units", sa.Float(), nullable=False),
            sa.Column("max_price", sa.Integer(), nullable=False),
            sa.Column("asset", sa.String(), nullable=False),
            sa.Column("expiry", sa.DateTime(), nullable=False),
            sa.Column("slippage_bps", sa.Integer(), nullable=False),
            sa.Column("nonce", sa.Integer(), nullable=False),
            sa.Column("quote_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("context", sa.String(), nullable=True),
        )
    elif not _has_column(conn, "bid", "quote_id"):
        op.add_column("bid", sa.Column("quote_id", sa.String(), nullable=True))

    if _has_table(conn, "bid"):
        if not _has_index(conn, "bid", "ix_bid_bid_id"):
            op.create_index("ix_bid_bid_id", "bid", ["bid_id"])
        if not _has_index(conn, "bid", "ix_bid_buyer_address"):
            op.create_index("ix_bid_buyer_address", "bid", ["buyer_address"])
        if not _has_index(conn, "bid", "ix_bid_quote_id"):
            op.create_index("ix_bid_quote_id", "bid", ["quote_id"])


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "bid") and _has_column(conn, "bid", "quote_id"):
        try:
            op.drop_index("ix_bid_quote_id", table_name="bid")
        except Exception:
            pass
        try:
            op.drop_column("bid", "quote_id")
        except Exception:
            pass
