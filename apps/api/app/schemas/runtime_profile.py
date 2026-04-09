from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class RuntimeProfileCreateRequest(BaseModel):
    project_id: str
    blueprint_id: Optional[str] = None
    name: str = Field(min_length=1, max_length=255)
    model_bindings_json: dict[str, Any] = Field(default_factory=dict)
    skill_bindings_json: dict[str, Any] = Field(default_factory=dict)
    mcp_bindings_json: dict[str, Any] = Field(default_factory=dict)
    env_vars_json: dict[str, Any] = Field(default_factory=dict)
    input_overrides_json: dict[str, Any] = Field(default_factory=dict)
    resource_limits_json: dict[str, Any] = Field(default_factory=dict)
    debug_options_json: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class RuntimeProfileUpdateRequest(BaseModel):
    blueprint_id: Optional[str] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    model_bindings_json: Optional[dict[str, Any]] = None
    skill_bindings_json: Optional[dict[str, Any]] = None
    mcp_bindings_json: Optional[dict[str, Any]] = None
    env_vars_json: Optional[dict[str, Any]] = None
    input_overrides_json: Optional[dict[str, Any]] = None
    resource_limits_json: Optional[dict[str, Any]] = None
    debug_options_json: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = None


class RuntimeProfileResponse(BaseModel):
    id: str
    project_id: str
    blueprint_id: Optional[str] = None
    name: str
    model_bindings_json: dict[str, Any] = Field(default_factory=dict)
    skill_bindings_json: dict[str, Any] = Field(default_factory=dict)
    mcp_bindings_json: dict[str, Any] = Field(default_factory=dict)
    env_vars_json: dict[str, Any] = Field(default_factory=dict)
    input_overrides_json: dict[str, Any] = Field(default_factory=dict)
    resource_limits_json: dict[str, Any] = Field(default_factory=dict)
    debug_options_json: dict[str, Any] = Field(default_factory=dict)
    is_default: bool
    created_at: str
    updated_at: str
