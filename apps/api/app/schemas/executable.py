from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import ExecutableStatus, ExecutableType


class ExecutableCreateRequest(BaseModel):
    project_id: str
    blueprint_id: Optional[str] = None
    executable_type: ExecutableType = ExecutableType.REPO
    source_path: Optional[str] = None
    manifest_json: dict[str, Any] = Field(default_factory=dict)
    generated_files_index_json: dict[str, Any] = Field(default_factory=dict)
    build_version: Optional[str] = None
    status: ExecutableStatus = ExecutableStatus.DRAFT


class ExecutableResponse(BaseModel):
    id: str
    project_id: str
    blueprint_id: Optional[str] = None
    executable_type: ExecutableType
    source_path: Optional[str] = None
    manifest_json: dict[str, Any] = Field(default_factory=dict)
    generated_files_index_json: dict[str, Any] = Field(default_factory=dict)
    build_version: Optional[str] = None
    status: ExecutableStatus
    created_at: str
    updated_at: str


class ExecutableListItem(BaseModel):
    id: str
    blueprint_id: Optional[str] = None
    executable_type: ExecutableType
    build_version: Optional[str] = None
    status: ExecutableStatus
    created_at: str
    updated_at: str


class ExecutableFileItem(BaseModel):
    path: str
    kind: str
    content_type: str
    exists: bool
    size_bytes: Optional[int] = None


class ExecutableFileContentResponse(BaseModel):
    path: str
    kind: str
    content_type: str
    formatted_hint: str
    exists: bool
    content: str
