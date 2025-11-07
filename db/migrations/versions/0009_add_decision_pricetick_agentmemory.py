from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "0009_add_decision_pricetick_agentmemory"
down_revision = "0008_add_dex_pool_tables"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    try:
        insp = Inspector.from_engine(conn)  # type: ignore[arg-type]
        tables = set(insp.get_table_names())
        return name in tables
    except Exception:
        # Best-effort: if inspector fails, assume missing so create attempts will proceed
        return False


def _has_index(conn, table: str, index: str) -> bool:
    try:
        insp = Inspector.from_engine(conn)  # type: ignore[arg-type]
        indexes = insp.get_indexes(table)
        return any(idx.get("name") == index for idx in indexes)
    except Exception:
        # If introspection fails (e.g., missing table), treat as missing index
        return False


def upgrade() -> None:
    conn = op.get_bind()

    # Ensure the alembic version table can store longer revision identifiers
    if _has_table(conn, "alembic_version"):
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            nullable=False,
        )

    # Decision
    if not _has_table(conn, "decision"):
        op.create_table(
            "decision",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("agent", sa.String(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("correlation_id", sa.String(), nullable=True),
            sa.Column("details", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        if not _has_index(conn, "decision", "ix_decision_agent"):
            op.create_index("ix_decision_agent", "decision", ["agent"])  # idempotent in most backends
        if not _has_index(conn, "decision", "ix_decision_created_at"):
            op.create_index("ix_decision_created_at", "decision", ["created_at"])  # ditto

    # PriceTick
    if not _has_table(conn, "pricetick"):
        op.create_table(
            "pricetick",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("symbol", sa.String(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("price_usd", sa.Float(), nullable=False),
        )
        if not _has_index(conn, "pricetick", "ix_pricetick_ts"):
            op.create_index("ix_pricetick_ts", "pricetick", ["ts"])  # index on timestamp

    # AgentMemory
    if not _has_table(conn, "agentmemory"):
        op.create_table(
            "agentmemory",
            sa.Column("id", sa.String(), primary_key=True, nullable=False),
            sa.Column("agent", sa.String(), nullable=False),
            sa.Column("cycle_id", sa.String(), nullable=True),
            sa.Column("decision_id", sa.Integer(), sa.ForeignKey("decision.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
        )
        if not _has_index(conn, "agentmemory", "ix_agentmemory_agent"):
            op.create_index("ix_agentmemory_agent", "agentmemory", ["agent"])  # fast filter by agent
        if not _has_index(conn, "agentmemory", "ix_agentmemory_created_at"):
            op.create_index("ix_agentmemory_created_at", "agentmemory", ["created_at"])  # retention pruning
        op.create_foreign_key(
            "fk_agentmemory_decision_id",
            "agentmemory",
            "decision",
            ["decision_id"],
            ["id"],
            initially=None,
        )


def downgrade() -> None:
    # Safety-first downgrade: drop AgentMemory and PriceTick, keep Decision log unless explicitly removed
    try:
        op.drop_table("agentmemory")
    except Exception:
        pass
    try:
        op.drop_table("pricetick")
    except Exception:
        pass
    # Decision table is intentionally preserved to avoid losing audit history; uncomment to allow dropping
    # try:
    # 	op.drop_table("decision")
    # except Exception:
    # 	pass
