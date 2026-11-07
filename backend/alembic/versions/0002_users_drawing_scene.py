"""Users + plan ownership; descriptive drawing JSON and Babylon scene spec per plan.

Revision ID: 0002_users_drawing_scene
Revises: 0001_initial
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_users_drawing_scene"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Existing plans keep owner_id NULL: they become invisible (no account owns them).
    op.add_column(
        "plans",
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
        ),
    )
    op.create_index("ix_plans_owner_id", "plans", ["owner_id"])
    op.add_column("plans", sa.Column("drawing", postgresql.JSONB(), nullable=True))
    op.add_column("plans", sa.Column("scene", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "scene")
    op.drop_column("plans", "drawing")
    op.drop_index("ix_plans_owner_id", table_name="plans")
    op.drop_column("plans", "owner_id")
    op.drop_table("users")
