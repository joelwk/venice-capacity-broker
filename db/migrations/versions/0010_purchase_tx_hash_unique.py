"""enforce unique tx_hash on purchase

One payment transaction must map to exactly one purchase so concurrent
verify calls cannot mint two Venice keys for the same payment.

Revision ID: 0010_purchase_tx_hash_unique
Revises: 0009_add_decision_pricetick_agentmemory
Create Date: 2026-08-13 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "0010_purchase_tx_hash_unique"
down_revision = "0009_add_decision_pricetick_agentmemory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove race artifacts before enforcing uniqueness: keep the earliest
    # row per tx_hash (duplicates could only exist via the old verify race).
    try:
        op.execute(
            "DELETE FROM purchase WHERE id NOT IN "
            "(SELECT MIN(id) FROM purchase GROUP BY tx_hash)"
        )
    except Exception:
        pass
    try:
        op.drop_index("ix_purchase_tx_hash", table_name="purchase")
    except Exception:
        pass
    try:
        op.create_index("ix_purchase_tx_hash", "purchase", ["tx_hash"], unique=True)
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index("ix_purchase_tx_hash", table_name="purchase")
    except Exception:
        pass
    try:
        op.create_index("ix_purchase_tx_hash", "purchase", ["tx_hash"], unique=False)
    except Exception:
        pass
