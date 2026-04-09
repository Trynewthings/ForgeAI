from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ArtifactResponse(BaseModel):
    id: str
    build_id: Optional[str] = None
    build_step_id: Optional[str] = None
    run_id: Optional[str] = None
    run_step_id: Optional[str] = None
    executable_id: Optional[str] = None
    artifact_type: str
    storage_kind: str
    path_or_content: str
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str
