from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import BuildStatus, BuildStepStatus
from app.core.enums import JobStatus


class BuildCreateRequest(BaseModel):
    project_id: str
    # Exactly one of (blueprint_id, session_id) must be provided. Callers
    # on the legacy path keep supplying blueprint_id; the session-anchored
    # C-slice entry uses session_id (planner_output on FrontChainState).
    blueprint_id: Optional[str] = None
    session_id: Optional[str] = None
    build_options_json: dict[str, Any] = Field(default_factory=dict)


class BuildStepResponse(BaseModel):
    id: str
    build_id: str
    step_name: str
    step_order: int
    status: BuildStepStatus
    input_payload_json: dict[str, Any] = Field(default_factory=dict)
    output_payload_json: dict[str, Any] = Field(default_factory=dict)
    log_excerpt: Optional[str] = None
    token_usage_json: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str


class BuildResponse(BaseModel):
    id: str
    project_id: str
    blueprint_id: Optional[str] = None
    session_id: Optional[str] = None
    executable_id: Optional[str] = None
    active_job_id: Optional[str] = None
    active_job_status: Optional[JobStatus] = None
    build_version: str
    status: BuildStatus
    build_options_json: dict[str, Any] = Field(default_factory=dict)
    manifest_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    summary: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    created_at: str
    updated_at: str
    build_steps: list[BuildStepResponse] = Field(default_factory=list)
