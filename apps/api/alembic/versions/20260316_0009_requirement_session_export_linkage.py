"""add requirement session export linkage

Revision ID: 20260316_0009
Revises: 20260315_0008
Create Date: 2026-03-16 10:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_0009"
down_revision = "20260315_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requirement_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("exported_requirement_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "export_status",
                sa.String(length=50),
                nullable=False,
                server_default="not_exported",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("requirement_sessions", schema=None) as batch_op:
        batch_op.drop_column("export_status")
        batch_op.drop_column("exported_at")
        batch_op.drop_column("exported_requirement_id")
