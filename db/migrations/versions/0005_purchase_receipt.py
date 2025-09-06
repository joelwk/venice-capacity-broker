"""add receipt column to purchases

Revision ID: 0005_purchase_receipt
Revises: 0004_tokensnapshot_numeric_widen
Create Date: 2025-09-06 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0005_purchase_receipt"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.add_column("purchase", sa.Column("receipt", sa.Text(), nullable=True))
    except Exception:
        # Some backends or states may already have the column; ignore
        pass


def downgrade() -> None:
    try:
        op.drop_column("purchase", "receipt")
    except Exception:
        pass
