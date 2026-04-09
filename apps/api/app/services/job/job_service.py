from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import JobStatus, JobType
from app.core.exceptions import AppException
from app.db.models import Job
from app.db.repositories.job_repository import JobRepository
from app.db.repositories.project_repository import ProjectRepository
from app.schemas.job import JobResponse


def _iso(value) -> str:
    return value.isoformat() if value else None


class JobService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.job_repository = JobRepository(db)
        self.project_repository = ProjectRepository(db)

    def create_job(self, *, project_id: str, job_type: str, owner_id: str, payload_json: dict) -> Job:
        active = self.job_repository.get_active_by_owner(job_type, owner_id)
        if active is not None:
            raise AppException("An active job already exists for this owner", status_code=400)
        job = Job(
            project_id=project_id,
            job_type=job_type,
            owner_id=owner_id,
            status=JobStatus.QUEUED.value,
            payload_json=payload_json,
        )
        self.job_repository.create(job)
        self.db.flush()
        return job

    def get_job(self, job_id: str) -> JobResponse:
        job = self.job_repository.get_by_id(job_id)
        if job is None:
            raise AppException("Job not found", status_code=404)
        return self._serialize(job)

    def list_project_jobs(self, project_id: str) -> list[JobResponse]:
        if self.project_repository.get_by_id(project_id) is None:
            raise AppException("Project not found", status_code=404)
        return [self._serialize(item) for item in self.job_repository.list_by_project(project_id)]

    def _serialize(self, job: Job) -> JobResponse:
        return JobResponse(
            id=job.id,
            project_id=job.project_id,
            job_type=job.job_type,
            owner_id=job.owner_id,
            status=job.status,
            payload_json=job.payload_json or {},
            result_json=job.result_json or {},
            error_message=job.error_message,
            celery_task_id=job.celery_task_id,
            started_at=_iso(job.started_at),
            finished_at=_iso(job.finished_at),
            created_at=_iso(job.created_at) or "",
            updated_at=_iso(job.updated_at) or "",
        )
