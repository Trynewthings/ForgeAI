from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import Blueprint
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.requirement_repository import RequirementRepository
from app.schemas.blueprint import (
    BlueprintCreateRequest,
    BlueprintResponse,
    BlueprintUpdateRequest,
    BlueprintValidationResponse,
)


def _iso(value) -> str:
    return value.isoformat() if value else ""


class BlueprintService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.requirement_repository = RequirementRepository(db)
        self.blueprint_repository = BlueprintRepository(db)

    def create_blueprint(self, payload: BlueprintCreateRequest) -> BlueprintResponse:
        project = self.project_repository.get_by_id(payload.project_id)
        if project is None:
            raise AppException("Project not found", status_code=404)
        if payload.requirement_id is not None and self.requirement_repository.get_by_id(payload.requirement_id) is None:
            raise AppException("Requirement not found", status_code=404)
        normalized = self._normalized_payload(payload.model_dump(mode="python", exclude_none=True))
        validation = self.validate_payload(normalized)
        if not validation.valid:
            raise AppException("; ".join(validation.errors), status_code=400)
        blueprint = Blueprint(**normalized)
        self.blueprint_repository.create(blueprint)
        self.db.commit()
        return self._serialize(blueprint)

    def get_blueprint(self, blueprint_id: str) -> BlueprintResponse:
        blueprint = self.blueprint_repository.get_by_id(blueprint_id)
        if blueprint is None:
            raise AppException("Blueprint not found", status_code=404)
        return self._serialize(blueprint)

    def update_blueprint(self, blueprint_id: str, payload: BlueprintUpdateRequest) -> BlueprintResponse:
        blueprint = self.blueprint_repository.get_by_id(blueprint_id)
        if blueprint is None:
            raise AppException("Blueprint not found", status_code=404)
        data = self._normalized_payload(payload.model_dump(exclude_unset=True))
        if "requirement_id" in data and data["requirement_id"] is not None:
            if self.requirement_repository.get_by_id(data["requirement_id"]) is None:
                raise AppException("Requirement not found", status_code=404)
        merged = self._normalized_payload(
            {
                "project_id": blueprint.project_id,
                "requirement_id": blueprint.requirement_id,
                "blueprint_type": blueprint.blueprint_type,
                "name": blueprint.name,
                "description": blueprint.description,
                "version": blueprint.version,
                "status": blueprint.status,
                "input_schema_json": blueprint.input_schema_json or {},
                "output_schema_json": blueprint.output_schema_json or {},
                "execution_nodes_json": blueprint.execution_nodes_json or {},
                "execution_steps_json": blueprint.execution_steps_json or {},
                "configurable_vars_json": blueprint.configurable_vars_json or {},
                "execution_env_json": blueprint.execution_env_json or {},
                "dependencies_json": blueprint.dependencies_json or {},
                "artifacts_spec_json": blueprint.artifacts_spec_json or {},
                "observability_spec_json": blueprint.observability_spec_json or {},
                "retry_policy_json": blueprint.retry_policy_json or {},
                "trigger_mode": blueprint.trigger_mode,
                "runtime_constraints_json": blueprint.runtime_constraints_json or {},
                "template_tags_json": blueprint.template_tags_json or {},
                **data,
            }
        )
        validation = self.validate_payload(merged)
        if not validation.valid:
            raise AppException("; ".join(validation.errors), status_code=400)
        for field_name, value in data.items():
            setattr(blueprint, field_name, value)
        self.db.commit()
        self.db.refresh(blueprint)
        return self._serialize(blueprint)

    def list_project_blueprints(self, project_id: str) -> list[BlueprintResponse]:
        if self.project_repository.get_by_id(project_id) is None:
            raise AppException("Project not found", status_code=404)
        return [self._serialize(item) for item in self.blueprint_repository.list_by_project(project_id)]

    def validate_blueprint(self, blueprint_id: str) -> BlueprintValidationResponse:
        blueprint = self.blueprint_repository.get_by_id(blueprint_id)
        if blueprint is None:
            raise AppException("Blueprint not found", status_code=404)
        return self.validate_payload(
            {
                "blueprint_type": blueprint.blueprint_type,
                "name": blueprint.name,
                "input_schema_json": blueprint.input_schema_json or {},
                "output_schema_json": blueprint.output_schema_json or {},
                "execution_steps_json": blueprint.execution_steps_json or {},
                "configurable_vars_json": blueprint.configurable_vars_json or {},
                "execution_env_json": blueprint.execution_env_json or {},
                "observability_spec_json": blueprint.observability_spec_json or {},
                "retry_policy_json": blueprint.retry_policy_json or {},
            }
        )

    def validate_payload(self, payload: dict) -> BlueprintValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []

        blueprint_type = payload.get("blueprint_type")
        if blueprint_type not in {"single_service", "workflow", "multi_agent"}:
            errors.append("blueprint_type must be one of: single_service, workflow, multi_agent")

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("name is required")

        for schema_field in ("input_schema_json", "output_schema_json"):
            value = payload.get(schema_field, {})
            if value is not None and not isinstance(value, dict):
                errors.append(f"{schema_field} must be a JSON object")
            elif isinstance(value, dict) and value and "type" not in value:
                warnings.append(f"{schema_field} has no top-level 'type' field")

        steps_value = payload.get("execution_steps_json", {})
        normalized_steps = self._normalize_execution_steps(steps_value)
        if normalized_steps["steps"]:
            for index, step in enumerate(normalized_steps["steps"]):
                if not step.get("step_name"):
                    errors.append(f"execution_steps_json.steps[{index}] must contain step_name")
        else:
            warnings.append("execution_steps_json.steps is empty; runtime will fall back to default placeholder steps")

        for field_name in (
            "configurable_vars_json",
            "execution_env_json",
            "observability_spec_json",
            "retry_policy_json",
        ):
            value = payload.get(field_name, {})
            if value is not None and not isinstance(value, dict):
                errors.append(f"{field_name} must be a JSON object")

        return BlueprintValidationResponse(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            normalized={"execution_steps_json": normalized_steps},
        )

    def _serialize(self, blueprint: Blueprint) -> BlueprintResponse:
        return BlueprintResponse(
            id=blueprint.id,
            project_id=blueprint.project_id,
            requirement_id=blueprint.requirement_id,
            blueprint_type=blueprint.blueprint_type,
            name=blueprint.name,
            description=blueprint.description,
            version=blueprint.version,
            status=blueprint.status,
            input_schema_json=blueprint.input_schema_json or {},
            output_schema_json=blueprint.output_schema_json or {},
            execution_nodes_json=blueprint.execution_nodes_json or {},
            execution_steps_json=blueprint.execution_steps_json or {},
            configurable_vars_json=blueprint.configurable_vars_json or {},
            execution_env_json=blueprint.execution_env_json or {},
            dependencies_json=blueprint.dependencies_json or {},
            artifacts_spec_json=blueprint.artifacts_spec_json or {},
            observability_spec_json=blueprint.observability_spec_json or {},
            retry_policy_json=blueprint.retry_policy_json or {},
            trigger_mode=blueprint.trigger_mode,
            runtime_constraints_json=blueprint.runtime_constraints_json or {},
            template_tags_json=blueprint.template_tags_json or {},
            created_at=_iso(blueprint.created_at),
            updated_at=_iso(blueprint.updated_at),
        )

    def _normalized_payload(self, payload: dict) -> dict:
        for field_name, value in list(payload.items()):
            if hasattr(value, "value"):
                payload[field_name] = value.value
        if "execution_steps_json" in payload:
            payload["execution_steps_json"] = self._normalize_execution_steps(payload["execution_steps_json"])
        return payload

    def _normalize_execution_steps(self, value):
        if value is None:
            return {"steps": []}
        if isinstance(value, list):
            return {"steps": value}
        if isinstance(value, dict):
            steps = value.get("steps", [])
            return {"steps": steps if isinstance(steps, list) else []}
        return {"steps": []}
