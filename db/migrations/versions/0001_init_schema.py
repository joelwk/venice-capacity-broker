from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "key",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("subkey", sa.String(), nullable=False),
        sa.Column("quota", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name="fk_key_tenant_id_tenant"),
    )

    op.create_table(
        "plan",
        sa.Column("name", sa.String(), primary_key=True, nullable=False),
        sa.Column("monthly_quota", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rps", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("burst", sa.Integer(), nullable=False, server_default="60"),
    )


def downgrade() -> None:
    op.drop_table("plan")
    op.drop_table("key")
    op.drop_table("tenant")

