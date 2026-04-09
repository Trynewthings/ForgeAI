from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.model_config import ModelConfigResponse


class SkillRegistryItemResponse(BaseModel):
    name: str
    description: str
    config_schema: dict[str, Any]


class MCPRegistryItemResponse(BaseModel):
    name: str
    description: str
    endpoint: Optional[str] = None
    config_schema: dict[str, Any]


class AgentNodeSkillBindingResponse(BaseModel):
    id: str
    skill_name: str
    config_json: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class AgentNodeMCPBindingResponse(BaseModel):
    id: str
    mcp_name: str
    config_json: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class AgentNodeResponse(BaseModel):
    id: str
    agent_system_id: str
    name: str
    role: str
    description: Optional[str] = None
    input_schema_json: Optional[dict[str, Any]] = None
    output_schema_json: Optional[dict[str, Any]] = None
    model_config_id: Optional[str] = None
    model_config_details: Optional[ModelConfigResponse] = Field(default=None, serialization_alias="model_config")
    skill_bindings: list[AgentNodeSkillBindingResponse] = Field(default_factory=list)
    mcp_bindings: list[AgentNodeMCPBindingResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class AgentSystemResponse(BaseModel):
    id: str
    project_id: str
    name: str
    description: Optional[str] = None
    version: str
    nodes: list[AgentNodeResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class AgentNodeModelBindingRequest(BaseModel):
    model_config_id: Optional[str] = None


class NamedConfigPayload(BaseModel):
    name: str
    config_json: dict[str, Any] = Field(default_factory=dict)


class AgentNodeSkillBindingRequest(BaseModel):
    bindings: list[NamedConfigPayload] = Field(default_factory=list)


class AgentNodeMCPBindingRequest(BaseModel):
    bindings: list[NamedConfigPayload] = Field(default_factory=list)
