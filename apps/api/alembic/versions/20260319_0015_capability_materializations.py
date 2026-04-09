"""add capability materializations

Revision ID: 20260319_0015
Revises: 20260318_0014
Create Date: 2026-03-19 10:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260319_0015"
down_revision = "20260318_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_materializations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_request_id", sa.String(length=36), nullable=False),
        sa.Column("source_lifecycle_entry_id", sa.String(length=36), nullable=False),
        sa.Column("source_proposal_id", sa.String(length=36), nullable=False),
        sa.Column("materialization_kind", sa.String(length=64), nullable=False),
        sa.Column("materialization_status", sa.String(length=64), nullable=False),
        sa.Column("resulting_handler_ref", sa.String(length=255), nullable=True),
        sa.Column("resulting_execution_kind", sa.String(length=128), nullable=True),
        sa.Column("resulting_runtime_adapter", sa.String(length=128), nullable=True),
        sa.Column("materialized_asset_refs_json", sa.JSON(), nullable=True),
        sa.Column("runtime_constraints_json", sa.JSON(), nullable=True),
        sa.Column("review_notes_json", sa.JSON(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_request_id"], ["capability_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_lifecycle_entry_id"], ["capability_lifecycle_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_proposal_id"], ["capability_proposals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_capability_materializations_project_id"), "capability_materializations", ["project_id"], unique=False)
    op.create_index(op.f("ix_capability_materializations_source_request_id"), "capability_materializations", ["source_request_id"], unique=False)
    op.create_index(
        op.f("ix_capability_materializations_source_lifecycle_entry_id"),
        "capability_materializations",
        ["source_lifecycle_entry_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_capability_materializations_source_proposal_id"),
        "capability_materializations",
        ["source_proposal_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_capability_materializations_source_proposal_id"), table_name="capability_materializations")
    op.drop_index(op.f("ix_capability_materializations_source_lifecycle_entry_id"), table_name="capability_materializations")
    op.drop_index(op.f("ix_capability_materializations_source_request_id"), table_name="capability_materializations")
    op.drop_index(op.f("ix_capability_materializations_project_id"), table_name="capability_materializations")
    op.drop_table("capability_materializations")
