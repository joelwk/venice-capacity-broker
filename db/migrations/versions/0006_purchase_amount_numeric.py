"""widen purchase amount_paid to numeric

Revision ID: 0006_purchase_amount_numeric
Revises: 0005_purchase_receipt
Create Date: 2025-09-21 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_purchase_amount_numeric"
down_revision = "0005_purchase_receipt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        with op.batch_alter_table("purchase") as batch:
            batch.alter_column("amount_paid", type_=sa.Numeric(78, 0))
    except Exception:
        pass


def downgrade() -> None:
    try:
        with op.batch_alter_table("purchase") as batch:
            batch.alter_column("amount_paid", type_=sa.Integer())
    except Exception:
        pass
