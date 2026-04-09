"""add requirement studio compiler fields

Revision ID: 20260315_0006
Revises: 20260314_0005
Create Date: 2026-03-15 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260315_0006"
down_revision = "20260314_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requirements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("domain", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("interactivity_level", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("human_review_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))
        batch_op.add_column(sa.Column("recurrence_mode", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("external_tools_needed_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("compiler_status", sa.String(length=50), server_default=sa.text("'idle'"), nullable=False))
        batch_op.add_column(sa.Column("compiler_notes_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("compiler_open_questions_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("compiler_result_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("requirements", schema=None) as batch_op:
        batch_op.drop_column("compiler_result_json")
        batch_op.drop_column("compiler_open_questions_json")
        batch_op.drop_column("compiler_notes_json")
        batch_op.drop_column("compiler_status")
        batch_op.drop_column("external_tools_needed_json")
        batch_op.drop_column("recurrence_mode")
        batch_op.drop_column("human_review_required")
        batch_op.drop_column("interactivity_level")
        batch_op.drop_column("domain")

