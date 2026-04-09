from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.job import JobResponse
from app.services.job.job_service import JobService

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobResponse:
    return JobService(db).get_job(job_id)


@router.get("/projects/{project_id}/jobs", response_model=list[JobResponse])
def list_project_jobs(project_id: str, db: Session = Depends(get_db)) -> list[JobResponse]:
    return JobService(db).list_project_jobs(project_id)
