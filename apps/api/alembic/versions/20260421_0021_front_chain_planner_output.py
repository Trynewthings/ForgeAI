"""front chain planner output column

Planning stage: extends ``front_chain_states`` with ``planner_output_json``
and ``planner_version`` so that the new build-planning endpoint can persist
the latest WorkflowPlannerOutput against the same state lineage that holds
the compile authority. The column is NULLABLE so states produced by
interpret / answer / evaluate / compile continue to function without any
back-fill.

Revision ID: 20260421_0021
Revises: 20260420_0020
Create Date: 2026-04-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260421_0021"
down_revision = "20260420_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.add_column(sa.Column("planner_output_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("planner_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_column("planner_version")
        batch_op.drop_column("planner_output_json")
