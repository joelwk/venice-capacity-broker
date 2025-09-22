"""add owner address and key id columns

Revision ID: 0007_add_tenant_owner_keyid
Revises: 0006_purchase_amount_numeric
Create Date: 2025-09-22 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_add_tenant_owner_keyid"
down_revision = "0006_purchase_amount_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.add_column("tenant", sa.Column("owner_address", sa.String(), nullable=True))
    except Exception:
        pass
    try:
        op.add_column("key", sa.Column("key_id", sa.String(), nullable=True))
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_column("key", "key_id")
    except Exception:
        pass
    try:
        op.drop_column("tenant", "owner_address")
    except Exception:
        pass
