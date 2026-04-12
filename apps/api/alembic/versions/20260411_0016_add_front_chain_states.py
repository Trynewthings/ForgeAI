"""add front chain states

Phase 1A: creates the front_chain_states table as the sole persistence home
for the new front-chain authority island. Legacy requirement JSON fields
stay untouched and are demoted to compatibility-only snapshots.

Revision ID: 20260411_0016
Revises: 20260319_0015
Create Date: 2026-04-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260411_0016"
down_revision = "20260319_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "front_chain_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("requirement_id", sa.String(length=36), nullable=True),
        # server_default uses sa.false() so SQLAlchemy renders the correct
        # dialect-specific literal (FALSE on PostgreSQL, 0 on SQLite).
        sa.Column("preview_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_intent", sa.Text(), nullable=False),
        sa.Column("problem_formulation_json", sa.JSON(), nullable=False),
        sa.Column("input_closure_json", sa.JSON(), nullable=False),
        sa.Column("output_closure_json", sa.JSON(), nullable=False),
        sa.Column("merged_gaps_json", sa.JSON(), nullable=False),
        sa.Column("top_questions_json", sa.JSON(), nullable=False),
        sa.Column("llm_trace_json", sa.JSON(), nullable=False),
        sa.Column("interpreter_version", sa.String(length=64), nullable=False),
        sa.Column("validator_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["requirement_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Dialect-neutral boolean CHECK — ``preview_mode`` is already a
        # boolean column so ``preview_mode OR ...`` evaluates correctly on
        # both PostgreSQL and SQLite. An earlier version used
        # ``preview_mode = 1`` which failed on PostgreSQL (boolean != integer).
        sa.CheckConstraint(
            "preview_mode OR session_id IS NOT NULL OR requirement_id IS NOT NULL",
            name="front_chain_states_binding_required",
        ),
    )
    op.create_index(
        op.f("ix_front_chain_states_project_id"),
        "front_chain_states",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_front_chain_states_session_id"),
        "front_chain_states",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_front_chain_states_requirement_id"),
        "front_chain_states",
        ["requirement_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_front_chain_states_requirement_id"), table_name="front_chain_states")
    op.drop_index(op.f("ix_front_chain_states_session_id"), table_name="front_chain_states")
    op.drop_index(op.f("ix_front_chain_states_project_id"), table_name="front_chain_states")
    op.drop_table("front_chain_states")
