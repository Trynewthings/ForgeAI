"""capability proposals

Revision ID: 20260318_0013
Revises: 20260318_0012
Create Date: 2026-03-18 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0013"
down_revision = "20260318_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_request_id", sa.String(length=36), nullable=False),
        sa.Column("source_lifecycle_entry_id", sa.String(length=36), nullable=True),
        sa.Column("proposal_kind", sa.String(length=64), nullable=False),
        sa.Column("proposal_status", sa.String(length=64), nullable=False),
        sa.Column("request_kind_origin", sa.String(length=64), nullable=False),
        sa.Column("capability_id_candidate", sa.String(length=128), nullable=False),
        sa.Column("logical_step_kind", sa.String(length=64), nullable=True),
        sa.Column("capability_purpose", sa.Text(), nullable=False),
        sa.Column("implementation_route", sa.String(length=128), nullable=False),
        sa.Column("io_contract_summary", sa.Text(), nullable=False),
        sa.Column("configuration_contract_summary", sa.Text(), nullable=False),
        sa.Column("review_notes_json", sa.JSON(), nullable=True),
        sa.Column("test_suggestions_json", sa.JSON(), nullable=True),
        sa.Column("capability_metadata_draft_json", sa.JSON(), nullable=True),
        sa.Column("code_stub", sa.Text(), nullable=True),
        sa.Column("template_stub", sa.Text(), nullable=True),
        sa.Column("bounded_execution_kind", sa.String(length=128), nullable=True),
        sa.Column("requires_human_review", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_request_id"], ["capability_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_lifecycle_entry_id"], ["capability_lifecycle_entries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_request_id"),
        sa.UniqueConstraint("source_lifecycle_entry_id"),
    )
    op.create_index("ix_capability_proposals_project_id", "capability_proposals", ["project_id"])
    op.create_index("ix_capability_proposals_status", "capability_proposals", ["proposal_status"])
    op.create_index("ix_capability_proposals_capability_id_candidate", "capability_proposals", ["capability_id_candidate"])


def downgrade() -> None:
    op.drop_index("ix_capability_proposals_capability_id_candidate", table_name="capability_proposals")
    op.drop_index("ix_capability_proposals_status", table_name="capability_proposals")
    op.drop_index("ix_capability_proposals_project_id", table_name="capability_proposals")
    op.drop_table("capability_proposals")
