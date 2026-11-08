"""Background processing stage, the layer roles used, and the automatic checks' report per plan.

Revision ID: 0003_processing_checks
Revises: 0002_users_drawing_scene
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_processing_checks"
down_revision: str | None = "0002_users_drawing_scene"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("stage", sa.String(32), nullable=True))
    op.add_column("plans", sa.Column("parse_options", postgresql.JSONB(), nullable=True))
    op.add_column("plans", sa.Column("report", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "report")
    op.drop_column("plans", "parse_options")
    op.drop_column("plans", "stage")
