"""front chain evaluation columns

Phase 3: extends ``front_chain_states`` with columns that carry the
solvability proof and execution admissibility decision produced by the
new evaluate pipeline. All columns are NULLABLE so Phase 1A/1B rows
continue to function without any back-fill.

Revision ID: 20260411_0018
Revises: 20260411_0017
Create Date: 2026-04-11 03:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260411_0018"
down_revision = "20260411_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.add_column(sa.Column("solvability_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("admissibility_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("solvability_llm_trace_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("evaluation_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_column("evaluation_version")
        batch_op.drop_column("solvability_llm_trace_json")
        batch_op.drop_column("admissibility_json")
        batch_op.drop_column("solvability_json")
