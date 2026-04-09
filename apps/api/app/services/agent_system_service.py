from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import BlueprintStatus, BlueprintType
from app.core.exceptions import AppException
from app.db.models import Blueprint
from app.db.models.entities import AgentNode, AgentSystem, MCPBinding, ModelConfig, SkillBinding
from app.repositories.agent_node_repository import AgentNodeRepository
from app.repositories.agent_system_repository import AgentSystemRepository
from app.repositories.mcp_binding_repository import MCPBindingRepository
from app.repositories.model_config_repository import ModelConfigRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.skill_binding_repository import SkillBindingRepository
from app.schemas.agent_system import (
    AgentNodeMCPBindingRequest,
    AgentNodeMCPBindingResponse,
    AgentNodeModelBindingRequest,
    AgentNodeResponse,
    AgentNodeSkillBindingRequest,
    AgentNodeSkillBindingResponse,
    AgentSystemResponse,
)
from app.schemas.model_config import ModelConfigResponse
from app.services.mcp_registry_service import MCPRegistryService
from app.services.skill_registry_service import SkillRegistryService


DEFAULT_AGENT_NODES = [
    {
        "name": "Requirement Analyst",
        "role": "requirement_analyst",
        "description": "Owns requirement clarification quality and structured requirement outputs.",
        "input_schema_json": {"type": "object", "properties": {"messages": {"type": "array"}}},
        "output_schema_json": {"type": "object", "properties": {"requirement_json": {"type": "object"}}},
    },
    {
        "name": "Build Planner",
        "role": "build_planner",
        "description": "Turns finalized requirements into build steps and repository plans.",
        "input_schema_json": {"type": "object", "properties": {"requirement_document": {"type": "string"}}},
        "output_schema_json": {"type": "object", "properties": {"step_plan": {"type": "array"}}},
    },
    {
        "name": "Code Generator",
        "role": "code_generator",
        "description": "Produces simulated or provider-backed generation output for build steps.",
        "input_schema_json": {"type": "object", "properties": {"engineering_spec": {"type": "string"}}},
        "output_schema_json": {"type": "object", "properties": {"artifact": {"type": "string"}}},
    },
    {
        "name": "QA Reviewer",
        "role": "qa_reviewer",
        "description": "Reviews generated artifacts, tests, and consistency before completion.",
        "input_schema_json": {"type": "object", "properties": {"artifacts": {"type": "array"}}},
        "output_schema_json": {"type": "object", "properties": {"review_summary": {"type": "string"}}},
    },
]


def _iso(value) -> str:
    return value.isoformat() if value else ""


class AgentSystemService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.agent_system_repository = AgentSystemRepository(db)
        self.agent_node_repository = AgentNodeRepository(db)
        self.model_config_repository = ModelConfigRepository(db)
        self.skill_binding_repository = SkillBindingRepository(db)
        self.mcp_binding_repository = MCPBindingRepository(db)
        self.skill_registry_service = SkillRegistryService()
        self.mcp_registry_service = MCPRegistryService()

    def get_or_initialize_project_agent_system(self, project_id: str) -> AgentSystemResponse:
        project = self.project_repository.get_by_id(project_id)
        if project is None:
            raise AppException("Project not found", status_code=404)

        legacy_blueprint = self._get_or_create_legacy_multi_agent_blueprint(project_id, project.name)
        agent_system = self.agent_system_repository.get_by_project_id(project_id, with_nodes=True)
        if agent_system is None:
            agent_system = AgentSystem(
                project_id=project_id,
                blueprint_id=legacy_blueprint.id,
                name=f"{project.name} Default Agent System",
                description="Legacy multi-agent structure scoped under a multi_agent blueprint.",
                version="0.2.0",
            )
            self.agent_system_repository.create(agent_system)
            for node in DEFAULT_AGENT_NODES:
                self.db.add(
                    AgentNode(
                        agent_system_id=agent_system.id,
                        name=node["name"],
                        role=node["role"],
                        description=node["description"],
                        input_schema_json=node["input_schema_json"],
                        output_schema_json=node["output_schema_json"],
                    )
                )
            self.db.commit()
            agent_system = self.agent_system_repository.get_by_project_id(project_id, with_nodes=True)
            if agent_system is None:
                raise AppException("Agent system initialization failed", status_code=500)
        elif agent_system.blueprint_id is None:
            agent_system.blueprint_id = legacy_blueprint.id
            self.db.commit()
            self.db.refresh(agent_system)

        return self._serialize_agent_system(agent_system)

    def get_agent_node(self, agent_node_id: str) -> AgentNodeResponse:
        agent_node = self.agent_node_repository.get_by_id(agent_node_id, with_relations=True)
        if agent_node is None:
            raise AppException("Agent node not found", status_code=404)
        return self._serialize_agent_node(agent_node)

    def update_agent_node_model(self, agent_node_id: str, payload: AgentNodeModelBindingRequest) -> AgentNodeResponse:
        agent_node = self.agent_node_repository.get_by_id(agent_node_id, with_relations=True)
        if agent_node is None:
            raise AppException("Agent node not found", status_code=404)
        if payload.model_config_id is not None:
            model_config = self.model_config_repository.get_by_id(payload.model_config_id)
            if model_config is None:
                raise AppException("Model config not found", status_code=404)
        agent_node.model_config_id = payload.model_config_id
        self.db.commit()
        self.db.refresh(agent_node)
        return self.get_agent_node(agent_node_id)

    def update_agent_node_skills(self, agent_node_id: str, payload: AgentNodeSkillBindingRequest) -> AgentNodeResponse:
        agent_node = self.agent_node_repository.get_by_id(agent_node_id)
        if agent_node is None:
            raise AppException("Agent node not found", status_code=404)

        available_skills = {item.name for item in self.skill_registry_service.list_skills()}
        bindings = []
        for item in payload.bindings:
            if item.name not in available_skills:
                raise AppException(f"Unsupported skill: {item.name}", status_code=400)
            bindings.append(SkillBinding(agent_node_id=agent_node_id, skill_name=item.name, config_json=item.config_json))
        self.skill_binding_repository.replace_for_node(agent_node_id, bindings)
        self.db.commit()
        return self.get_agent_node(agent_node_id)

    def update_agent_node_mcps(self, agent_node_id: str, payload: AgentNodeMCPBindingRequest) -> AgentNodeResponse:
        agent_node = self.agent_node_repository.get_by_id(agent_node_id)
        if agent_node is None:
            raise AppException("Agent node not found", status_code=404)

        available_mcps = {item.name for item in self.mcp_registry_service.list_mcps()}
        bindings = []
        for item in payload.bindings:
            if item.name not in available_mcps:
                raise AppException(f"Unsupported MCP: {item.name}", status_code=400)
            bindings.append(MCPBinding(agent_node_id=agent_node_id, mcp_name=item.name, config_json=item.config_json))
        self.mcp_binding_repository.replace_for_node(agent_node_id, bindings)
        self.db.commit()
        return self.get_agent_node(agent_node_id)

    def _serialize_agent_system(self, agent_system: AgentSystem) -> AgentSystemResponse:
        return AgentSystemResponse(
            id=agent_system.id,
            project_id=agent_system.project_id,
            name=agent_system.name,
            description=agent_system.description,
            version=agent_system.version,
            nodes=[self._serialize_agent_node(node) for node in sorted(agent_system.agent_nodes, key=lambda item: item.created_at)],
            created_at=_iso(agent_system.created_at),
            updated_at=_iso(agent_system.updated_at),
        )

    def _serialize_agent_node(self, agent_node: AgentNode) -> AgentNodeResponse:
        return AgentNodeResponse(
            id=agent_node.id,
            agent_system_id=agent_node.agent_system_id,
            name=agent_node.name,
            role=agent_node.role,
            description=agent_node.description,
            input_schema_json=agent_node.input_schema_json,
            output_schema_json=agent_node.output_schema_json,
            model_config_id=agent_node.model_config_id,
            model_config_details=self._serialize_model_config(agent_node.model_config) if agent_node.model_config is not None else None,
            skill_bindings=[
                AgentNodeSkillBindingResponse(
                    id=item.id,
                    skill_name=item.skill_name,
                    config_json=item.config_json,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in agent_node.skill_bindings
            ],
            mcp_bindings=[
                AgentNodeMCPBindingResponse(
                    id=item.id,
                    mcp_name=item.mcp_name,
                    config_json=item.config_json,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in agent_node.mcp_bindings
            ],
            created_at=_iso(agent_node.created_at),
            updated_at=_iso(agent_node.updated_at),
        )

    def _serialize_model_config(self, model_config: ModelConfig) -> ModelConfigResponse:
        return ModelConfigResponse(
            id=model_config.id,
            provider=model_config.provider,
            model_name=model_config.model_name,
            endpoint=model_config.endpoint,
            api_key_ref=model_config.api_key_ref,
            temperature=model_config.temperature,
            max_tokens=model_config.max_tokens,
            created_at=_iso(model_config.created_at),
            updated_at=_iso(model_config.updated_at),
        )

    def _get_or_create_legacy_multi_agent_blueprint(self, project_id: str, project_name: str) -> Blueprint:
        blueprint = self.db.query(Blueprint).filter(
            Blueprint.project_id == project_id,
            Blueprint.blueprint_type == BlueprintType.MULTI_AGENT.value,
            Blueprint.name == "Legacy Multi-Agent Blueprint",
        ).first()
        if blueprint is not None:
            return blueprint

        blueprint = Blueprint(
            project_id=project_id,
            requirement_id=None,
            blueprint_type=BlueprintType.MULTI_AGENT.value,
            name="Legacy Multi-Agent Blueprint",
            description=f"Legacy compatibility blueprint for multi-agent structures in project {project_name}.",
            version="0.2.0",
            status=BlueprintStatus.ACTIVE.value,
            execution_nodes_json={"source": "legacy_agent_system"},
        )
        self.db.add(blueprint)
        self.db.flush()
        self.db.refresh(blueprint)
        self.db.commit()
        return blueprint
