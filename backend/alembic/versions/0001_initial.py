"""Create plans and plan_edits tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("source_format", sa.String(8), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("original_file", sa.LargeBinary(), nullable=False),
        sa.Column("dxf_file", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", postgresql.JSONB(), nullable=True),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("parse_warnings", postgresql.JSONB(), nullable=False),
        sa.Column("parse_stats", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_plans_created_at", "plans", ["created_at"])
    op.create_table(
        "plan_edits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("previous_model", postgresql.JSONB(), nullable=False),
        sa.Column("affected_ids", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_plan_edits_plan_id", "plan_edits", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_edits_plan_id", table_name="plan_edits")
    op.drop_table("plan_edits")
    op.drop_index("ix_plans_created_at", table_name="plans")
    op.drop_table("plans")
