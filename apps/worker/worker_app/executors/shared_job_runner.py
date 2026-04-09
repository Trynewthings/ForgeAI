from __future__ import annotations

from worker_app.bootstrap import ensure_api_path

ensure_api_path()

from app.services.job.job_execution_service import JobExecutionService


def execute_job(job_id: str) -> None:
    JobExecutionService().execute_job(job_id)
