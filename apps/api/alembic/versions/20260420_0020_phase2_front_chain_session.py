"""Phase 2: front-chain takes over session main path.

Creates ``front_chain_sessions`` (the new lightweight session container),
repoints ``front_chain_states.session_id`` FK onto it, and hard-drops the
legacy ``requirement_messages`` + ``requirement_sessions`` tables.

Revision ID: 20260420_0020
Revises: 20260413_0019
Create Date: 2026-04-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260420_0020"
down_revision = "20260413_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create the new front_chain_sessions table.
    op.create_table(
        "front_chain_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
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
    )

    # 2. Repoint front_chain_states.session_id FK from requirement_sessions
    #    to front_chain_sessions. Since there's no data yet (pre-launch), we
    #    just drop + recreate via batch.
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_constraint(
            "fk_front_chain_states_session_id_requirement_sessions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_front_chain_states_session_id_front_chain_sessions",
            "front_chain_sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 3. Hard-drop the legacy tables. First release any FK constraints
    #    pointing at requirement_sessions from other tables (capability_requests
    #    has such a column; Phase 2 keeps the column as a plain string).
    with op.batch_alter_table("capability_requests") as batch_op:
        batch_op.drop_constraint(
            "fk_capability_requests_source_session_id_requirement_sessions",
            type_="foreignkey",
        )
    op.drop_table("requirement_messages")
    op.drop_table("requirement_sessions")


def downgrade() -> None:
    # Re-create legacy tables (no data restore).
    op.create_table(
        "requirement_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("current_stage", sa.String(length=100), nullable=False),
        sa.Column("raw_user_input", sa.Text(), nullable=True),
        sa.Column("requirement_draft_json", sa.JSON(), nullable=True),
        sa.Column("markdown_document", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("exported_requirement_id", sa.String(length=36), nullable=True),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("export_status", sa.String(length=50), nullable=False),
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
    )
    op.create_table(
        "requirement_messages",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "requirement_session_id",
            sa.String(length=36),
            sa.ForeignKey("requirement_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_snapshot_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_constraint(
            "fk_front_chain_states_session_id_front_chain_sessions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_front_chain_states_session_id_requirement_sessions",
            "requirement_sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.drop_table("front_chain_sessions")
