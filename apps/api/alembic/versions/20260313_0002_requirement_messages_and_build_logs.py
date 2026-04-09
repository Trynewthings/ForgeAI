"""requirement messages and build logs

Revision ID: 20260313_0002
Revises: 20260313_0001
Create Date: 2026-03-13 15:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260313_0002"
down_revision: Union[str, None] = "20260313_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "requirement_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("requirement_session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="user"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["requirement_session_id"], ["requirement_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("build_steps", sa.Column("log_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("build_steps", "log_text")
    op.drop_table("requirement_messages")
