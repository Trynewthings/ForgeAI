from __future__ import annotations

from celery import Celery

from worker_app.config import settings

celery_app = Celery(
    "agentforge_worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["worker_app.tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    worker_concurrency=settings.worker_concurrency,
)

