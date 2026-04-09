"""capability lifecycle entries

Revision ID: 20260318_0012
Revises: 20260317_0011
Create Date: 2026-03-18 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0012"
down_revision = "20260317_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_lifecycle_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_request_id", sa.String(length=36), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=64), nullable=False),
        sa.Column("capability_type", sa.String(length=64), nullable=False),
        sa.Column("request_kind_origin", sa.String(length=64), nullable=False),
        sa.Column("implementation_route", sa.String(length=128), nullable=False),
        sa.Column("activation_requirements_json", sa.JSON(), nullable=True),
        sa.Column("review_notes_json", sa.JSON(), nullable=True),
        sa.Column("enabled_contexts_json", sa.JSON(), nullable=True),
        sa.Column("supported_step_kinds_json", sa.JSON(), nullable=True),
        sa.Column("linked_static_registry_capability_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_contract_summary", sa.Text(), nullable=False),
        sa.Column("output_contract_summary", sa.Text(), nullable=False),
        sa.Column("runtime_requirements_json", sa.JSON(), nullable=True),
        sa.Column("provider_requirements_json", sa.JSON(), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=False),
        sa.Column("template_backed_only", sa.Boolean(), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_request_id"], ["capability_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_request_id"),
    )
    op.create_index("ix_capability_lifecycle_entries_project_id", "capability_lifecycle_entries", ["project_id"])
    op.create_index("ix_capability_lifecycle_entries_capability_id", "capability_lifecycle_entries", ["capability_id"])
    op.create_index("ix_capability_lifecycle_entries_status", "capability_lifecycle_entries", ["lifecycle_status"])


def downgrade() -> None:
    op.drop_index("ix_capability_lifecycle_entries_status", table_name="capability_lifecycle_entries")
    op.drop_index("ix_capability_lifecycle_entries_capability_id", table_name="capability_lifecycle_entries")
    op.drop_index("ix_capability_lifecycle_entries_project_id", table_name="capability_lifecycle_entries")
    op.drop_table("capability_lifecycle_entries")
