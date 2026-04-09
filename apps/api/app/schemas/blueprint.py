from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import BlueprintStatus, BlueprintType
from app.schemas.common import ValidationResultResponse


class BlueprintCreateRequest(BaseModel):
    project_id: str
    requirement_id: Optional[str] = None
    blueprint_type: BlueprintType = BlueprintType.SINGLE_SERVICE
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    version: str = "0.1.0"
    status: BlueprintStatus = BlueprintStatus.DRAFT
    input_schema_json: dict[str, Any] = Field(default_factory=dict)
    output_schema_json: dict[str, Any] = Field(default_factory=dict)
    execution_nodes_json: dict[str, Any] = Field(default_factory=dict)
    execution_steps_json: dict[str, Any] = Field(default_factory=dict)
    configurable_vars_json: dict[str, Any] = Field(default_factory=dict)
    execution_env_json: dict[str, Any] = Field(default_factory=dict)
    dependencies_json: dict[str, Any] = Field(default_factory=dict)
    artifacts_spec_json: dict[str, Any] = Field(default_factory=dict)
    observability_spec_json: dict[str, Any] = Field(default_factory=dict)
    retry_policy_json: dict[str, Any] = Field(default_factory=dict)
    trigger_mode: Optional[str] = None
    runtime_constraints_json: dict[str, Any] = Field(default_factory=dict)
    template_tags_json: dict[str, Any] = Field(default_factory=dict)


class BlueprintUpdateRequest(BaseModel):
    requirement_id: Optional[str] = None
    blueprint_type: Optional[BlueprintType] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    version: Optional[str] = None
    status: Optional[BlueprintStatus] = None
    input_schema_json: Optional[dict[str, Any]] = None
    output_schema_json: Optional[dict[str, Any]] = None
    execution_nodes_json: Optional[dict[str, Any]] = None
    execution_steps_json: Optional[dict[str, Any]] = None
    configurable_vars_json: Optional[dict[str, Any]] = None
    execution_env_json: Optional[dict[str, Any]] = None
    dependencies_json: Optional[dict[str, Any]] = None
    artifacts_spec_json: Optional[dict[str, Any]] = None
    observability_spec_json: Optional[dict[str, Any]] = None
    retry_policy_json: Optional[dict[str, Any]] = None
    trigger_mode: Optional[str] = None
    runtime_constraints_json: Optional[dict[str, Any]] = None
    template_tags_json: Optional[dict[str, Any]] = None


class BlueprintResponse(BaseModel):
    id: str
    project_id: str
    requirement_id: Optional[str] = None
    blueprint_type: BlueprintType
    name: str
    description: Optional[str] = None
    version: str
    status: BlueprintStatus
    input_schema_json: dict[str, Any] = Field(default_factory=dict)
    output_schema_json: dict[str, Any] = Field(default_factory=dict)
    execution_nodes_json: dict[str, Any] = Field(default_factory=dict)
    execution_steps_json: dict[str, Any] = Field(default_factory=dict)
    configurable_vars_json: dict[str, Any] = Field(default_factory=dict)
    execution_env_json: dict[str, Any] = Field(default_factory=dict)
    dependencies_json: dict[str, Any] = Field(default_factory=dict)
    artifacts_spec_json: dict[str, Any] = Field(default_factory=dict)
    observability_spec_json: dict[str, Any] = Field(default_factory=dict)
    retry_policy_json: dict[str, Any] = Field(default_factory=dict)
    trigger_mode: Optional[str] = None
    runtime_constraints_json: dict[str, Any] = Field(default_factory=dict)
    template_tags_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class BlueprintValidationResponse(ValidationResultResponse):
    pass
