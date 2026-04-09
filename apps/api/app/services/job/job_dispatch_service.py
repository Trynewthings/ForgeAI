from __future__ import annotations

import logging
from threading import Thread

from celery import Celery

from app.core.config import settings
from app.core.logging import log_event
from app.services.job.job_execution_service import JobExecutionService

logger = logging.getLogger(__name__)

celery_client = Celery(
    "agentforge_api_jobs",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)


class JobDispatchService:
    def dispatch(self, job_id: str) -> tuple[str, str | None]:
        mode = settings.build_execution_mode.lower()
        if mode in {"auto", "celery"}:
            try:
                async_result = celery_client.send_task("agentforge.job.execute", args=[job_id])
                log_event(logger, logging.INFO, "job.dispatch.sent_to_celery", job_id=job_id, celery_task_id=async_result.id)
                return "celery", async_result.id
            except Exception as exc:
                log_event(logger, logging.WARNING, "job.dispatch.celery_failed", job_id=job_id, error=str(exc))
                if mode == "celery":
                    raise

        thread = Thread(target=JobExecutionService().execute_job, args=(job_id,), daemon=True)
        thread.start()
        log_event(logger, logging.INFO, "job.dispatch.sent_to_thread", job_id=job_id)
        return "thread", None
