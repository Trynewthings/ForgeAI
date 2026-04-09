"""add capability requests

Revision ID: 20260317_0011
Revises: 20260316_0010
Create Date: 2026-03-17 13:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260317_0011"
down_revision = "20260316_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_gap_id", sa.String(length=255), nullable=False),
        sa.Column("source_stage", sa.String(length=64), nullable=False),
        sa.Column("source_requirement_id", sa.String(length=36), nullable=True),
        sa.Column("source_compile_record_id", sa.String(length=36), nullable=True),
        sa.Column("source_session_id", sa.String(length=36), nullable=True),
        sa.Column("source_blueprint_id", sa.String(length=36), nullable=True),
        sa.Column("source_build_id", sa.String(length=36), nullable=True),
        sa.Column("request_kind", sa.String(length=64), nullable=False),
        sa.Column("requested_logical_step_kind", sa.String(length=64), nullable=True),
        sa.Column("requested_capability_purpose", sa.Text(), nullable=False),
        sa.Column("requested_capability_name_or_label", sa.String(length=255), nullable=True),
        sa.Column("suggested_alternatives_json", sa.JSON(), nullable=True),
        sa.Column("supporting_gap_summary_json", sa.JSON(), nullable=True),
        sa.Column("example_context_json", sa.JSON(), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(length=64), nullable=False),
        sa.Column("review_notes_json", sa.JSON(), nullable=True),
        sa.Column("implementation_route_hint", sa.String(length=128), nullable=False),
        sa.Column("requires_human_owner", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_requirement_id"], ["requirements.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_compile_record_id"], ["requirement_compile_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_session_id"], ["requirement_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_blueprint_id"], ["blueprints.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_build_id"], ["builds.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_capability_requests_project_id"), "capability_requests", ["project_id"], unique=False)
    op.create_index(op.f("ix_capability_requests_source_gap_id"), "capability_requests", ["source_gap_id"], unique=False)
    op.create_index(op.f("ix_capability_requests_source_stage"), "capability_requests", ["source_stage"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_capability_requests_source_stage"), table_name="capability_requests")
    op.drop_index(op.f("ix_capability_requests_source_gap_id"), table_name="capability_requests")
    op.drop_index(op.f("ix_capability_requests_project_id"), table_name="capability_requests")
    op.drop_table("capability_requests")
