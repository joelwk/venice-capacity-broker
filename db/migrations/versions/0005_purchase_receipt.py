"""add receipt column to purchases

Revision ID: 0005_purchase_receipt
Revises: 0004_tokensnapshot_numeric_widen
Create Date: 2025-09-06 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "0005_purchase_receipt"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: ensure table exists; then ensure column exists
    bind = op.get_bind()
    insp = inspect(bind)
    has_purchase = False
    try:
        has_purchase = insp.has_table("purchase")  # type: ignore[attr-defined]
    except Exception:
        # Fallback: reflect columns; empty/exception -> treat as missing
        try:
            insp.get_columns("purchase")
            has_purchase = True
        except Exception:
            has_purchase = False

    if not has_purchase:
        op.create_table(
            "purchase",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("purchase_id", sa.String(), nullable=False),
            sa.Column("quote_id", sa.String(), nullable=False),
            sa.Column("buyer_address", sa.String(), nullable=False),
            sa.Column("asset", sa.String(), nullable=False),
            sa.Column("amount_paid", sa.Numeric(78, 0), nullable=False),
            sa.Column("tx_hash", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("tenant_id", sa.String(), nullable=True),
            sa.Column("subkey", sa.String(), nullable=True),
            sa.Column("key_id", sa.String(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("receipt", sa.Text(), nullable=True),
        )
        # Optional: lightweight indexes to mirror model hints
        try:
            op.create_index("ix_purchase_purchase_id", "purchase", ["purchase_id"])  # type: ignore[list-item]
            op.create_index("ix_purchase_quote_id", "purchase", ["quote_id"])  # type: ignore[list-item]
            op.create_index("ix_purchase_tx_hash", "purchase", ["tx_hash"])  # type: ignore[list-item]
        except Exception:
            pass
        return

    # Table exists; ensure column exists
    existing = {col["name"] for col in insp.get_columns("purchase")}
    if "receipt" not in existing:
        op.add_column("purchase", sa.Column("receipt", sa.Text(), nullable=True))


def downgrade() -> None:
    # Idempotent: drop column if present; do not drop table to avoid data loss in down revs
    bind = op.get_bind()
    insp = inspect(bind)
    try:
        cols = {col["name"] for col in insp.get_columns("purchase")}
    except Exception:
        cols = set()
    if "receipt" in cols:
        op.drop_column("purchase", "receipt")
