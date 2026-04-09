"""add requirement clarification state

Revision ID: 20260315_0007
Revises: 20260315_0006
Create Date: 2026-03-15 16:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260315_0007"
down_revision = "20260315_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requirements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("clarification_state_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("requirements", schema=None) as batch_op:
        batch_op.drop_column("clarification_state_json")
