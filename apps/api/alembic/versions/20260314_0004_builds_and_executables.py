"""build and executable v2 generation

Revision ID: 20260314_0004
Revises: 20260314_0003
Create Date: 2026-03-14 15:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260314_0004"
down_revision: Union[str, None] = "20260314_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "builds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("blueprint_id", sa.String(length=36), nullable=False),
        sa.Column("executable_id", sa.String(length=36), nullable=True),
        sa.Column("build_version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("build_options_json", sa.JSON(), nullable=True),
        sa.Column("manifest_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["blueprint_id"], ["blueprints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["executable_id"], ["executables.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_builds_project_id", "builds", ["project_id"], unique=False)
    op.create_index("ix_builds_blueprint_id", "builds", ["blueprint_id"], unique=False)

    op.create_table(
        "build_steps_v2",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("build_id", sa.String(length=36), nullable=False),
        sa.Column("step_name", sa.String(length=255), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("input_payload_json", sa.JSON(), nullable=True),
        sa.Column("output_payload_json", sa.JSON(), nullable=True),
        sa.Column("log_excerpt", sa.Text(), nullable=True),
        sa.Column("token_usage_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["build_id"], ["builds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_build_steps_v2_build_id", "build_steps_v2", ["build_id"], unique=False)

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("build_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("build_step_v2_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("executable_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_artifacts_build_id_builds", "builds", ["build_id"], ["id"], ondelete="CASCADE")
        batch_op.create_foreign_key(
            "fk_artifacts_build_step_v2_id_build_steps_v2",
            "build_steps_v2",
            ["build_step_v2_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key("fk_artifacts_executable_id_executables", "executables", ["executable_id"], ["id"], ondelete="CASCADE")

    with op.batch_alter_table("token_usage_logs") as batch_op:
        batch_op.add_column(sa.Column("build_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("build_step_v2_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_token_usage_logs_build_id_builds", "builds", ["build_id"], ["id"], ondelete="CASCADE")
        batch_op.create_foreign_key(
            "fk_token_usage_logs_build_step_v2_id_build_steps_v2",
            "build_steps_v2",
            ["build_step_v2_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("token_usage_logs") as batch_op:
        batch_op.drop_constraint("fk_token_usage_logs_build_step_v2_id_build_steps_v2", type_="foreignkey")
        batch_op.drop_constraint("fk_token_usage_logs_build_id_builds", type_="foreignkey")
        batch_op.drop_column("build_step_v2_id")
        batch_op.drop_column("build_id")

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint("fk_artifacts_executable_id_executables", type_="foreignkey")
        batch_op.drop_constraint("fk_artifacts_build_step_v2_id_build_steps_v2", type_="foreignkey")
        batch_op.drop_constraint("fk_artifacts_build_id_builds", type_="foreignkey")
        batch_op.drop_column("executable_id")
        batch_op.drop_column("build_step_v2_id")
        batch_op.drop_column("build_id")

    op.drop_index("ix_build_steps_v2_build_id", table_name="build_steps_v2")
    op.drop_table("build_steps_v2")
    op.drop_index("ix_builds_blueprint_id", table_name="builds")
    op.drop_index("ix_builds_project_id", table_name="builds")
    op.drop_table("builds")
