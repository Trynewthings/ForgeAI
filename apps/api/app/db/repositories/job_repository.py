from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.db.models import Job


class JobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, job: Job) -> Job:
        self.db.add(job)
        self.db.flush()
        self.db.refresh(job)
        return job

    def get_by_id(self, job_id: str) -> Optional[Job]:
        statement = select(Job).where(Job.id == job_id)
        return self.db.scalar(statement)

    def list_by_project(self, project_id: str) -> list[Job]:
        statement = select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())
        return list(self.db.scalars(statement).all())

    def get_active_by_owner(self, job_type: str, owner_id: str) -> Optional[Job]:
        statement = (
            select(Job)
            .where(Job.job_type == job_type, Job.owner_id == owner_id, Job.status.in_([JobStatus.PENDING.value, JobStatus.QUEUED.value, JobStatus.STARTED.value]))
            .order_by(Job.created_at.desc())
        )
        return self.db.scalar(statement)
