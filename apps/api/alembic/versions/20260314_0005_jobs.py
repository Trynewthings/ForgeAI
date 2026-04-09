"""add jobs and owner job links

Revision ID: 20260314_0005
Revises: 20260314_0004
Create Date: 2026-03-14 14:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260314_0005"
down_revision = "20260314_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=50), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_project_id"), "jobs", ["project_id"], unique=False)
    op.create_index(op.f("ix_jobs_job_type"), "jobs", ["job_type"], unique=False)
    op.create_index(op.f("ix_jobs_owner_id"), "jobs", ["owner_id"], unique=False)
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_index(op.f("ix_jobs_celery_task_id"), "jobs", ["celery_task_id"], unique=False)

    with op.batch_alter_table("builds", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active_job_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_builds_active_job_id_jobs", "jobs", ["active_job_id"], ["id"], ondelete="SET NULL")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active_job_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_runs_active_job_id_jobs", "jobs", ["active_job_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_runs_active_job_id_jobs", type_="foreignkey")
        batch_op.drop_column("active_job_id")

    with op.batch_alter_table("builds", schema=None) as batch_op:
        batch_op.drop_constraint("fk_builds_active_job_id_jobs", type_="foreignkey")
        batch_op.drop_column("active_job_id")

    op.drop_index(op.f("ix_jobs_celery_task_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_owner_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_job_type"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_project_id"), table_name="jobs")
    op.drop_table("jobs")
