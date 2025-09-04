from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # AssetToken table
    op.create_table(
        "assettoken",
        sa.Column("address", sa.String(), primary_key=True, nullable=False),
        sa.Column("chain", sa.String(), nullable=False, server_default="base"),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("decimals", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # TokenSnapshot table
    op.create_table(
        "tokensnapshot",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("token_address", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=True),
        sa.Column("supply_total", sa.BigInteger(), nullable=True),
        sa.Column("supply_circulating", sa.BigInteger(), nullable=True),
        sa.Column("holders", sa.Integer(), nullable=True),
        sa.Column("transfers_24h", sa.Integer(), nullable=True),
        sa.Column("marketcap_usd", sa.Float(), nullable=True),
        sa.Column("max_total_supply", sa.BigInteger(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["token_address"], ["assettoken.address"], name="fk_tokensnapshot_token_address_assettoken"),
    )
    # Helpful indexes
    op.create_index("ix_tokensnapshot_ts", "tokensnapshot", ["ts"])  # type: ignore[arg-type]
    op.create_index("ix_tokensnapshot_token_ts", "tokensnapshot", ["token_address", "ts"])  # type: ignore[arg-type]


def downgrade() -> None:
    op.drop_index("ix_tokensnapshot_token_ts", table_name="tokensnapshot")
    op.drop_index("ix_tokensnapshot_ts", table_name="tokensnapshot")
    op.drop_table("tokensnapshot")
    op.drop_table("assettoken")

