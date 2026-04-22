"""build session anchor column + relax blueprint_id

Adds ``session_id`` column on ``builds`` and relaxes ``blueprint_id`` to
nullable so the session-anchored C-slice entry (FrontChainState-backed,
no Blueprint) can create builds. Exactly one of (blueprint_id,
session_id) must be non-null at create time; this is enforced at the
application layer in ``BuildService.create_build``.

Revision ID: 20260422_0022
Revises: 20260421_0021
Create Date: 2026-04-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260422_0022"
down_revision = "20260421_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("builds") as batch_op:
        batch_op.alter_column("blueprint_id", existing_type=sa.String(length=36), nullable=True)
        batch_op.add_column(sa.Column("session_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_builds_session_id_front_chain_sessions",
            "front_chain_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_builds_session_id", ["session_id"])
    with op.batch_alter_table("executables") as batch_op:
        batch_op.alter_column("blueprint_id", existing_type=sa.String(length=36), nullable=True)
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column("blueprint_id", existing_type=sa.String(length=36), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column("blueprint_id", existing_type=sa.String(length=36), nullable=False)
    with op.batch_alter_table("executables") as batch_op:
        batch_op.alter_column("blueprint_id", existing_type=sa.String(length=36), nullable=False)
    with op.batch_alter_table("builds") as batch_op:
        batch_op.drop_index("ix_builds_session_id")
        batch_op.drop_constraint("fk_builds_session_id_front_chain_sessions", type_="foreignkey")
        batch_op.drop_column("session_id")
        batch_op.alter_column("blueprint_id", existing_type=sa.String(length=36), nullable=False)
