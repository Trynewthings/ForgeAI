"""capability lifecycle activation fields

Revision ID: 20260318_0014
Revises: 20260318_0013
Create Date: 2026-03-18 01:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0014"
down_revision = "20260318_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("capability_lifecycle_entries", sa.Column("activation_notes_json", sa.JSON(), nullable=True))
    op.add_column("capability_lifecycle_entries", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("capability_lifecycle_entries", "activated_at")
    op.drop_column("capability_lifecycle_entries", "activation_notes_json")
