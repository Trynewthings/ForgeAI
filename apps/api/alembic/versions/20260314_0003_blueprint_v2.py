"""blueprint v2 domain models

Revision ID: 20260314_0003
Revises: 20260313_0002
Create Date: 2026-03-14 10:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260314_0003"
down_revision: Union[str, None] = "20260313_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "requirements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("target_users", sa.Text(), nullable=True),
        sa.Column("input_description", sa.Text(), nullable=True),
        sa.Column("output_description", sa.Text(), nullable=True),
        sa.Column("constraints_json", sa.JSON(), nullable=True),
        sa.Column("acceptance_criteria_json", sa.JSON(), nullable=True),
        sa.Column("non_goals_json", sa.JSON(), nullable=True),
        sa.Column("priority_features_json", sa.JSON(), nullable=True),
        sa.Column("raw_conversation_json", sa.JSON(), nullable=True),
        sa.Column("structured_spec_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requirements_project_id", "requirements", ["project_id"], unique=False)

    op.create_table(
        "blueprints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("requirement_id", sa.String(length=36), nullable=True),
        sa.Column("blueprint_type", sa.String(length=50), nullable=False, server_default="single_service"),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=False, server_default="0.1.0"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("input_schema_json", sa.JSON(), nullable=True),
        sa.Column("output_schema_json", sa.JSON(), nullable=True),
        sa.Column("execution_nodes_json", sa.JSON(), nullable=True),
        sa.Column("execution_steps_json", sa.JSON(), nullable=True),
        sa.Column("configurable_vars_json", sa.JSON(), nullable=True),
        sa.Column("execution_env_json", sa.JSON(), nullable=True),
        sa.Column("dependencies_json", sa.JSON(), nullable=True),
        sa.Column("artifacts_spec_json", sa.JSON(), nullable=True),
        sa.Column("observability_spec_json", sa.JSON(), nullable=True),
        sa.Column("retry_policy_json", sa.JSON(), nullable=True),
        sa.Column("trigger_mode", sa.String(length=100), nullable=True),
        sa.Column("runtime_constraints_json", sa.JSON(), nullable=True),
        sa.Column("template_tags_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_blueprints_project_id", "blueprints", ["project_id"], unique=False)

    op.create_table(
        "executables",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("blueprint_id", sa.String(length=36), nullable=False),
        sa.Column("executable_type", sa.String(length=50), nullable=False, server_default="repo"),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("manifest_json", sa.JSON(), nullable=True),
        sa.Column("generated_files_index_json", sa.JSON(), nullable=True),
        sa.Column("build_version", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["blueprint_id"], ["blueprints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executables_project_id", "executables", ["project_id"], unique=False)
    op.create_index("ix_executables_blueprint_id", "executables", ["blueprint_id"], unique=False)

    op.create_table(
        "runtime_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("blueprint_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("model_bindings_json", sa.JSON(), nullable=True),
        sa.Column("skill_bindings_json", sa.JSON(), nullable=True),
        sa.Column("mcp_bindings_json", sa.JSON(), nullable=True),
        sa.Column("env_vars_json", sa.JSON(), nullable=True),
        sa.Column("input_overrides_json", sa.JSON(), nullable=True),
        sa.Column("resource_limits_json", sa.JSON(), nullable=True),
        sa.Column("debug_options_json", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["blueprint_id"], ["blueprints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runtime_profiles_project_id", "runtime_profiles", ["project_id"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("blueprint_id", sa.String(length=36), nullable=False),
        sa.Column("executable_id", sa.String(length=36), nullable=True),
        sa.Column("runtime_profile_id", sa.String(length=36), nullable=True),
        sa.Column("triggered_by", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("resolved_config_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["blueprint_id"], ["blueprints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["executable_id"], ["executables.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["runtime_profile_id"], ["runtime_profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_project_id", "runs", ["project_id"], unique=False)
    op.create_index("ix_runs_blueprint_id", "runs", ["blueprint_id"], unique=False)

    op.create_table(
        "run_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("step_name", sa.String(length=255), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("node_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("input_payload_json", sa.JSON(), nullable=True),
        sa.Column("output_payload_json", sa.JSON(), nullable=True),
        sa.Column("log_excerpt", sa.Text(), nullable=True),
        sa.Column("token_usage_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_steps_run_id", "run_steps", ["run_id"], unique=False)

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("run_step_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("storage_kind", sa.String(length=50), nullable=False, server_default="inline"))
        batch_op.alter_column("build_task_id", existing_type=sa.String(length=36), nullable=True)
        batch_op.create_foreign_key("fk_artifacts_run_id_runs", "runs", ["run_id"], ["id"], ondelete="CASCADE")
        batch_op.create_foreign_key("fk_artifacts_run_step_id_run_steps", "run_steps", ["run_step_id"], ["id"], ondelete="SET NULL")

    with op.batch_alter_table("token_usage_logs") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("run_step_id", sa.String(length=36), nullable=True))
        batch_op.alter_column("build_task_id", existing_type=sa.String(length=36), nullable=True)
        batch_op.create_foreign_key("fk_token_usage_logs_run_id_runs", "runs", ["run_id"], ["id"], ondelete="CASCADE")
        batch_op.create_foreign_key("fk_token_usage_logs_run_step_id_run_steps", "run_steps", ["run_step_id"], ["id"], ondelete="SET NULL")

    with op.batch_alter_table("agent_systems") as batch_op:
        batch_op.add_column(sa.Column("blueprint_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_agent_systems_blueprint_id_blueprints", "blueprints", ["blueprint_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("agent_systems") as batch_op:
        batch_op.drop_constraint("fk_agent_systems_blueprint_id_blueprints", type_="foreignkey")
        batch_op.drop_column("blueprint_id")

    with op.batch_alter_table("token_usage_logs") as batch_op:
        batch_op.drop_constraint("fk_token_usage_logs_run_step_id_run_steps", type_="foreignkey")
        batch_op.drop_constraint("fk_token_usage_logs_run_id_runs", type_="foreignkey")
        batch_op.alter_column("build_task_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.drop_column("run_step_id")
        batch_op.drop_column("run_id")

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint("fk_artifacts_run_step_id_run_steps", type_="foreignkey")
        batch_op.drop_constraint("fk_artifacts_run_id_runs", type_="foreignkey")
        batch_op.alter_column("build_task_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.drop_column("storage_kind")
        batch_op.drop_column("run_step_id")
        batch_op.drop_column("run_id")

    op.drop_index("ix_run_steps_run_id", table_name="run_steps")
    op.drop_table("run_steps")
    op.drop_index("ix_runs_blueprint_id", table_name="runs")
    op.drop_index("ix_runs_project_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_runtime_profiles_project_id", table_name="runtime_profiles")
    op.drop_table("runtime_profiles")
    op.drop_index("ix_executables_blueprint_id", table_name="executables")
    op.drop_index("ix_executables_project_id", table_name="executables")
    op.drop_table("executables")
    op.drop_index("ix_blueprints_project_id", table_name="blueprints")
    op.drop_table("blueprints")
    op.drop_index("ix_requirements_project_id", table_name="requirements")
    op.drop_table("requirements")
