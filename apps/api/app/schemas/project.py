from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import BuildStatus, BuildTaskStatus, ExecutableStatus, ExecutableType, ProjectStatus, RunStatus


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None


class BuildTaskListItem(BaseModel):
    id: str
    status: BuildTaskStatus
    total_tokens: int
    created_at: str
    updated_at: str


class RequirementListItem(BaseModel):
    id: str
    title: str
    status: str
    version: int
    created_at: str
    updated_at: str


class BlueprintListItem(BaseModel):
    id: str
    requirement_id: Optional[str] = None
    blueprint_type: str
    name: str
    status: str
    version: str
    created_at: str
    updated_at: str


class RuntimeProfileListItem(BaseModel):
    id: str
    blueprint_id: Optional[str] = None
    name: str
    is_default: bool
    created_at: str
    updated_at: str


class BuildListItem(BaseModel):
    id: str
    blueprint_id: str
    executable_id: Optional[str] = None
    build_version: str
    status: BuildStatus
    created_at: str
    updated_at: str


class ExecutableListItem(BaseModel):
    id: str
    blueprint_id: str
    executable_type: ExecutableType
    build_version: Optional[str] = None
    status: ExecutableStatus
    created_at: str
    updated_at: str


class RunListItem(BaseModel):
    id: str
    blueprint_id: str
    executable_id: Optional[str] = None
    runtime_profile_id: Optional[str] = None
    status: RunStatus
    created_at: str
    updated_at: str


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    status: ProjectStatus
    created_at: str
    updated_at: str


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse] = Field(default_factory=list)
    total: int = 0


class ProjectDetailResponse(ProjectResponse):
    build_tasks: list[BuildTaskListItem] = Field(default_factory=list)
    requirements: list[RequirementListItem] = Field(default_factory=list)
    blueprints: list[BlueprintListItem] = Field(default_factory=list)
    builds: list[BuildListItem] = Field(default_factory=list)
    executables: list[ExecutableListItem] = Field(default_factory=list)
    runtime_profiles: list[RuntimeProfileListItem] = Field(default_factory=list)
    runs: list[RunListItem] = Field(default_factory=list)
