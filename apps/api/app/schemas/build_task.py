from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import BuildStepStatus, BuildTaskStatus


class BuildStepResponse(BaseModel):
    id: str
    step_name: str
    step_order: int
    status: BuildStepStatus
    input_payload: Optional[dict[str, Any]] = None
    output_payload: Optional[dict[str, Any]] = None
    prompt_text: Optional[str] = None
    log_text: Optional[str] = None
    token_usage: int
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str


class ArtifactResponse(BaseModel):
    id: str
    build_task_id: str
    build_step_id: Optional[str] = None
    artifact_type: str
    path_or_content: str
    metadata_json: Optional[dict[str, Any]] = None
    created_at: str


class TokenUsageLogResponse(BaseModel):
    id: str
    build_task_id: str
    build_step_id: Optional[str] = None
    provider: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_estimate: Optional[float] = None
    created_at: str


class BuildTaskResponse(BaseModel):
    id: str
    project_id: str
    requirement_session_id: Optional[str] = Field(
        default=None,
        description="Historical legacy linkage for already-created direct session->build tasks.",
    )
    status: BuildTaskStatus
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    total_tokens: int
    error_message: Optional[str] = None
    created_at: str
    updated_at: str
    steps: list[BuildStepResponse] = Field(default_factory=list)
    artifacts: list[ArtifactResponse] = Field(default_factory=list)
    token_usage_logs: list[TokenUsageLogResponse] = Field(default_factory=list)


class BuildTaskEventPayload(BaseModel):
    task_id: str
    status: BuildTaskStatus
    current_step: Optional[str] = None
    total_tokens: int
    error_message: Optional[str] = None
    steps: list[BuildStepResponse] = Field(default_factory=list)
