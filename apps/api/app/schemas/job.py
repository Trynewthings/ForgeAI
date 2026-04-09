from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import JobStatus, JobType


class JobResponse(BaseModel):
    id: str
    project_id: str
    job_type: JobType
    owner_id: str
    status: JobStatus
    payload_json: dict[str, Any] = Field(default_factory=dict)
    result_json: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    celery_task_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str
