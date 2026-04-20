"""front chain compile context column

Phase 4: extends ``front_chain_states`` with ``compile_context_json`` that
carries the assembled CompileContextResponse produced by the deterministic
compile step.  The column is NULLABLE so Phase 1A/1B/3 rows continue to
function without any back-fill.

Revision ID: 20260413_0019
Revises: 20260411_0018
Create Date: 2026-04-13 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0019"
down_revision = "20260411_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.add_column(sa.Column("compile_context_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("compile_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("front_chain_states") as batch_op:
        batch_op.drop_column("compile_version")
        batch_op.drop_column("compile_context_json")
