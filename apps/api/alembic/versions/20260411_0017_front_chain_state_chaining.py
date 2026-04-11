"""front chain state chaining

Phase 1B: extends ``front_chain_states`` with the columns required for
selective per-turn updates after a user answers a clarification question.

  * ``parent_state_id`` — FK back to the prior state in the same chain
    (NULL for turn_number=1 rows produced by /interpret).
  * ``turn_number``     — 1 for the initial interpret, +1 per
    /states/{id}/answer call.

This is intentionally additive — Phase 1A rows continue to function with
turn_number defaulting to 1 and parent_state_id NULL.

Revision ID: 20260411_0017
Revises: 20260411_0016
Create Date: 2026-04-11 02:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260411_0017"
down_revision = "20260411_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.add_column(
            sa.Column("parent_state_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("turn_number", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_foreign_key(
            "fk_front_chain_states_parent_state_id",
            "front_chain_states",
            ["parent_state_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_front_chain_states_parent_state_id",
            ["parent_state_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_index("ix_front_chain_states_parent_state_id")
        batch_op.drop_constraint(
            "fk_front_chain_states_parent_state_id", type_="foreignkey"
        )
        batch_op.drop_column("turn_number")
        batch_op.drop_column("parent_state_id")
