"""add dex pool watcher tables

Revision ID: 0008_add_dex_pool_tables
Revises: 0007_add_tenant_owner_keyid
Create Date: 2025-09-29 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_add_dex_pool_tables"
down_revision = "0007_add_tenant_owner_keyid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dexfactorycursor",
        sa.Column("factory_address", sa.String(length=66), primary_key=True, nullable=False),
        sa.Column("factory_type", sa.String(length=64), nullable=False, server_default="uniswap_v2"),
        sa.Column("chain_id", sa.Integer(), nullable=True),
        sa.Column("last_block", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "dexpool",
        sa.Column("pool_address", sa.String(length=66), primary_key=True, nullable=False),
        sa.Column("factory_address", sa.String(length=66), nullable=False),
        sa.Column("factory_type", sa.String(length=64), nullable=False, server_default="uniswap_v2"),
        sa.Column("chain_id", sa.Integer(), nullable=True),
        sa.Column("token0", sa.String(length=66), nullable=False),
        sa.Column("token1", sa.String(length=66), nullable=False),
        sa.Column("fee", sa.Integer(), nullable=True),
        sa.Column("stable", sa.Boolean(), nullable=True),
        sa.Column("tick_spacing", sa.Integer(), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("tx_hash", sa.String(length=66), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserve0", sa.Numeric(78, 0), nullable=True),
        sa.Column("reserve1", sa.Numeric(78, 0), nullable=True),
        sa.Column("liquidity", sa.Numeric(78, 0), nullable=True),
    )

    op.create_index("ix_dexfactorycursor_factory_type", "dexfactorycursor", ["factory_type"])

    op.create_index("ix_dexpool_factory_address", "dexpool", ["factory_address"])
    op.create_index("ix_dexpool_factory_type", "dexpool", ["factory_type"])
    op.create_index("ix_dexpool_chain_id", "dexpool", ["chain_id"])
    op.create_index("ix_dexpool_token0", "dexpool", ["token0"])
    op.create_index("ix_dexpool_token1", "dexpool", ["token1"])
    op.create_index("ix_dexpool_tx_hash", "dexpool", ["tx_hash"])


def downgrade() -> None:
    op.drop_index("ix_dexpool_tx_hash", table_name="dexpool")
    op.drop_index("ix_dexpool_token1", table_name="dexpool")
    op.drop_index("ix_dexpool_token0", table_name="dexpool")
    op.drop_index("ix_dexpool_chain_id", table_name="dexpool")
    op.drop_index("ix_dexpool_factory_type", table_name="dexpool")
    op.drop_index("ix_dexpool_factory_address", table_name="dexpool")
    op.drop_index("ix_dexfactorycursor_factory_type", table_name="dexfactorycursor")

    op.drop_table("dexpool")
    op.drop_table("dexfactorycursor")
