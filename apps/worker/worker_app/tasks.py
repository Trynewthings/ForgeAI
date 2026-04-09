from __future__ import annotations

import logging

from worker_app.bootstrap import ensure_api_path
from worker_app.celery_app import celery_app
from worker_app.executors.shared_job_runner import execute_job

ensure_api_path()

from app.core.logging import configure_logging, log_event

configure_logging()
logger = logging.getLogger(__name__)


@celery_app.task(name="agentforge.ping")
def ping() -> dict[str, str]:
    return {"status": "ok", "service": "worker"}


@celery_app.task(name="agentforge.job.execute", bind=True)
def execute_job_task(self, job_id: str) -> dict[str, str]:
    try:
        log_event(logger, logging.INFO, "worker.job.started", job_id=job_id)
        execute_job(job_id)
        log_event(logger, logging.INFO, "worker.job.completed", job_id=job_id)
        return {"job_id": job_id, "status": "completed"}
    except Exception as exc:
        log_event(logger, logging.ERROR, "worker.job.failed", job_id=job_id, error=str(exc))
        raise
