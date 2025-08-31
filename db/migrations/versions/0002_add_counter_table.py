from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "counter",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="chat"),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("bucket_start", sa.DateTime(), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name="fk_counter_tenant_id_tenant"),
    )
    # Optional: index for efficient queries by tenant/time
    op.create_index("ix_counter_tenant_bucket", "counter", ["tenant_id", "bucket_start"])  # type: ignore[arg-type]


def downgrade() -> None:
    op.drop_index("ix_counter_tenant_bucket", table_name="counter")
    op.drop_table("counter")

