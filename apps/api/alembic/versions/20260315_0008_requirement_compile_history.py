"""add requirement compile history

Revision ID: 20260315_0008
Revises: 20260315_0007
Create Date: 2026-03-15 18:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260315_0008"
down_revision = "20260315_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requirement_compile_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.Column("compiler_mode", sa.String(length=50), nullable=False),
        sa.Column("clarifications_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("clarification_freshness_status", sa.String(length=50), nullable=False),
        sa.Column("selected_family", sa.String(length=50), nullable=False),
        sa.Column("completion_state", sa.String(length=50), nullable=False),
        sa.Column("draft_readiness_status", sa.String(length=50), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("draft_strategy_action", sa.String(length=100), nullable=False),
        sa.Column("warnings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gaps_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("family_changed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("delta_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_requirement_compile_records_requirement_id",
        "requirement_compile_records",
        ["requirement_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_requirement_compile_records_requirement_id", table_name="requirement_compile_records")
    op.drop_table("requirement_compile_records")
