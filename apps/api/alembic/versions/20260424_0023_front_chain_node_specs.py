"""front chain node specs columns

Node spec pass: extends ``front_chain_states`` with ``node_specs_json``
and ``node_specs_version`` so the new spec endpoint can persist a
``NodeExecutionSpecBundle`` against the same state lineage that holds
the planner_output. The column is NULLABLE — all prior stages continue
to function without back-fill; spec runs only after /plan.

Revision ID: 20260424_0023
Revises: 20260422_0022
Create Date: 2026-04-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0023"
down_revision = "20260422_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.add_column(sa.Column("node_specs_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("node_specs_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_column("node_specs_version")
        batch_op.drop_column("node_specs_json")
