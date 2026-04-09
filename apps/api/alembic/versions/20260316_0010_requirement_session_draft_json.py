"""rename requirement session draft column

Revision ID: 20260316_0010
Revises: 20260316_0009
Create Date: 2026-03-16 22:30:00
"""

from __future__ import annotations

from alembic import op


revision = "20260316_0010"
down_revision = "20260316_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requirement_sessions", schema=None) as batch_op:
        batch_op.alter_column("structured_requirement_json", new_column_name="requirement_draft_json")


def downgrade() -> None:
    with op.batch_alter_table("requirement_sessions", schema=None) as batch_op:
        batch_op.alter_column("requirement_draft_json", new_column_name="structured_requirement_json")
