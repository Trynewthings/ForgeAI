from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.core.enums import BuildStatus, JobStatus, JobType, RunStatus
from app.core.logging import log_event
from app.db.repositories.build_repository import BuildRepository
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.run_repository import RunRepository
from app.services.build.build_execution_service import BuildExecutionService
from app.services.run.run_execution_service import RunExecutionService

logger = logging.getLogger(__name__)


class JobExecutionService:
    def execute_job(self, job_id: str) -> None:
        db = SessionLocal()
        job_repository = JobRepository(db)
        build_repository = BuildRepository(db)
        run_repository = RunRepository(db)

        try:
            job = job_repository.get_by_id(job_id)
            if job is None:
                raise RuntimeError("Job not found")

            job.status = JobStatus.STARTED.value
            job.started_at = datetime.now(timezone.utc)
            job.error_message = None
            db.commit()
            log_event(logger, logging.INFO, "job.started", job_id=job.id, job_type=job.job_type, owner_id=job.owner_id)

            if job.job_type == JobType.BUILD.value:
                BuildExecutionService().execute_build(job.owner_id)
                owner = build_repository.get_by_id(job.owner_id)
                owner_status = owner.status if owner is not None else BuildStatus.FAILED.value
                if owner is None or owner_status == BuildStatus.FAILED.value:
                    raise RuntimeError(owner.error_message if owner and owner.error_message else "Build execution failed")
                job.result_json = {
                    "owner_status": owner.status,
                    "build_id": owner.id,
                    "executable_id": owner.executable_id,
                    "summary": owner.summary,
                }
            elif job.job_type == JobType.RUN.value:
                RunExecutionService().execute_run(job.owner_id)
                owner = run_repository.get_by_id(job.owner_id)
                owner_status = owner.status if owner is not None else RunStatus.FAILED.value
                if owner is None or owner_status == RunStatus.FAILED.value:
                    raise RuntimeError(owner.error_message if owner and owner.error_message else "Run execution failed")
                job.result_json = {
                    "owner_status": owner.status,
                    "run_id": owner.id,
                    "summary": owner.summary,
                    "execution_mode": (owner.resolved_config_snapshot_json or {}).get("execution_mode"),
                }
            else:
                raise RuntimeError(f"Unsupported job type: {job.job_type}")

            job.status = JobStatus.COMPLETED.value
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            log_event(logger, logging.INFO, "job.completed", job_id=job.id, job_type=job.job_type, owner_id=job.owner_id)
        except Exception as exc:
            db.rollback()
            job = job_repository.get_by_id(job_id)
            if job is not None:
                if job.job_type == JobType.BUILD.value:
                    owner = build_repository.get_by_id(job.owner_id)
                    if owner is not None and owner.status not in {BuildStatus.COMPLETED.value, BuildStatus.FAILED.value}:
                        owner.status = BuildStatus.FAILED.value
                        owner.error_message = str(exc)
                        owner.ended_at = datetime.now(timezone.utc)
                elif job.job_type == JobType.RUN.value:
                    owner = run_repository.get_by_id(job.owner_id)
                    if owner is not None and owner.status not in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
                        owner.status = RunStatus.FAILED.value
                        owner.error_message = str(exc)
                        owner.ended_at = datetime.now(timezone.utc)
            if job is not None:
                job.status = JobStatus.FAILED.value
                job.error_message = str(exc)
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
            log_event(logger, logging.ERROR, "job.failed", job_id=job_id, error=str(exc))
            raise
        finally:
            db.close()
